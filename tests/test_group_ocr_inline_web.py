import asyncio
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
import httpx
from telegram import Chat,User,Message
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import main,primo,config
from provider import GroqClient,WebSearchRequested
from web_search import FeloClient,decode_event
from answer_engine import answer_stream
from math_format import readable_math,render_equation
from ocr import extract_text,recognize
from inline_mode import inline_query,inline_callback,start_inline


def fixture(group=True):
    chat=Chat(-100123,'supergroup',title='Test group') if group else Chat(123,'private')
    person=User(123,'Tester',False)
    msg=NS(chat=chat,chat_id=chat.id,photo=[],document=None,reply_to_message=None,sender_chat=None,
           from_user=person,new_chat_members=[],message_thread_id=None,text='/ask hi',message_id=8,
           reply_text=AsyncMock(),reply_photo=AsyncMock(),reply_document=AsyncMock())
    update=NS(effective_message=msg,message=msg,effective_chat=chat,effective_user=person,callback_query=None)
    bot=NS(_post=AsyncMock(),id=42,username='queryaibot',send_message=AsyncMock(),get_chat_member=AsyncMock(return_value=NS(status='administrator')),
           edit_message_text=AsyncMock())
    ctx=NS(bot=bot,args=['hi'],application=NS(bot_data={'active_requests':{}}))
    return update,ctx


class GroupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.groups={}
        def load():return copy.deepcopy(self.groups)
        def save(groups):self.groups.clear();self.groups.update(copy.deepcopy(groups))
        self.patches=[patch.object(primo,'load_group_data',load),patch.object(primo,'save_group_data',save),
                      patch.object(main,'load_group_data',load),patch.object(main,'save_group_data',save)]
        for p in self.patches:p.start()
    def tearDown(self):
        for p in self.patches:p.stop()

    async def test_allow_group_real_chat_without_invite_link(self):
        u,c=fixture()
        self.assertFalse(hasattr(u.effective_chat,'invite_link'))
        await primo.allow_group(u,c)
        self.assertTrue(self.groups[str(u.effective_chat.id)]['is_allowed'])
        self.assertIn('enabled',u.message.reply_text.call_args.args[0])

    async def test_join_auto_saves_for_broadcast_and_welcomes_once(self):
        u,c=fixture();u.message.new_chat_members=[NS(id=42)]
        await primo.handle_group_addition(u,c)
        await primo.handle_group_addition(u,c)
        c.bot.send_message.assert_awaited_once()
        self.assertTrue(self.groups[str(u.effective_chat.id)]['is_member'])
        self.assertTrue(self.groups[str(u.effective_chat.id)]['is_allowed'])

    async def test_my_chat_member_join_and_remove(self):
        u,c=fixture()
        change=NS(chat=u.effective_chat,old_chat_member=NS(status='left'),new_chat_member=NS(status='administrator'))
        await primo.bot_membership_changed(NS(my_chat_member=change),c)
        self.assertTrue(self.groups[str(u.effective_chat.id)]['is_member'])
        change.old_chat_member,change.new_chat_member=change.new_chat_member,change.old_chat_member
        await primo.bot_membership_changed(NS(my_chat_member=change),c)
        self.assertFalse(self.groups[str(u.effective_chat.id)]['is_member'])

    async def test_unknown_group_question_auto_registers(self):
        u,c=fixture();user={'request_count':0,'subscription':'inactive'}
        with patch.object(main,'user_data_cache',{'123':user}),patch.object(main,'check_channel_membership',AsyncMock(return_value=True)):
            self.assertIs(await main.eligible_user(u,c),user)
        self.assertTrue(self.groups[str(u.effective_chat.id)]['is_allowed'])

    async def test_disabled_group_gets_explanation(self):
        u,c=fixture();self.groups[str(u.effective_chat.id)]={'is_allowed':False}
        self.assertIsNone(await main.eligible_user(u,c))
        self.assertIn('/allowgroup',u.message.reply_text.call_args.args[0])

    async def test_group_ask_routes_to_generation(self):
        u,c=fixture()
        with patch.object(main,'launch_question',AsyncMock()) as launch:
            await main.ask_command(u,c)
        launch.assert_awaited_once_with(u,c,prompt='hi',photo=False)

    async def test_forty_question_boundary(self):
        u,c=fixture(False)
        for count,allowed in [(20,True),(39,True),(40,False)]:
            user={'request_count':count,'subscription':'inactive','quota_started_at':'2099-01-01T00:00:00+00:00'}
            with patch.object(main,'user_data_cache',{'123':user}),patch.object(main,'check_channel_membership',AsyncMock(return_value=True)):
                self.assertEqual(await main.eligible_user(u,c) is not None,allowed)

    def test_copy_answer_button_removed(self):
        keyboard=main.answer_keyboard(123,'short').to_dict()
        self.assertNotIn('copy_text',str(keyboard));self.assertNotIn('Copy answer',str(keyboard))


class WebMathTests(unittest.IsolatedAsyncioTestCase):
    def test_math_render_and_readable_fraction(self):
        output=readable_math(r'Use \(\frac{1}{z-1}\). Code: `a_$b`')
        self.assertIn('(1)/(z-1)',output)
        self.assertIn('`a_$b`',output)
        self.assertTrue(render_equation(r'\frac{1}{z-1}').startswith(b'\x89PNG'))

    def test_extract_original_ocr_responses(self):
        self.assertEqual(extract_text({'data':{'text':'solve x + 2'}}),'solve x + 2')
        self.assertEqual(extract_text('plain text'),'plain text')

    async def test_ocr_remote_failure_uses_local(self):
        from PIL import Image
        from io import BytesIO
        image=BytesIO();Image.new('RGB',(20,20),'white').save(image,'PNG')
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(503)))
        with patch('ocr.local_ocr',AsyncMock(return_value='x+2=5')) as local,patch.object(config,'OCR_MODE','auto'):
            self.assertEqual(await recognize(image.getvalue(),client),'x+2=5')
            local.assert_awaited_once()
        await client.aclose()

    async def test_current_query_routes_to_web(self):
        web=NS(search=AsyncMock(return_value='Verified weather evidence with current temperature and source.'))
        async def synthesize(messages,**kwargs):
            self.assertEqual(messages[-1]['role'],'tool');yield 'Explained result'
        groq=NS(stream=synthesize)
        result=''.join([s async for s in answer_stream(groq,web,[{'role':'user','content':'weather today'}])])
        self.assertEqual(result,'Explained result');web.search.assert_awaited_once()

    async def test_model_web_tool_call_dispatch(self):
        event={'choices':[{'delta':{'tool_calls':[{'index':0,'function':{'name':'web_search','arguments':'{"query":"latest moon launch"}'}}]},'finish_reason':'tool_calls'}]}
        def handle(r):
            body=json.loads(r.content)
            output=event if body.get('tools') else {'choices':[{'delta':{'content':'Web answer explained'},'finish_reason':'stop'}]}
            return httpx.Response(200,text='data: '+json.dumps(output)+'\n\ndata: [DONE]\n\n')
        transport=httpx.MockTransport(handle)
        client=GroqClient('fake',httpx.AsyncClient(transport=transport))
        web=NS(search=AsyncMock(return_value='Current agency leadership evidence including sources and dates.'))
        result=''.join([s async for s in answer_stream(client,web,[{'role':'user','content':'Who leads the agency?'}])])
        self.assertIn('Web answer',result);web.search.assert_awaited_once_with('latest moon launch')
        await client.close()

    async def test_felo_nested_sse_and_completion(self):
        event=json.dumps({'content':json.dumps({'data':{'type':'answer','data':{'text':'Fresh answer'}}})})
        def respond(request):
            if request.method=='POST':return httpx.Response(200,json={'stream_key':'abc'})
            return httpx.Response(200,text='data: '+event+'\n\ndata: {"status":"completed"}\n\n')
        web=FeloClient(httpx.AsyncClient(transport=httpx.MockTransport(respond)))
        self.assertEqual(await web.search('query'),'Fresh answer');await web.close()

    async def test_felo_reconnect_does_not_duplicate_prefix(self):
        def event(text):return 'data: '+json.dumps({'content':json.dumps({'data':{'type':'answer','data':{'text':text}}})})+'\n\n'
        count=0
        def respond(request):
            nonlocal count
            if request.method=='POST':return httpx.Response(200,json={'stream_key':'abc'})
            count+=1
            return httpx.Response(200,text=event('A')+(event('B')+'data: {"status":"completed"}\n\n' if count>1 else ''))
        web=FeloClient(httpx.AsyncClient(transport=httpx.MockTransport(respond)))
        with patch('web_search.asyncio.sleep',AsyncMock()):
            self.assertEqual(await web.search('query'),'AB')
        await web.close()


class InlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_inline_query_returns_fast_selection_result(self):
        u,c=fixture(False);q=NS(query='hello',from_user=NS(id=123,is_bot=False),answer=AsyncMock())
        await inline_query(NS(inline_query=q),c)
        self.assertEqual(len(q.answer.call_args.args[0]),1)
        self.assertEqual(len(c.application.bot_data['active_requests']),0)

    async def test_inline_foreign_owner_denied(self):
        u,c=fixture(False);c.application.bot_data['inline_sessions']={'a':{'owner':1,'created':__import__('time').monotonic()}}
        q=NS(data='inl:run:a',from_user=NS(id=2),answer=AsyncMock())
        await inline_callback(NS(callback_query=q),c)
        self.assertTrue(q.answer.call_args.kwargs['show_alert'])

    async def test_inline_quota_and_no_private_history(self):
        u,c=fixture(False)
        from inline_mode import generate_inline
        from chat_store import ChatStore
        calls=[]
        async def stream(messages,**kwargs):
            calls.append(messages);yield 'Answer'
        c.application.bot_data.update(groq=NS(stream=stream),web=None)
        item={'query':'hello','owner':123,'pages':None,'running':True}
        with patch('inline_mode.charge_request') as charge:
            await generate_inline(c,'inline-id','token',item,{'request_count':0,'subscription':'inactive'})
        charge.assert_called_once()
        self.assertEqual([m['role'] for m in calls[0]],['system','user'])
