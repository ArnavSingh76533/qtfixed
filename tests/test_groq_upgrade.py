import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
import httpx
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('DATA_DIR',tempfile.mkdtemp())
import main, config, primo
from provider import GroqClient, ProviderError
from formatting import formatted_chunks, units
from chat_store import ChatStore
from broadcast import BroadcastManager, parse_broadcast
from streaming import StreamPreview
from telegram.error import BadRequest, Forbidden, NetworkError


def message():
    return NS(chat=NS(type='private'),chat_id=123,message_id=10,message_thread_id=None,
        text='Hello',sender_chat=None,reply_text=AsyncMock(),reply_document=AsyncMock(),delete=AsyncMock())


def make_update():
    m=message()
    return NS(message=m,effective_message=m,effective_user=NS(id=123),effective_chat=NS(id=123,type='private'))


class GroqTests(unittest.IsolatedAsyncioTestCase):
    async def make(self, handler):
        return GroqClient('fake-key',httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    async def test_stream_payload_and_reasoning_excluded(self):
        requests=[]
        def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200,text='data: '+json.dumps({'choices':[{'delta':{'reasoning':'private','content':'Hello '}}]})+'\n\ndata: '+json.dumps({'choices':[{'delta':{'content':'world'},'finish_reason':'stop'}]})+'\n\ndata: [DONE]\n\n')
        client=await self.make(handler)
        try:
            result=''.join([s async for s in client.stream([{'role':'user','content':'hi'}])])
            self.assertEqual(result,'Hello world')
            self.assertEqual(requests[0]['model'],'openai/gpt-oss-120b')
            self.assertFalse(requests[0]['include_reasoning'])
            self.assertTrue(requests[0]['stream'])
        finally:await client.close()

    async def test_auth_error_does_not_echo_secret(self):
        client=await self.make(lambda request:httpx.Response(401,text='fake-key secret'))
        with self.assertRaises(ProviderError) as caught:
            _=[s async for s in client.stream([])]
        self.assertNotIn('fake-key',str(caught.exception));await client.close()

    async def test_broken_stream_not_silently_accepted(self):
        client=await self.make(lambda req:httpx.Response(200,text='data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'))
        with self.assertRaises(ProviderError):
            _=[s async for s in client.stream([])]
        await client.close()

    async def test_rate_limit_retry(self):
        count=0
        def handler(req):
            nonlocal count
            count+=1
            if count==1:return httpx.Response(429,headers={'retry-after':'0'})
            return httpx.Response(200,text='data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n')
        client=await self.make(handler)
        with patch('provider.asyncio.sleep',AsyncMock()):
            result=''.join([s async for s in client.stream([])])
        self.assertEqual(result,'ok');self.assertEqual(count,2);await client.close()

    async def test_empty_stream_is_error(self):
        client=await self.make(lambda req:httpx.Response(200,text='data: [DONE]\n'))
        with self.assertRaises(ProviderError):_=[s async for s in client.stream([])]
        await client.close()


class FormattingTests(unittest.TestCase):
    def test_markdown_and_unicode_offsets(self):
        text,entities=next(formatted_chunks('😀 **Bold** and `x < y`'))
        self.assertEqual(text,'😀 Bold and x < y')
        bold=next(e for e in entities if e.type=='bold')
        self.assertEqual((bold.offset,bold.length),(3,4))
        self.assertTrue(any(e.type=='code' for e in entities))

    def test_long_code_split_preserves_all_content(self):
        body='😀x\n'*3000
        chunks=list(formatted_chunks('```python\n'+body+'```'))
        self.assertEqual(''.join(t for t,e in chunks),body.rstrip('\n'))
        self.assertGreater(len(chunks),1)
        for text,entities in chunks:
            self.assertLessEqual(units(text),3800)
            self.assertTrue(any(e.type=='pre' and e.language=='python' for e in entities))
            for e in entities:self.assertLessEqual(e.offset+e.length,units(text))

    def test_spoilers_quotes_links(self):
        chunks=list(formatted_chunks('> **Quote**\n\n||hidden|| [site](https://example.com)'))
        kinds={e.type for t,ents in chunks for e in ents}
        self.assertTrue({'blockquote','bold','spoiler','text_link'} <= kinds)

    def test_html_is_literal(self):
        text,entities=next(formatted_chunks('<b>hello</b>'))
        self.assertEqual(text,'<b>hello</b>');self.assertFalse(entities)

    def test_original_system_prompt(self):
        self.assertIn('Your name is Question Ai.',config.SYSTEM_PROMPT)
        self.assertIn('short and general answers',config.SYSTEM_PROMPT)
        self.assertIn("Use LaTeX",config.SYSTEM_PROMPT)
        messages=main.provider_messages([],'Hello',{'style':'balanced'})
        self.assertEqual(messages[0]['role'],'system')
        self.assertTrue(messages[0]['content'].startswith(config.SYSTEM_PROMPT))


class ChatTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=ChatStore(Path(self.temp.name)/'chats.db')
        self.bot=NS(send_chat_action=AsyncMock(),send_message_draft=AsyncMock(),send_document=AsyncMock())
        self.ctx=NS(bot=self.bot,application=NS(bot_data={'chat_store':self.store,'active_requests':{},'groq':None}))
        self.u=make_update();self.u.message.reply_text.return_value=NS(delete=AsyncMock(),edit_text=AsyncMock())
        self.user={'user_id':'123','request_count':0,'subscription':'inactive','sub_end':None,'last_request_time':None}
    def tearDown(self):self.store.close();self.temp.cleanup()

    def test_persistent_history_isolation(self):
        self.store.save('1:0:1',[{'role':'user','content':'private'}],{'streaming':False})
        self.store.close();self.store=ChatStore(Path(self.temp.name)/'chats.db')
        self.assertEqual(self.store.get('1:0:1')[0][0]['content'],'private')
        self.assertEqual(self.store.get('2:0:1')[0],[])

    async def test_success_commits_once(self):
        async def stream(*a,**kw):yield '**Answer**'
        self.ctx.application.bot_data['groq']=NS(stream=stream)
        with patch.object(main,'charge_request') as charge,patch.object(main,'record_log',AsyncMock()):
            await main.generate_answer(self.u,self.ctx,self.user,'question')
        charge.assert_called_once_with(self.user)
        history,_=self.store.get('123:0:123')
        self.assertEqual(len(history),2);self.assertEqual(history[-1]['content'],'**Answer**')

    async def test_failure_preserves_history_and_quota(self):
        async def stream(*a,**kw):
            raise ProviderError('offline')
            yield ''
        self.ctx.application.bot_data['groq']=NS(stream=stream)
        with patch.object(main,'charge_request') as charge:
            await main.generate_answer(self.u,self.ctx,self.user,'question')
        charge.assert_not_called();self.assertEqual(self.store.get('123:0:123')[0],[])

    async def test_retry_replaces_last_exchange(self):
        prior=[{'role':'user','content':'one'},{'role':'assistant','content':'old'}]
        self.store.save('123:0:123',prior,{'style':'balanced'})
        async def stream(*a,**kw):yield 'new'
        self.ctx.application.bot_data['groq']=NS(stream=stream)
        with patch.object(main,'charge_request'),patch.object(main,'record_log',AsyncMock()):
            await main.generate_answer(self.u,self.ctx,self.user,'one',True)
        history,_=self.store.get('123:0:123');self.assertEqual(len(history),2);self.assertEqual(history[-1]['content'],'new')

    async def test_stop_cancels_task(self):
        async def forever(*a,**kw):
            await asyncio.sleep(100)
            yield 'late'
        self.ctx.application.bot_data['groq']=NS(stream=forever)
        task=asyncio.create_task(main.generate_answer(self.u,self.ctx,self.user,'q'))
        self.ctx.application.bot_data['active_requests'][123]=task
        await asyncio.sleep(0)
        with patch.object(main,'charge_request') as charge:
            await main.stop_command(self.u,self.ctx)
        self.assertTrue(task.done());charge.assert_not_called()
        self.assertNotIn(123,self.ctx.application.bot_data['active_requests'])

    async def test_foreign_callback_denied(self):
        query=NS(data='chat:new:456',from_user=NS(id=123),answer=AsyncMock())
        await main.chat_callback(NS(callback_query=query),self.ctx)
        self.assertTrue(query.answer.call_args.kwargs['show_alert'])

    async def test_draft_fallback_to_edits(self):
        self.bot.send_message_draft.side_effect=BadRequest('unsupported')
        preview=StreamPreview(self.u.message,self.bot,123)
        await preview.start();await preview.update('hello')
        self.assertFalse(preview.draft)
        preview.status.edit_text.assert_awaited_once()


class CampaignTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'campaign.db'
        self.bot=NS(send_message=AsyncMock(return_value=NS(message_id=1)),copy_message=AsyncMock(),pin_chat_message=AsyncMock())
        self.m=BroadcastManager(self.path,self.bot)
        self.payload=parse_broadcast('/broadcast -user -delay 0.1 -- Hi')
    async def asyncTearDown(self):await self.m.close();self.temp.cleanup()

    def test_flags_multiline_buttons(self):
        o=parse_broadcast('/broadcast -user -premium -active 7 -random 10 --button "Open|https://example.com" -- **Hi**\nsecond line')
        self.assertEqual(o['text'],'**Hi**\nsecond line');self.assertEqual(o['random'],10)
        self.assertEqual(o['buttons'][0][0],'Open')

    def test_reply_copy_and_legacy_text(self):
        self.assertTrue(parse_broadcast('/broadcast -user',True)['copy'])
        self.assertEqual(parse_broadcast('/broadcast -user read this')['text'],'read this')
        with self.assertRaises(ValueError):parse_broadcast('/broadcast -user r0 hi')

    async def test_campaign_sent_only_once(self):
        cid=self.m.create(self.payload,['123'],999)
        self.m.transition(cid,'start');await self.m.worker
        self.assertEqual(self.m.counts(cid),{'sent':1})
        with self.assertRaises(ValueError):self.m.transition(cid,'start')
        self.assertEqual(self.bot.send_message.await_count,2) # recipient + admin summary

    async def test_blocked_recipient_recorded_not_deleted(self):
        self.bot.send_message.side_effect=[Forbidden('blocked'),NS(message_id=1)]
        cid=self.m.create(self.payload,['123'],999);self.m.transition(cid,'start');await self.m.worker
        self.assertEqual(self.m.counts(cid),{'skipped':1})
        self.assertEqual(self.m.db.execute('SELECT chat FROM blocked').fetchone()[0],'123')

    async def test_bad_request_is_retryable_failure_not_uncertain(self):
        self.bot.send_message.side_effect=[BadRequest('chat not found'),NS(message_id=1)]
        cid=self.m.create(self.payload,['123'],999);self.m.transition(cid,'start');await self.m.worker
        self.assertEqual(self.m.counts(cid),{'failed':1})
        self.m.transition(cid,'retry_failed')
        self.assertEqual(self.m.counts(cid),{'pending':1})

    def test_activity_filter_keeps_older_than_one_day(self):
        import datetime as dt
        last=(dt.datetime.now(dt.timezone.utc)-dt.timedelta(days=3)).isoformat()
        records={'123':{'user_id':'123','request_count':20,'subscription':'inactive','last_request_time':last}}
        payload=parse_broadcast('/broadcast -user -active 7 -- Hi')
        with patch('broadcast.user_data_cache',records):
            self.assertEqual(self.m.targets(payload),['123'])
        self.assertEqual(records['123']['request_count'],20)

    async def test_network_ambiguity_not_retried(self):
        self.bot.send_message.side_effect=[NetworkError('timeout'),NS(message_id=1)]
        cid=self.m.create(self.payload,['123'],999);self.m.transition(cid,'start');await self.m.worker
        self.assertEqual(self.m.counts(cid),{'uncertain':1})
        self.m.transition(cid,'retry_failed')
        self.assertEqual(self.m.counts(cid),{'uncertain':1})

    async def test_restart_pauses_and_preserves_delivery_state(self):
        cid=self.m.create(self.payload,['123','456'],999)
        self.m.recipient_state(cid,'123','sent');self.m.recipient_state(cid,'456','sending')
        with self.m.db:self.m.db.execute("UPDATE campaigns SET state='running' WHERE id=?",(cid,))
        await self.m.close();self.m=BroadcastManager(self.path,self.bot)
        self.assertEqual(self.m.get(cid)['state'],'paused')
        self.assertEqual(self.m.counts(cid),{'sent':1,'uncertain':1})
