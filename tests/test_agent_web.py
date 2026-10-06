import asyncio
import base64
import importlib.util
import json
import socket
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
import httpx
import config
from public_web import destination, connect_public, web_url
from agent_web import fetch_page
from agent_sandbox import Sandbox
from agent_tools import ToolRuntime
from provider import GroqClient, ProviderError
from test_premium_agent import AgentTestBase
import test_premium_agent as premium
import agent_engine

PUBLIC=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('93.184.216.34',443))]

class PublicDestinationTests(unittest.TestCase):
    def test_private_metadata_loopback_multicast_and_mixed_dns_are_blocked(self):
        for address in ('127.0.0.1','10.0.0.1','169.254.169.254','::1','fe80::1','224.0.0.1'):
            records=[(socket.AF_INET,socket.SOCK_STREAM,6,'',(address,443))]
            with patch('public_web.socket.getaddrinfo',return_value=records):
                with self.assertRaises(ValueError):destination('example.com',443)
        with patch('public_web.socket.getaddrinfo',return_value=PUBLIC+[(socket.AF_INET,socket.SOCK_STREAM,6,'',('10.0.0.1',443))]):
            with self.assertRaises(ValueError):destination('example.com',443)
        for url in ('file:///etc/passwd','http://user:secret@example.com','https://example.com:8080','http://example.com\nHost: x'):
            with self.assertRaises(ValueError):web_url(url)

    def test_dns_is_pinned_to_numeric_address_before_connect(self):
        connection=NS(settimeout=lambda t:None,connect=unittest.mock.Mock(),close=lambda:None)
        with patch('public_web.socket.getaddrinfo',return_value=PUBLIC) as lookup,patch('public_web.socket.socket',return_value=connection):
            self.assertIs(connect_public('example.com',443),connection)
            connection.connect.assert_called_once_with(('93.184.216.34',443))
            lookup.assert_called_once()

    def test_proxy_guard_copy_cannot_drift(self):
        root=Path(__file__).resolve().parents[1]
        self.assertEqual((root/'public_web.py').read_bytes(),(root/'sandbox/public_web.py').read_bytes())

    def test_fetch_follows_redirect_and_rechecks_destination_on_connection(self):
        def response(status,headers,body):
            return NS(status=status,getheader=lambda name,default=None:headers.get(name,default),read=lambda limit:body)
        replies=[response(302,{'Location':'https://example.com/live'},b''),
                 response(200,{'Content-Type':'text/html'},b'<h1>Live score 42/1</h1><script>hidden</script><a href="/match">match</a>')]
        connections=[]
        def factory(host,port,timeout):
            instance=NS(request=lambda *a,**k:instance._create_connection(None,timeout),getresponse=lambda:replies.pop(0),close=lambda:None)
            connections.append(instance);return instance
        with patch('agent_web.http.client.HTTPSConnection',side_effect=factory),patch('agent_web.connect_public') as connect:
            result=fetch_page('https://example.com')
        self.assertEqual(connect.call_count,2)
        self.assertEqual(result['url'],'https://example.com/live')
        self.assertIn('42/1',result['text']);self.assertNotIn('hidden',result['text'])
        self.assertIn('<script>',result['body']);self.assertIn('https://example.com/match',result['links'])

class WebToolTests(AgentTestBase):
    async def test_fetch_saves_live_body_for_ai_and_offline_code(self):
        runtime=ToolRuntime(self.c,123,self.user,'dm','Read site',{'web':True})
        result={'body':'<h1>Live 42</h1>','text':'Live 42','status':200,'url':'https://example.com','fetched_at':'now'}
        with patch('agent_web.fetch',AsyncMock(return_value=result)):
            receipt=await runtime.execute('fetch_url',{'url':'https://example.com','path':'live.html'})
        self.assertEqual(base64.b64decode(runtime.files['live.html']),b'<h1>Live 42</h1>')
        self.assertTrue(receipt['untrusted_content']);self.assertNotIn('body',receipt)
        read=await runtime.execute('read_file',{'path':'live.html'})
        self.assertIn('42',read['content'])

    async def test_global_web_off_blocks_fetch_and_network_code(self):
        runtime=ToolRuntime(self.c,123,self.user,'dm','Read site',{'web':False})
        with patch.object(config,'SANDBOX_ENABLED',True):
            self.assertNotIn('fetch_url',[s['function']['name'] for s in runtime.registry()])
            with self.assertRaisesRegex(ValueError,'disabled web'):
                await runtime.execute('python',{'code':'print(1)','network':True})

    async def test_network_argument_is_boolean_and_forwarded(self):
        runtime=ToolRuntime(self.c,123,self.user,'dm','Test',{'web':True})
        sandbox=NS(execute=AsyncMock(return_value={'exit_code':0,'stdout':'42','stderr':'','files':{}}))
        self.c.application.bot_data['sandbox']=sandbox
        with patch.object(config,'SANDBOX_ENABLED',True):
            with self.assertRaises(ValueError):await runtime.execute('python',{'code':'print(1)','network':'true'})
            await runtime.execute('python',{'code':'print(42)','network':True})
        sandbox.execute.assert_awaited_once_with('print(42)',{},'python',network=True)

    async def test_browser_receipt_and_generated_file_are_returned(self):
        runtime=ToolRuntime(self.c,123,self.user,'dm','Read page',{'web':True})
        html=base64.b64encode(b'<html>rendered 42</html>').decode()
        sandbox=NS(execute=AsyncMock(return_value={'exit_code':0,'stdout':json.dumps({'url':'https://example.com','status':200,'text':'rendered 42'}),'stderr':'','files':{'rendered.html':html}}))
        self.c.application.bot_data['sandbox']=sandbox
        with patch.object(config,'SANDBOX_ENABLED',True),patch.object(config,'SANDBOX_WEB_ENABLED',True):
            result=await runtime.execute('browse_url',{'url':'https://example.com','path':'rendered.html'})
        self.assertEqual(result['status'],200);self.assertTrue(result['untrusted_content'])
        self.assertIn('rendered.html',runtime.files)
        self.assertTrue(sandbox.execute.call_args.kwargs['network'])
        code=sandbox.execute.call_args.args[0]
        self.assertIn("URL='https://example.com'",code)
        self.assertIn("os.environ['HTTPS_PROXY']",code)

    async def test_nonzero_code_exit_is_explicit_failure(self):
        runtime=ToolRuntime(self.c,123,self.user,'dm','Test',{'web':True})
        self.c.application.bot_data['sandbox']=NS(execute=AsyncMock(return_value={'exit_code':1,'stdout':'','stderr':'bad parser','files':{}}))
        with patch.object(config,'SANDBOX_ENABLED',True):result=await runtime.execute('python',{'code':'raise ValueError()'})
        self.assertIn('error',result);self.assertEqual(result['stderr'],'bad parser')

class DecisionRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def run_decisions(self,replies,**kwargs):
        seen=[]
        def handler(request):
            seen.append(json.loads(request.content));status,body=replies.pop(0)
            return httpx.Response(status,json=body)
        client=GroqClient('fake',httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        try:
            with patch('provider.asyncio.sleep',AsyncMock()):result=await client.complete([{'role':'user','content':'remind me'}],**kwargs)
            return result,seen
        finally:await client.close()

    async def test_tool_use_failed_is_repaired_without_executing_failed_generation(self):
        result,seen=await self.run_decisions([(400,{'error':{'code':'tool_use_failed','failed_generation':'DO NOT EXECUTE'}}),
            (200,{'choices':[{'message':{'content':'Valid decision'}}]})])
        self.assertEqual(result['content'],'Valid decision')
        self.assertEqual(len(seen),2);self.assertNotIn('DO NOT EXECUTE',json.dumps(seen[1]))

    async def test_empty_decision_and_bad_json_retry(self):
        result,seen=await self.run_decisions([(200,{'choices':[{'message':{'content':None}}]}),
            (200,{'choices':[{'message':{'content':'bad json'}}]}),
            (200,{'choices':[{'message':{'content':'{"steps":[]}'}}]})],json_mode=True)
        self.assertEqual(len(seen),3);self.assertEqual(json.loads(result['content']),{'steps':[]})

    async def test_truncated_decision_has_larger_budget_and_lower_reasoning(self):
        _,seen=await self.run_decisions([(200,{'choices':[{'finish_reason':'length','message':{'content':'incomplete'}}]}),
            (200,{'choices':[{'message':{'content':'complete'}}]})])
        self.assertEqual(seen[1]['max_completion_tokens'],8192)
        self.assertEqual(seen[1]['reasoning_effort'],'low')

    async def test_empty_and_interrupted_final_stream_retry_before_any_output(self):
        count=0
        def handler(request):
            nonlocal count
            count+=1
            if count==1:data='data: [DONE]\n\n'
            elif count==2:data='data: {"error":{"message":"interrupted"}}\n\n'
            else:data='data: {"choices":[{"delta":{"content":"Recovered"}}]}\n\ndata: [DONE]\n\n'
            return httpx.Response(200,text=data)
        client=GroqClient('fake',httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        try:
            with patch('provider.asyncio.sleep',AsyncMock()):output=''.join([x async for x in client.stream([{'role':'user','content':'Hi'}])])
        finally:await client.close()
        self.assertEqual(output,'Recovered');self.assertEqual(count,3)

class AgentFallbackTests(AgentTestBase):
    async def test_clarification_does_not_change_authorization_and_failed_review_keeps_file(self):
        decisions=[{'content':json.dumps({'normalized_request':'Create a verified report','assumptions':['Use a text file'],'steps':[{'title':'Create report'}]})},
            premium.PlannerTests.tool(self,'write_file',{'path':'report.txt','content':'42'},'a'),
            premium.PlannerTests.tool(self,'export_file',{'path':'report.txt'},'b'),{'content':'Created report'}]
        async def stream(*args,**kwargs):
            raise ProviderError('The assistant returned an empty answer.')
            yield ''
        self.c.application.bot_data['groq']=NS(complete=AsyncMock(side_effect=decisions),stream=stream)
        state={'request':'make rpt'}
        output=''.join([x async for x in agent_engine.agent_stream(self.c,self.user,123,'dm',[{'role':'user','content':'make rpt'}],{'web':True,'reasoning':'low'},AsyncMock(),state)])
        self.assertEqual(state['runtime'].request,'make rpt')
        self.assertEqual(state['normalized_request'],'Create a verified report')
        self.assertIn('Download report',output);self.assertIn('AI review could not finish',output)
        self.assertEqual(self.store.last_run(123)['status'],'partial')

    async def test_invalid_plan_falls_back_and_resource_catalog_is_authoritative(self):
        seen=[]
        async def complete(messages,**kwargs):
            seen.append(messages)
            return {'content':'{"steps":null}'} if len(seen)==1 else {'content':'No installed skills.'}
        async def stream(messages,**kwargs):yield 'No installed skills.'
        self.c.application.bot_data['groq']=NS(complete=complete,stream=stream)
        state={}
        output=''.join([x async for x in agent_engine.agent_stream(self.c,self.user,123,'dm',[{'role':'user','content':'Any installed skills?'}],{'web':True,'reasoning':'low'},AsyncMock(),state)])
        self.assertIn('No installed skills',output)
        self.assertIn('Installed resource catalog (only these exist): []',seen[1][0]['content'])

class NetworkLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_offline_default_and_proxy_worker_has_no_direct_bridge_or_host_mounts(self):
        sandbox=Sandbox()
        self.assertIn('--network=none',sandbox.command('qtfixed-test'))
        command=sandbox.command('qtfixed-test','qtfixed-test-net','http://qtfixed-proxy:8080')
        self.assertIn('--network=qtfixed-test-net',command)
        self.assertIn('--env=HTTPS_PROXY=http://qtfixed-proxy:8080',command)
        self.assertNotIn('--network=bridge',command);self.assertNotIn('-v',command)
        sandbox.control=AsyncMock()
        await sandbox.prepare_web('qtfixed-test-net','qtfixed-test-proxy')
        args=[call.args for call in sandbox.control.call_args_list]
        self.assertIn('--internal',args[0]);self.assertIn('com.docker.network.bridge.inhibit_ipv4=true',args[0])
        self.assertIn('/web_proxy.py',args[1]);self.assertIn('--network=bridge',args[1])

    async def test_web_setup_failure_cleans_up_proxy_and_network(self):
        sandbox=Sandbox();sandbox.check_isolation=AsyncMock();sandbox.prepare_web=AsyncMock(side_effect=ValueError('setup failed'))
        sandbox.control=AsyncMock()
        cleanup=NS(wait=AsyncMock(return_value=0))
        with patch.object(config,'SANDBOX_ENABLED',True),patch.object(config,'SANDBOX_WEB_ENABLED',True),patch('agent_sandbox.shutil.which',return_value='/usr/bin/docker'),patch('agent_sandbox.asyncio.create_subprocess_exec',AsyncMock(return_value=cleanup)):
            with self.assertRaisesRegex(ValueError,'setup failed'):await sandbox.execute('print(1)',{},network=True)
        self.assertEqual(sandbox.control.await_count,2)
        self.assertEqual(sandbox.control.call_args_list[0].args[:2],('rm','-f'))
        self.assertEqual(sandbox.control.call_args_list[1].args[:2],('network','rm'))
