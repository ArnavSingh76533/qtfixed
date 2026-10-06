import asyncio
import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
import httpx
from PIL import Image
from telegram import Update
from telegram.error import BadRequest
from telegram.ext import ApplicationHandlerStop
import main,config,primo
from rich_messages import normalize_math,rich_pages,asked,api
from runtime_settings import get_settings,initialize,save_settings
from request_queue import submit,cancel
from guest_mode import guest_update
from image_generation import ImageClient,ImageRequested,image_count,image_intent,ContentRejected
from inline_mode import inline_query,start_inline,generate_inline
from test_group_ocr_inline_web import fixture

class RichTests(unittest.IsolatedAsyncioTestCase):
    def test_delimiters_converted_but_code_untouched(self):
        text=r'Use \(\frac{1}{2}\). Then \[x^2\]. `\(literal\)`'
        out=normalize_math(text)
        self.assertIn(r'$\frac{1}{2}$',out)
        self.assertIn('$$\nx^2\n$$',out)
        self.assertIn(r'`\(literal\)`',out)
        self.assertEqual(normalize_math('```python\ns = "\\[x\\]"\n```'),'```python\ns = "\\[x\\]"\n```')

    def test_rich_pages_keep_formulas_and_full_text(self):
        text=('A paragraph.\n\n'*2000)+'$$\\frac{1}{z-1}$$'
        pages=list(rich_pages(text))
        self.assertEqual(''.join(pages),text)
        self.assertTrue(all(len(p.encode())<=24000 for p in pages))
        self.assertIn('$$\\frac{1}{z-1}$$',pages[-1])

    async def test_regular_answer_uses_native_rich_method(self):
        u,c=fixture();u.message.get_bot=lambda:c.bot
        await main.deliver_answer(u.message,r'## Result\n\[\frac{5}{2}\]',123)
        call=c.bot._post.call_args
        self.assertEqual(call.args[0],'sendRichMessage')
        body=call.kwargs['data']
        self.assertIn('$$',body['rich_message']['markdown'])
        self.assertEqual(body['reply_parameters']['message_id'],8)
        self.assertNotIn('Settings',str(body))
        u.message.reply_photo.assert_not_awaited()

    async def test_unsupported_rich_falls_back_readably(self):
        u,c=fixture();u.message.get_bot=lambda:c.bot
        c.bot._post.side_effect=BadRequest('unknown method')
        await main.deliver_answer(u.message,r'\(\frac{1}{z-1}\)')
        text=u.message.reply_text.call_args.args[0]
        self.assertIn('(1)/(z-1)',text);self.assertNotIn('\\frac',text)

    def test_asked_query_is_literal(self):
        out=asked('**hello** $5 <b>x</b>','Answer')
        self.assertEqual(out,'Answer')

class AdminQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_regular_user_cannot_read_or_change_global_settings(self):
        u,c=fixture(False)
        with patch.object(main,'ADMIN_ID','999'):
            await main.settings_command(u,c);await main.model_command(u,c)
            u.callback_query=NS(data='admin:mode:guest',answer=AsyncMock())
            await main.admin_callback(u,c)
        self.assertEqual(get_settings(c)['mode'],'inline')
        self.assertIn('premium',u.message.reply_text.call_args.args[0])
        self.assertTrue(u.callback_query.answer.call_args.kwargs['show_alert'])

    async def test_modes_are_global_persisted_and_exclusive(self):
        u,c=fixture(False)
        u.callback_query=NS(data='admin:mode:guest',answer=AsyncMock(),edit_message_text=AsyncMock())
        with tempfile.TemporaryDirectory() as d,patch.object(config,'DATA_DIR',Path(d)),patch.object(main,'ADMIN_ID','123'):
            await main.admin_callback(u,c)
            restarted=NS(bot_data={});initialize(restarted)
            self.assertEqual(restarted.bot_data['global_settings']['mode'],'guest')
            u.callback_query.data='admin:mode:off';await main.admin_callback(u,c)
            self.assertEqual(get_settings(c)['mode'],'off')

    async def test_queue_serializes_same_user_without_blocking_other_users(self):
        _,c=fixture();order=[];release=asyncio.Event();started=asyncio.Event()
        async def first():order.append('first');started.set();await release.wait();order.append('end')
        async def second():order.append('second')
        async def other():order.append('other')
        one=submit(c,123,'group',first);two=submit(c,123,'group',second);three=submit(c,456,'group',other)
        await started.wait();await three
        self.assertNotIn('second',order)
        release.set();await asyncio.gather(one,two)
        self.assertLess(order.index('end'),order.index('second'))
        self.assertEqual(c.application.bot_data['active_requests'],{})

    async def test_queued_questions_recheck_quota_before_generating(self):
        u,c=fixture();count=39;completed=[]
        async def eligible(*args):return {'subscription':'inactive'} if count<40 else None
        async def answer(*args,**kwargs):
            nonlocal count
            await asyncio.sleep(0);completed.append(args[3]);count+=1
        with patch.object(main,'eligible_user',eligible),patch.object(main,'generate_answer',answer):
            await main.launch_question(u,c,prompt='first');await main.launch_question(u,c,prompt='second')
            await asyncio.gather(*list(c.application.bot_data['active_requests'].values()))
        self.assertEqual(completed,['first']);u.message.reply_text.assert_not_awaited()

    async def test_cancel_before_task_starts_cleans_queue(self):
        _,c=fixture();work=AsyncMock()
        submit(c,123,'group',work)
        self.assertTrue(await cancel(c,123))
        work.assert_not_awaited();self.assertEqual(c.application.bot_data['active_requests'],{})

class GuestTests(unittest.IsolatedAsyncioTestCase):
    def update(self,bot):
        return Update.de_json({'update_id':1,'guest_message':{'message_id':7,'date':1700000000,
            'chat':{'id':-123,'type':'group','title':'Guest chat'},'from':{'id':123,'first_name':'Tester','is_bot':False},
            'guest_query_id':'q1','text':'@queryaibot explain this',
            'reply_to_message':{'message_id':6,'date':1700000000,'chat':{'id':-123,'type':'group','title':'Guest chat'},'text':'x²=4'}}},bot)

    async def test_real_guest_update_uses_query_id_and_keeps_context_isolated(self):
        _,c=fixture();c.application.bot_data['global_settings']={'mode':'guest'}
        c.bot._post.return_value={'inline_message_id':'guest-inline'}
        update=self.update(c.bot)
        with patch('guest_mode.user_data_cache',{'123':{'user_id':'123'}}),patch('guest_mode.start_inline',AsyncMock()) as start:
            with self.assertRaises(ApplicationHandlerStop):await guest_update(update,c)
        call=c.bot._post.call_args
        self.assertEqual(call.args[0],'answerGuestQuery');self.assertEqual(call.kwargs['data']['guest_query_id'],'q1')
        item=next(iter(c.application.bot_data['inline_sessions'].values()))
        self.assertEqual(item['query'],'explain this');self.assertIn('x²=4',item['context']);self.assertEqual(item['mode'],'guest')
        start.assert_awaited_once()
        self.assertNotIn('chat_store',c.application.bot_data)

    async def test_guest_disabled_does_not_answer(self):
        _,c=fixture()
        with self.assertRaises(ApplicationHandlerStop):await guest_update(self.update(c.bot),c)
        c.bot._post.assert_not_awaited()

    async def test_inline_disabled_returns_no_results(self):
        _,c=fixture();c.application.bot_data['global_settings']={'mode':'guest'}
        query=NS(from_user=NS(id=123,is_bot=False),query='hello',answer=AsyncMock())
        await inline_query(NS(inline_query=query),c)
        self.assertEqual(query.answer.call_args.args[0],[])

    async def test_inline_uses_global_style_and_native_math(self):
        _,c=fixture();c.application.bot_data['global_settings']={'style':'detailed','reasoning':'high','streaming':False}
        captured=[]
        async def stream(messages,**kwargs):captured.append((messages,kwargs));yield r'\[\frac{5}{2}\]'
        c.application.bot_data.update(groq=NS(stream=stream),web=None)
        item={'query':'solve','owner':123,'running':True,'pages':None}
        with patch('inline_mode.charge_request') as charge:
            await generate_inline(c,'id','token',item,{'subscription':'inactive','request_count':0})
        self.assertIn('thorough',captured[0][0][0]['content']);self.assertEqual(captured[0][1]['reasoning'],'high')
        body=c.bot._post.call_args.kwargs['data']
        self.assertNotIn('Asked:',body['rich_message']['markdown']);self.assertIn('$$',body['rich_message']['markdown'])
        charge.assert_called_once()

class ImageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        out=BytesIO();Image.new('RGB',(32,18),'blue').save(out,format='JPEG');self.image=out.getvalue()
        self.keys=[patch.object(config,'FAL_API_KEY','fake-fal'),patch.object(config,'GETIMG_API_KEY','fake-getimg')]
        for p in self.keys:p.start()
    def tearDown(self):
        for p in self.keys:p.stop()

    async def test_primary_https_and_default_16_9_with_four_images(self):
        calls=[]
        def handler(request):
            calls.append(request)
            if request.method=='POST':return httpx.Response(200,json={'images':[{'url':f'https://v3.fal.media/{i}.jpeg'} for i in range(4)]})
            return httpx.Response(200,content=self.image)
        client=ImageClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        try:images=await client.generate('a mountain',4)
        finally:await client.close()
        self.assertEqual(len(images),4);payload=json.loads(calls[0].content)
        self.assertEqual(str(calls[0].url),'https://fal.run/fal-ai/flux/schnell')
        self.assertEqual(payload['image_size'],'landscape_16_9');self.assertEqual(payload['num_images'],4)

    async def test_primary_failure_falls_back_and_getimg_returns_four(self):
        calls=[]
        def handler(request):
            calls.append(request)
            if request.url.host=='fal.run':return httpx.Response(503)
            if request.method=='POST':return httpx.Response(200,json={'url':'https://images.getimg.ai/image.jpeg'})
            return httpx.Response(200,content=self.image)
        client=ImageClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        try:images=await client.generate('a mountain',4)
        finally:await client.close()
        self.assertEqual(len(images),4)
        posts=[r for r in calls if r.method=='POST' and r.url.host=='api.getimg.ai']
        self.assertEqual(len(posts),4);data=json.loads(posts[0].content)
        self.assertEqual((data['width'],data['height']),(1024,576))

    async def test_both_fail_safe_error_no_secret(self):
        client=ImageClient(httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(401))))
        try:
            with self.assertRaisesRegex(Exception,'No quota was used') as error:await client.generate('landscape',1)
            self.assertNotIn('fake-',str(error.exception))
        finally:await client.close()

    async def test_content_rejection_is_not_retried_on_backup(self):
        client=ImageClient(httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'has_nsfw_concepts':[True]}))))
        try:
            with patch.object(client,'getimg',AsyncMock()) as fallback:
                with self.assertRaises(ContentRejected):await client.generate('test',1)
                fallback.assert_not_awaited()
        finally:await client.close()

    def test_counts_and_intent(self):
        self.assertEqual(image_count({'subscription':'inactive'}),1)
        self.assertEqual(image_count({'subscription':'active'}),4)
        self.assertTrue(image_intent('Create an image of a mountain'))
        self.assertFalse(image_intent('Explain how image generation works'))

    async def test_model_image_tool_routes_to_generation(self):
        from provider import GroqClient
        event={'choices':[{'delta':{'tool_calls':[{'index':0,'function':{'name':'generate_image','arguments':'{"prompt":"blue mountain"}'}}]},'finish_reason':'tool_calls'}]}
        transport=httpx.MockTransport(lambda r:httpx.Response(200,text='data: '+json.dumps(event)+'\n\ndata: [DONE]\n\n'))
        client=GroqClient('fake',httpx.AsyncClient(transport=transport))
        try:
            with self.assertRaises(ImageRequested) as request:
                async for _ in client.stream([{'role':'user','content':'a picture please'}],allow_image=True):pass
            self.assertEqual(request.exception.prompt,'blue mountain')
        finally:await client.close()

    async def test_successful_image_delivery_charges_once(self):
        from chat_store import ChatStore
        u,c=fixture(False)
        with tempfile.TemporaryDirectory() as d:
            store=ChatStore(Path(d)/'chat.db');c.application.bot_data['chat_store']=store
            with patch.object(main,'generate_images',AsyncMock(return_value=[self.image])),patch('image_generation.cache_images',AsyncMock(return_value=['file-id'])),patch.object(main,'charge_request') as charge:
                await main.generate_answer(u,c,{'subscription':'inactive'},'mountain',force_image=True)
                charge.assert_called_once();self.assertEqual(c.bot._post.call_args.args[0],'sendRichMessage')
                self.assertEqual(c.bot._post.call_args.kwargs['data']['chat_id'],123)
            store.close()

class TransportAndDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_rich_payload_serializes_through_real_ptb_transport(self):
        from telegram import Bot
        from telegram.request import BaseRequest
        class CaptureRequest(BaseRequest):
            @property
            def read_timeout(self):return 5
            async def initialize(self):pass
            async def shutdown(self):pass
            async def do_request(self,url,method,request_data=None,**kwargs):
                self.payload=request_data.json_parameters
                self.endpoint=url.rsplit('/',1)[-1]
                return 200,b'{"ok":true,"result":true}'
        transport=CaptureRequest();bot=Bot('123456:fake-token',request=transport)
        await api(bot,'sendRichMessage',chat_id=123,rich_message={'markdown':r'$$\frac{5}{2}$$'})
        self.assertEqual(transport.endpoint,'sendRichMessage')
        payload=json.loads(transport.payload['rich_message'])
        self.assertEqual(payload['markdown'],r'$$\frac{5}{2}$$')
        await transport.shutdown()

    async def test_failed_image_request_never_charges(self):
        from chat_store import ChatStore
        from provider import ProviderError
        u,c=fixture(False)
        with tempfile.TemporaryDirectory() as d:
            store=ChatStore(Path(d)/'chat.db');c.application.bot_data['chat_store']=store
            with patch.object(main,'generate_images',AsyncMock(side_effect=ProviderError('unavailable'))),patch.object(main,'charge_request') as charge:
                await main.generate_answer(u,c,{'subscription':'inactive'},'mountain',force_image=True)
            charge.assert_not_called();u.message.reply_photo.assert_not_awaited();store.close()

    async def test_image_log_contains_original_prompt_plain_text(self):
        from image_generation import generate_images
        _,c=fixture();c.application.bot_data['images']=NS(generate=AsyncMock(return_value=[b'image']))
        with patch.object(main,'LOG_CHANNEL_ID','-123'):
            await generate_images(c,123,{'subscription':'inactive'},'rewritten prompt',original_prompt='<b>original prompt</b>')
        call=c.bot.send_message.call_args.kwargs
        self.assertIn('<b>original prompt</b>',call['text']);self.assertIsNone(call['parse_mode'])
        self.assertEqual(call['chat_id'],'-123')

    async def test_guest_only_users_excluded_from_private_broadcast(self):
        from broadcast import BroadcastManager
        import broadcast
        _,c=fixture()
        with tempfile.TemporaryDirectory() as d,patch.object(broadcast,'user_data_cache',{
                '1':{'subscription':'inactive','dm_started':False},'2':{'subscription':'inactive'}}):
            manager=BroadcastManager(Path(d)/'campaign.db',c.bot)
            try:
                result=manager.targets({'audience':['user'],'segment':None,'days':None,'random':None})
                self.assertEqual(result,['2'])
            finally:await manager.close()
