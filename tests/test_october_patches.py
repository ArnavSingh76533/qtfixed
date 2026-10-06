import asyncio
import html
import json
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
import httpx
from telegram import Document, Update
from telegram.ext import ApplicationHandlerStop
from telegram.error import NetworkError, BadRequest
import main, config, group_agent, broadcast
from answer_engine import answer_stream
from provider import GroqClient, ProviderError
from runtime_settings import initialize, get_settings
from guest_mode import guest_update
from inline_mode import generate_inline
from documents import read_text_document, MAX_TEXT_BYTES
from image_generation import cache_images, ImageRequested
from rich_messages import with_code_copy, normalize_math
from broadcast import BroadcastManager, parse_broadcast, collect_album
from test_group_ocr_inline_web import fixture

EVIDENCE='Current evidence with a verified date and source https://example.org/report.'

class SearchTests(unittest.IsolatedAsyncioTestCase):
    def groq(self):
        self.calls=[]
        async def stream(messages,**kwargs):
            self.calls.append((messages,kwargs));yield 'Synthesized answer'
        return NS(stream=stream,browser_search=AsyncMock(return_value=EVIDENCE))

    async def test_primary_evidence_enters_conversation_then_streams(self):
        groq=self.groq();web=NS(search=AsyncMock(return_value=EVIDENCE))
        messages=[{'role':'system','content':'Be helpful'},{'role':'user','content':'Earlier question'},
                  {'role':'assistant','content':'Earlier answer'},{'role':'user','content':'weather today'}]
        answer=''.join([v async for v in answer_stream(groq,web,messages)])
        self.assertEqual(answer,'Synthesized answer');groq.browser_search.assert_not_awaited()
        sent,kwargs=self.calls[0];self.assertIn(messages[2],sent)
        self.assertEqual(sent[-1]['role'],'tool');self.assertIn(EVIDENCE,sent[-1]['content'])
        self.assertFalse(kwargs['allow_web']);self.assertFalse(kwargs['allow_image'])

    async def test_failure_short_empty_incomplete_use_backup(self):
        for value in (ProviderError('429'),'', 'tiny', 'Some evidence '+('x'*60)+' stream ended early'):
            groq=self.groq();web=NS(search=AsyncMock())
            if isinstance(value,Exception):web.search.side_effect=value
            else:web.search.return_value=value
            result=''.join([v async for v in answer_stream(groq,web,[{'role':'user','content':'news today'}])])
            self.assertEqual(result,'Synthesized answer');groq.browser_search.assert_awaited_once()

    async def test_both_searches_fail_no_fabricated_answer(self):
        groq=self.groq();groq.browser_search.side_effect=ProviderError('fail')
        with self.assertRaisesRegex(ProviderError,'could not verify'):
            async for _ in answer_stream(groq,NS(search=AsyncMock(side_effect=ProviderError('fail'))),[{'role':'user','content':'news today'}]):pass
        self.assertFalse(self.calls)

    async def test_disabled_blocks_both_search_and_web_command(self):
        groq=self.groq();web=NS(search=AsyncMock())
        async for _ in answer_stream(groq,web,[{'role':'user','content':'latest news'}],web_enabled=False):pass
        self.assertFalse(self.calls[0][1]['allow_web'])
        with self.assertRaisesRegex(ProviderError,'disabled'):
            async for _ in answer_stream(groq,web,[{'role':'user','content':'news'}],force_web=True,web_enabled=False):pass
        web.search.assert_not_awaited();groq.browser_search.assert_not_awaited()

    async def test_video_regression_quoted_web_error_does_not_route_hi_or_image(self):
        groq=self.groq();web=NS(search=AsyncMock())
        quoted='Quoted message: Web search is unavailable. No current facts verified. Question: '
        async for _ in answer_stream(groq,web,[{'role':'user','content':quoted+'Hi'}],routing_prompt='Hi'):pass
        with self.assertRaises(ImageRequested) as error:
            async for _ in answer_stream(groq,web,[{'role':'user','content':quoted+'Generate image of peacocks'}],routing_prompt='Generate image of peacocks'):pass
        self.assertEqual(error.exception.prompt,'Generate image of peacocks');web.search.assert_not_awaited()

    async def test_native_groq_browser_request(self):
        calls=[]
        def handle(request):
            calls.append(json.loads(request.content))
            return httpx.Response(200,json={'choices':[{'message':{'content':EVIDENCE,'reasoning':'private'}}]})
        client=GroqClient('fake',httpx.AsyncClient(transport=httpx.MockTransport(handle)))
        try:self.assertEqual(await client.browser_search('news'),EVIDENCE)
        finally:await client.close()
        self.assertEqual(calls[0]['tools'],[{'type':'browser_search'}]);self.assertEqual(calls[0]['tool_choice'],'required')
        self.assertFalse(calls[0]['stream'])

class FileAgentTests(unittest.IsolatedAsyncioTestCase):
    def bot_file(self,content):
        async def download(buffer):buffer.write(content)
        return NS(get_file=AsyncMock(return_value=NS(file_size=len(content),download_to_memory=download)))

    async def test_text_encoding_and_size_validation(self):
        doc={'file_id':'x','file_name':'data.txt'}
        self.assertEqual(await read_text_document(self.bot_file(b'\xef\xbb\xbfhello'),doc),'hello')
        for content in (b'\xff',b'',b'hello\x00world',b'x'*(MAX_TEXT_BYTES+1)):
            with self.assertRaises(ProviderError):await read_text_document(self.bot_file(content),doc)

    async def test_admin_agent_save_scope_restart_clear_and_permissions(self):
        u,c=fixture();u.message.reply_to_message=NS(document=Document('file','unique',file_name='agent.md'))
        c.args=[];c.bot.get_file=self.bot_file(b'Answer as a chemistry tutor.').get_file
        with tempfile.TemporaryDirectory() as d,patch.object(config,'DATA_DIR',Path(d)),patch.object(main,'ADMIN_ID','999'):
            await group_agent.agent_command(u,c);c.bot.get_file.assert_not_awaited()
            with patch.object(main,'ADMIN_ID','123'):
                await group_agent.agent_command(u,c)
                restarted=NS(bot_data={});group_agent.initialize(restarted)
                self.assertEqual(restarted.bot_data['external_system_prompt'],'Answer as a chemistry tutor.')
                self.assertEqual(group_agent.instructions(c,-1),'Answer as a chemistry tutor.')
                c.application.bot_data['global_settings']={'group_agent':False}
                self.assertEqual(group_agent.instructions(c,-100123),'')
                c.args=['clear'];await group_agent.agent_command(u,c)
                self.assertEqual(c.application.bot_data['external_system_prompt'],'')

    async def test_text_handler_and_ask_reply(self):
        u,c=fixture(False);u.message.caption='summarize';u.message.document=Document('f','u',file_name='a.txt')
        with patch.object(main,'launch_question',AsyncMock()) as launch:
            await main.text_document_command(u,c)
            self.assertEqual(launch.call_args.kwargs['document'],u.message.document)
            u.message.reply_to_message=NS(document=u.message.document,photo=[],text=None,caption=None)
            await main.ask_command(u,c)
            self.assertEqual(launch.call_args.kwargs['document'],u.message.document)

    async def test_web_switch_admin_only_and_persists(self):
        u,c=fixture(False);u.callback_query=NS(data='admin:web:toggle',answer=AsyncMock(),edit_message_text=AsyncMock())
        self.assertTrue(get_settings(c)['web'])
        with tempfile.TemporaryDirectory() as d,patch.object(config,'DATA_DIR',Path(d)),patch.object(main,'ADMIN_ID','999'):
            await main.admin_callback(u,c);self.assertTrue(get_settings(c)['web'])
            with patch.object(main,'ADMIN_ID','123'):await main.admin_callback(u,c)
            restarted=NS(bot_data={});initialize(restarted)
            self.assertFalse(restarted.bot_data['global_settings']['web'])

class GuestMediaCodeTests(unittest.IsolatedAsyncioTestCase):
    async def test_guest_mention_only_passes_reply_directly_no_asked(self):
        _,c=fixture();c.application.bot_data['global_settings']={'mode':'guest'}
        c.bot._post.return_value={'inline_message_id':'id'}
        update=Update.de_json({'update_id':1,'guest_message':{'message_id':7,'date':1700000000,
            'chat':{'id':-123,'type':'group','title':'Test'},'from':{'id':123,'first_name':'Tester','is_bot':False},
            'guest_query_id':'q','text':'@queryaibot','reply_to_message':{'message_id':6,'date':1700000000,
            'chat':{'id':-123,'type':'group','title':'Test'},'text':'Solve x + 2 = 5'}}},c.bot)
        with patch('guest_mode.user_data_cache',{'123':{}}),patch('guest_mode.start_inline',AsyncMock()):
            with self.assertRaises(ApplicationHandlerStop):await guest_update(update,c)
        item=next(iter(c.application.bot_data['inline_sessions'].values()))
        self.assertEqual(item['query'],'Solve x + 2 = 5');self.assertEqual(item['context'],'')
        self.assertNotIn('Asked:',str(c.bot._post.call_args))

    async def test_image_cache_never_uploads_to_requester(self):
        _,c=fixture();c.bot.send_photo=AsyncMock(return_value=NS(photo=[NS(file_id='fid')],message_id=3));c.bot.delete_message=AsyncMock()
        with patch.object(config,'IMAGE_CACHE_CHAT_ID','-1009'):
            self.assertEqual(await cache_images(c,123,[b'img']),['fid'])
        self.assertEqual(c.bot.send_photo.call_args.kwargs['chat_id'],'-1009')
        c.bot.delete_message.assert_awaited_once()
        with patch.object(config,'IMAGE_CACHE_CHAT_ID',''),patch.object(main,'LOG_CHANNEL_ID',''):
            with self.assertRaises(ProviderError):await cache_images(c,123,[b'img'])

    async def test_guest_images_are_embedded_and_charge_only_after_delivery(self):
        _,c=fixture();c.application.bot_data.update(global_settings={'mode':'guest'},groq=NS(),web=None)
        item={'query':'Generate image of peacocks','owner':123,'mode':'guest','pages':None,'running':True}
        with patch('inline_mode.generate_images',AsyncMock(return_value=[b'img'])),patch('inline_mode.cache_images',AsyncMock(return_value=['photo-id'])),patch('inline_mode.charge_request') as charge:
            await generate_inline(c,'guest-id','token',item,{'subscription':'inactive','dm_started':False})
            payload=c.bot._post.call_args.kwargs['data'];self.assertEqual(payload['inline_message_id'],'guest-id')
            self.assertEqual(payload['rich_message']['blocks'][0]['photo']['media'],'photo-id');charge.assert_called_once()
            charge.reset_mock();c.bot._post.side_effect=BadRequest('cannot edit')
            c.bot.edit_message_media=AsyncMock(side_effect=BadRequest('cannot send photo either'))
            await generate_inline(c,'guest-id','token',item,{'subscription':'inactive','dm_started':False})
            charge.assert_not_called()

    def test_code_copy_preserves_literal_text_in_one_complete_download(self):
        code='print("<tag> & `x` $x \\[x\\]")\n'+('hello😀'*100)
        saved=[]
        def download(number,content,lang):
            saved.append((number,content,lang));return 'https://t.me/queryaibot?start=code_test'
        result=normalize_math(with_code_copy('```python\n'+code+'\n```',download))
        attrs=re.findall(r'<tg-button type="copy_text" text="([^"]*)"',result)
        self.assertEqual(attrs,[])
        self.assertEqual(saved,[(1,code+'\n','python')])
        self.assertEqual(result.count('Download complete code'),1)
        self.assertIn(code,result)
        self.assertNotIn('Copy answer',result)

class BroadcastTests(unittest.IsolatedAsyncioTestCase):
    async def test_aliases_and_native_copy_preserves_entities_and_media(self):
        payload=parse_broadcast('/broadcast --user --group',True)
        self.assertEqual(payload['audience'],['user','group'])
        _,c=fixture();c.bot.copy_message=AsyncMock(return_value=NS(message_id=4))
        payload.update(source_chat=1,source_message=2)
        with tempfile.TemporaryDirectory() as d:
            manager=BroadcastManager(Path(d)/'b.db',c.bot)
            try:await manager.send(3,payload)
            finally:await manager.close()
        kwargs=c.bot.copy_message.call_args.kwargs
        self.assertEqual(kwargs['message_id'],2);self.assertNotIn('caption',kwargs);self.assertNotIn('parse_mode',kwargs)

    async def test_album_collection_copy_and_partial_not_retried(self):
        u,c=fixture();u.message.media_group_id='album'
        with patch.object(broadcast,'ADMIN_ID','123'):
            await collect_album(u,c);u.message.message_id=9;await collect_album(u,c)
        self.assertEqual(c.application.bot_data['broadcast_albums'][(-100123,'album')]['ids'],{8,9})
        c.bot.copy_messages=AsyncMock(return_value=[NS(message_id=10),NS(message_id=11)])
        payload=parse_broadcast('/broadcast --user',True);payload.update(source_chat=1,source_messages=[8,9])
        with tempfile.TemporaryDirectory() as d:
            manager=BroadcastManager(Path(d)/'b.db',c.bot)
            try:
                await manager.send(3,payload)
                c.bot.copy_messages.return_value=[NS(message_id=10)]
                with self.assertRaises(NetworkError):await manager.send(3,payload)
            finally:await manager.close()


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_txt_contents_enter_model_without_quota_charge(self):
        from chat_store import ChatStore
        u,c=fixture(False);calls=[]
        u.message.get_bot=lambda:c.bot
        async def stream(messages,**kwargs):calls.append(messages);yield 'Summary of file'
        c.application.bot_data.update(groq=NS(stream=stream),web=None)
        with tempfile.TemporaryDirectory() as d:
            store=ChatStore(Path(d)/'chat.db');c.application.bot_data['chat_store']=store
            try:
                with patch.object(main,'read_text_document',AsyncMock(return_value='Unique attachment text')),patch.object(main,'charge_request') as charge:
                    await main.generate_answer(u,c,{'subscription':'inactive'},'summarize',document={'file_id':'file'})
                self.assertIn('Unique attachment text',calls[0][-1]['content'])
                self.assertIn('summarize',calls[0][-1]['content']);charge.assert_not_called()
            finally:store.close()

    async def test_native_album_optional_button_failure_does_not_replay_delivery(self):
        _,c=fixture();c.bot.copy_messages=AsyncMock(return_value=[NS(message_id=1),NS(message_id=2)])
        c.bot.edit_message_reply_markup=AsyncMock(side_effect=BadRequest('button rejected'))
        payload=parse_broadcast('/broadcast --user --button "Open|https://example.org"',True)
        payload.update(source_chat=1,source_messages=[8,9])
        with tempfile.TemporaryDirectory() as d:
            manager=BroadcastManager(Path(d)/'campaigns.db',c.bot)
            try:result=await manager.send(3,payload)
            finally:await manager.close()
        self.assertEqual(result.message_id,1);c.bot.copy_messages.assert_awaited_once()

    def test_rich_code_buttons_survive_page_formatting(self):
        from rich_messages import rich_pages
        code='printf \"test\"\n'+'echo `date`\n'
        pages=list(rich_pages(with_code_copy('```bash\n'+code+'```')))
        combined=''.join(pages)
        text=html.unescape(re.search(r'<tg-button type="copy_text" text="([^"]*)"',combined)[1])
        self.assertEqual(text,code.rstrip('\n'))
