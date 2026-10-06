import base64
from io import BytesIO
import json
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
import unittest
import httpx
from PIL import Image
from agent_store import AgentStore
from provider import GroqClient,DecisionFormatError,ProviderError
from agent_tools import spec
from skill_bundles import parse_bundle
import agent_jobs,agent_engine,agent_ui,agent_media,config,main
from test_premium_agent import AgentTestBase

def b64(raw):return base64.b64encode(raw).decode()

class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_tool_failure_uses_validated_envelope_without_replaying_tools(self):
        payloads=[]
        def handler(request):
            body=json.loads(request.content);payloads.append(body)
            if 'tools' in body:return httpx.Response(400,json={'error':{'code':'tool_use_failed','failed_generation':'private garbage'}})
            return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps({'tool':{'name':'calculator','arguments':{'expression':'2+3'}}})}}]})
        groq=GroqClient('fake',httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        messages=[{'role':'user','content':'calculate'}, {'role':'assistant','content':None,'tool_calls':[{'id':'old','function':{'name':'memory_save','arguments':'{}'}}]}, {'role':'tool','tool_call_id':'old','content':'{"saved":true}'}]
        with patch('provider.asyncio.sleep',AsyncMock()):
            answer=await groq.complete(messages,tools=[spec('calculator','Calculate',{'expression':'string'},['expression'])],tool_choice='required')
        self.assertEqual(len(payloads),4)
        self.assertEqual(json.loads(answer['tool_calls'][0]['function']['arguments']),{'expression':'2+3'})
        self.assertNotIn('private garbage',json.dumps(payloads))
        self.assertFalse(any(m['role']=='tool' for m in payloads[-1]['messages']))
        await groq.close()

    async def test_invalid_envelope_cannot_introduce_tools_or_wrong_arguments(self):
        def handler(request):
            body=json.loads(request.content)
            if 'tools' in body:return httpx.Response(400,json={'error':{'code':'tool_use_failed'}})
            return httpx.Response(200,json={'choices':[{'message':{'content':'{"tool":{"name":"memory_delete","arguments":{"name":"other"}}}'}}]})
        groq=GroqClient('fake',httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        with patch('provider.asyncio.sleep',AsyncMock()):
            with self.assertRaises(DecisionFormatError):await groq.complete([],tools=[spec('calculator','Calculate',{'expression':'string'},['expression'])])
        await groq.close()

    async def test_active_models_and_nonchat_probe_does_not_change_model(self):
        def handler(request):
            if request.method=='GET':return httpx.Response(200,json={'data':[{'id':'chat','active':True},{'id':'old','active':False},{'id':'audio'}]})
            body=json.loads(request.content)
            if body['model']=='audio':return httpx.Response(400,json={'error':{}})
            return httpx.Response(200,json={'choices':[{'message':{'tool_calls':[{'function':{'name':'ready','arguments':'{}'}}]}}]})
        groq=GroqClient('fake',httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        original=groq.model
        self.assertEqual(await groq.list_models(),['audio','chat'])
        await groq.probe_model('chat')
        with self.assertRaises(ProviderError):await groq.probe_model('audio')
        self.assertEqual(groq.model,original);await groq.close()

class WorkflowTests(AgentTestBase):
    async def test_agent_modes_persist_and_premium_required(self):
        self.c.args=['on'];await agent_ui.agent_command(self.u,self.c)
        self.assertTrue(agent_engine.enabled(self.c,self.user,123,'ordinary message'))
        self.c.args=['auto'];await agent_ui.agent_command(self.u,self.c)
        self.assertFalse(agent_engine.enabled(self.c,self.user,123,'ordinary message'))
        self.assertTrue(agent_engine.enabled(self.c,self.user,123,'Agent task'))
        self.c.args=['off'];await agent_ui.agent_command(self.u,self.c)
        self.assertFalse(agent_engine.enabled(self.c,self.user,123,'Agent task'))
        self.user['subscription']='inactive';self.c.args=['on'];await agent_ui.agent_command(self.u,self.c)
        self.assertFalse(self.store.prefs(123)['enabled'])

    async def test_owner_default_inherited_then_personal_override(self):
        owner_bundle=parse_bundle(b'Owner instructions','owner.txt','agent')
        self.store.install(999,'agent',owner_bundle);self.store.set_default(999,'agent',owner_bundle['name'])
        self.assertEqual(self.store.effective_bundles(123)[0]['instructions'],'Owner instructions')
        mine=parse_bundle(b'Personal instructions','mine.txt','agent');self.store.install(123,'agent',mine)
        self.assertEqual([b['instructions'] for b in self.store.effective_bundles(123)],['Personal instructions'])
        self.store.change_bundle(123,'agent','mine','remove');self.store.set_pref(123,inherit_defaults=False)
        self.assertEqual(self.store.effective_bundles(123),[])

    async def test_pdf_is_verified_exported_and_attached_not_link_only(self):
        runtime=self.runtime('agent create a PDF');await runtime.execute('write_file',{'path':'report.html','content':'<h1>Report</h1>'})
        execute=AsyncMock(return_value={'files':{'report.pdf':b64(b'%PDF-1.7\nreal fixture')},'exit_code':0,'stdout':json.dumps({'pdf_pages':1,'text_sample':'Report'}),'stderr':''})
        self.c.application.bot_data['sandbox']=NS(execute=execute)
        with patch.object(config,'SANDBOX_ENABLED',True):result=await runtime.execute('create_pdf',{'html_path':'report.html','path':'report.pdf'})
        self.assertEqual(result['queued_document'],'report.pdf');self.assertEqual(len(runtime.documents),1)
        await agent_media.deliver(self.u.message,runtime.documents)
        self.u.message.reply_document.assert_awaited_once();self.assertNotIn('data',runtime.documents[0])
        self.assertFalse(execute.call_args.kwargs['network'])

    async def test_missing_pdf_image_is_reported_instead_of_claimed_complete(self):
        runtime=self.runtime('agent PDF with image');await runtime.execute('write_file',{'path':'a.html','content':'<img src="missing.png">'})
        self.c.application.bot_data['sandbox']=NS(execute=AsyncMock(return_value={'files':{'a.pdf':b64(b'%PDF-1.7\nfixture')},'exit_code':0,'stdout':json.dumps({'pdf_pages':1,'images':[{'src':'missing.png','loaded':False}]}),'stderr':''}))
        with patch.object(config,'SANDBOX_ENABLED',True):receipt=await runtime.execute('create_pdf',{'html_path':'a.html','path':'a.pdf'})
        self.assertIn('embedded images failed',receipt['error'])

    async def test_repaired_export_replaces_stale_queued_bytes(self):
        runtime=self.runtime('agent make file')
        await runtime.execute('write_file',{'path':'result.txt','content':'old'})
        await runtime.execute('export_file',{'path':'result.txt'})
        await runtime.execute('write_file',{'path':'result.txt','content':'fixed'})
        await runtime.execute('export_file',{'path':'result.txt'})
        self.assertEqual(len(runtime.documents),1)
        self.assertEqual(runtime.documents[0]['data'],b'fixed')

    async def test_browser_failure_exposes_error_and_preserves_outputs(self):
        runtime=self.runtime('agent test calculator');await runtime.execute('write_file',{'path':'calc.html','content':'broken'})
        execute=AsyncMock(return_value={'files':{'shot.png':b64(b'actual fixture')},'exit_code':0,'stdout':json.dumps({'page_errors':['missing THREE'],'checks':[],'screenshot':'shot.png'}),'stderr':''})
        self.c.application.bot_data['sandbox']=NS(execute=execute)
        with patch.object(config,'SANDBOX_ENABLED',True):receipt=await runtime.execute('inspect_page',{'path':'calc.html','screenshot_path':'shot.png','actions':'[]'})
        self.assertIn('error',receipt);self.assertIn('shot.png',runtime.files)
        self.assertIn('--use-angle=swiftshader',execute.call_args.args[0])

    async def test_video_and_source_search_respect_web_switch(self):
        runtime=self.runtime('agent download video')
        with patch.object(config,'SANDBOX_ENABLED',True),patch.object(config,'SANDBOX_WEB_ENABLED',True):
            with self.assertRaises(ValueError):await runtime.execute('download_video',{'url':'https://youtube.com/watch?v=test','path':'clip.mp4'})
            with self.assertRaises(ValueError):await runtime.execute('search_images',{'query':'Salman Khan'})
        self.assertIn("'pinterest.com'",agent_jobs.image_search_job('Salman Khan','pinterest.com'))
        self.assertIn("'js_runtimes':{'node':{}}",agent_jobs.video_job('https://youtube.com/shorts/test','clip.mp4'))

    async def test_review_recovers_nonstream_without_replaying_completed_action(self):
        answers=[{'content':'{"steps":[{"title":"Calculate"}]}'},
            {'role':'assistant','tool_calls':[{'id':'a','function':{'name':'calculator','arguments':'{"expression":"2+3"}'}}]},
            {'content':'5'},{'content':'Verified calculator result: 5'}]
        complete=AsyncMock(side_effect=answers)
        async def broken(*args,**kwargs):raise ProviderError('interrupted');yield ''
        self.c.application.bot_data['groq']=NS(complete=complete,stream=broken)
        state={};text=''.join([p async for p in agent_engine.agent_stream(self.c,self.user,123,'dm',[{'role':'user','content':'agent calculate 2+3'}],{'web':False,'reasoning':'low'},AsyncMock(),state)])
        self.assertEqual(state['runtime'].calls,1);self.assertIn('Verified calculator result',text);self.assertNotIn('review could not finish',text)

class MigrationTests(unittest.TestCase):
    def test_existing_preferences_migrate_without_changing_subscription_or_choice(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'store.db';db=sqlite3.connect(path)
            db.execute('CREATE TABLE preferences(owner INTEGER PRIMARY KEY,enabled INTEGER NOT NULL DEFAULT 0,timezone TEXT NOT NULL)');db.execute("INSERT INTO preferences VALUES (1,1,'UTC')");db.commit();db.close()
            store=AgentStore(path);self.assertEqual(store.prefs(1),{'enabled':1,'timezone':'UTC','always_on':0,'inherit_defaults':1})
            store.set_pref(1,always_on=True);store.close();store=AgentStore(path)
            self.assertTrue(store.prefs(1)['always_on']);store.close()
    def test_large_instructions_stored_in_full_and_code_builders_compile(self):
        text='Read carefully.\n'*5000
        self.assertEqual(parse_bundle(text.encode(),'large.txt','agent')['instructions'],text)
        for code in (agent_jobs.browser_job('file:///workspace/a.html','out.html',screenshot='a.png'),agent_jobs.browser_job('file:///workspace/a.html','a.pdf',pdf=True),agent_jobs.video_job('https://youtube.com/shorts/test','a.mp4'),agent_jobs.image_search_job('Salman Khan','pinterest.com')):compile(code,'generated','exec')
