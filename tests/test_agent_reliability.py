import asyncio
import datetime as dt
import json
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
import httpx
import agent_engine, agent_scheduler, agent_ui, config
from agent_tools import memory_authorized, recover_artifacts
from provider import GroqClient, ProviderError, DecisionFormatError
from test_premium_agent import AgentTestBase

class AuthorizationTests(unittest.TestCase):
    def test_memory_operation_and_target_are_separate_from_file_saving(self):
        for operation in ('memory_save','memory_delete'):
            self.assertFalse(memory_authorized('Save result.txt containing hello',operation,'favorite_color'))
            self.assertFalse(memory_authorized('Save result.txt containing forget favorite color',operation,'favorite_color'))
        self.assertTrue(memory_authorized('agent remember my favorite color is blue','memory_save','favorite_color'))
        self.assertFalse(memory_authorized('agent remember my favorite color is blue','memory_delete','favorite_color'))
        self.assertTrue(memory_authorized('agent forget my favorite color','memory_delete','favorite_color'))
        self.assertFalse(memory_authorized('forget my birthday','memory_delete','favorite_color'))
        self.assertFalse(memory_authorized("do not forget my favorite color",'memory_delete','favorite_color'))
        self.assertFalse(memory_authorized('Print "forget favorite color"','memory_delete','favorite_color'))

    def test_agent_prefix_only(self):
        for text in ('agent do this','Agent: research site','agent, test code'):
            self.assertTrue(agent_engine.requested(text));self.assertNotEqual(agent_engine.task_text(text),text)
        for text in ('Hi','I like this agent','agency task','Explain agent design'):
            self.assertFalse(agent_engine.requested(text))

class ReliabilityTests(AgentTestBase):
    async def test_memory_file_request_cannot_delete_existing_fact(self):
        self.store.remember(123,'dm','favorite_color','blue')
        with self.assertRaises(ValueError):await self.runtime('Save result.txt containing hello').execute('memory_delete',{'name':'favorite_color'})
        self.assertEqual(self.store.memories(123,'dm'),{'favorite_color':'blue'})

    async def test_artifacts_recover_cancelled_run_using_real_tokens_and_owner_expiry(self):
        runtime=self.runtime('Make a file');runtime.run_id=self.store.begin(123,'dm',[{'title':'Make file'}])
        await runtime.execute('write_file',{'path':'result.txt','content':'hello'})
        receipt=await runtime.execute('export_file',{'path':'result.txt'})
        self.store.finish(runtime.run_id,'cancelled','fake model token')
        self.assertEqual(recover_artifacts(self.c,123,runtime.run_id),[('result.txt',receipt['download'])])
        self.assertEqual(recover_artifacts(self.c,999,runtime.run_id),[])
        self.c.args=[runtime.run_id]
        await agent_ui.status_command(self.u,self.c)
        self.assertIn(receipt['download'],self.u.effective_message.reply_text.call_args.args[0])
        with patch('agent_tools.time.time',return_value=time.time()+3601):self.assertEqual(recover_artifacts(self.c,123,runtime.run_id),[])
        self.c.application.bot_data['agent_artifacts']={}
        self.assertEqual(recover_artifacts(self.c,123,runtime.run_id),[])

    async def test_five_minute_cron_does_not_skip_next_slot(self):
        now=dt.datetime(2026,10,6,12,0,tzinfo=dt.timezone.utc).timestamp()
        result=agent_scheduler.create_reminder(self.store,123,'Read',cron='*/5 * * * *',tz='UTC',now=now)
        await agent_scheduler.tick(self.c.application,now+301)
        job=next(j for j in self.store.schedules(123) if j['id']==result['id'])
        self.assertEqual(job['next_run'],now+600)

    async def test_prefix_required_even_when_enabled(self):
        self.assertTrue(agent_engine.enabled(self.c,self.user,123,'agent test'))
        self.assertFalse(agent_engine.enabled(self.c,self.user,123,'Hello'))

class ProviderReliabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_planning_json_400_retries_bounded_but_credentials_not_retried(self):
        calls=[]
        def handler(request):
            calls.append(json.loads(request.content))
            return httpx.Response(400,json={'error':{'code':'json_validate_failed','failed_generation':'secret answer'}})
        client=GroqClient('fake',httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        with patch('provider.asyncio.sleep',AsyncMock()):
            with self.assertRaises(DecisionFormatError):await client.complete([{'role':'user','content':'Hi'}],json_mode=True)
        await client.close();self.assertEqual(len(calls),3)
        self.assertNotIn('secret answer',json.dumps(calls))
        calls=[]
        def credentials(request):calls.append(request);return httpx.Response(401,json={'error':{'code':'invalid_api_key'}})
        client=GroqClient('fake',httpx.AsyncClient(transport=httpx.MockTransport(credentials)))
        with self.assertRaisesRegex(ProviderError,'credentials') as raised:await client.complete([],json_mode=True)
        self.assertNotIsInstance(raised.exception,DecisionFormatError)
        await client.close();self.assertEqual(len(calls),1)
