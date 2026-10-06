import asyncio
import base64
import datetime as dt
import json
import stat
import tempfile
import time
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
import httpx
from telegram import Document
from telegram.error import BadRequest, TimedOut, RetryAfter

import main, config, primo, agent_ui, agent_engine, agent_scheduler
from agent_store import AgentStore
from agent_tools import ToolRuntime,calculate
from agent_sandbox import Sandbox
from agent_scheduler import create_reminder,next_cron
from skill_bundles import parse_bundle
from telegram_delivery import call_with_retry,describe_error,download_into
from provider import ProviderError,GroqClient
from inline_mode import generate_inline,start_inline
from chat_store import ChatStore
from test_group_ocr_inline_web import fixture

PREMIUM={'subscription':'active','sub_end':'2099-12-31','dm_started':True}
SKILL=b'---\nname: reviewer\ndescription: Review code and results carefully.\n---\nCheck the evidence before answering.'

class RetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_edit_timeout_retries_and_not_modified_means_delivered(self):
        call=AsyncMock(side_effect=[TimedOut(),BadRequest('Message is not modified')])
        with patch('telegram_delivery.asyncio.sleep',AsyncMock()):
            self.assertTrue(await call_with_retry('editMessageText',call))
        self.assertEqual(call.await_count,2)

    async def test_new_send_timeout_is_not_duplicated_but_connect_failure_retries(self):
        call=AsyncMock(side_effect=TimedOut())
        with self.assertRaises(TimedOut):await call_with_retry('sendMessage',call)
        call.assert_awaited_once()
        error=TimedOut();error.__cause__=httpx.ConnectTimeout('connect only')
        call=AsyncMock(side_effect=[error,{'message_id':7}])
        with patch('telegram_delivery.asyncio.sleep',AsyncMock()):
            self.assertEqual(await call_with_retry('sendMessage',call),{'message_id':7})

    async def test_retry_after_and_permanent_bad_request(self):
        call=AsyncMock(side_effect=[RetryAfter(1),True])
        with patch('telegram_delivery.asyncio.sleep',AsyncMock()):self.assertTrue(await call_with_retry('sendPhoto',call))
        call=AsyncMock(side_effect=BadRequest('chat not found'))
        with self.assertRaises(BadRequest):await call_with_retry('sendMessage',call)
        call.assert_awaited_once()

    async def test_file_download_retry_resets_partial_buffer(self):
        count=0
        async def read(buffer):
            nonlocal count
            count+=1;buffer.write(b'good')
            if count==1:raise TimedOut()
        buffer=BytesIO()
        with patch('telegram_delivery.asyncio.sleep',AsyncMock()):await download_into(NS(download_to_memory=read),buffer)
        self.assertEqual(buffer.getvalue(),b'good')

    def test_error_logging_redacts_credentials_and_urls(self):
        error=RuntimeError('https://api.telegram.org/bot123456:secret/path token gsk_'+('A'*40))
        text=describe_error(error)
        self.assertNotIn('secret',text);self.assertNotIn('gsk_',text);self.assertIn('RuntimeError',text)

class BundleTests(unittest.TestCase):
    def archive(self,entries):
        out=BytesIO()
        with zipfile.ZipFile(out,'w') as z:
            for key,value in entries:z.writestr(key,value)
        return out.getvalue()
    def test_standard_skill_and_resource_archive(self):
        data=self.archive([('reviewer/SKILL.md',SKILL),('reviewer/scripts/check.py',b'print(42)')])
        bundle=parse_bundle(data,'reviewer.zip','skill')
        self.assertEqual(bundle['name'],'reviewer')
        self.assertEqual(base64.b64decode(bundle['files']['scripts/check.py']),b'print(42)')
        agent=parse_bundle(b'Be a careful chemistry tutor.','chemistry.txt','agent')
        self.assertEqual(agent['name'],'chemistry')
    def test_reject_traversal_symlink_bomb_and_bad_metadata(self):
        link=zipfile.ZipInfo('link');link.external_attr=(stat.S_IFLNK|0o777)<<16
        invalid=[self.archive([('../SKILL.md',SKILL)]),self.archive([('SKILL.md',SKILL),(link,b'/etc/passwd')]),
                 self.archive([('SKILL.md',SKILL),('large',b'x'*600000)]),b'No metadata']
        for data in invalid:
            with self.assertRaises((ValueError,zipfile.BadZipFile)):parse_bundle(data,'x.zip','skill')

class AgentTestBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=AgentStore(Path(self.tmp.name)/'agents.db')
        self.u,self.c=fixture(False);self.user=dict(PREMIUM)
        self.c.application.bot=self.c.bot
        self.c.application.bot_data.update(agent_store=self.store,sandbox=Sandbox(),global_settings={'streaming':False})
        self.store.set_pref(123,enabled=True)
        self.users=patch.object(primo,'user_data_cache',{'123':self.user});self.users.start()
    async def asyncTearDown(self):
        tasks=list(self.c.application.bot_data.get('active_requests',{}).values())
        for task in tasks:task.cancel()
        if tasks:await asyncio.gather(*tasks,return_exceptions=True)
        self.users.stop();self.store.close();self.tmp.cleanup()
    def runtime(self,request='Remember my preference and remind me tomorrow'):
        return ToolRuntime(self.c,123,self.user,'dm',request,{'web':False})

class PermissionTests(AgentTestBase):
    async def test_settings_are_personal_premium_only_and_no_old_agent_command(self):
        self.u.callback_query=NS(data='personal:toggle',from_user=self.u.effective_user,answer=AsyncMock(),edit_message_text=AsyncMock())
        await agent_ui.callback(self.u,self.c)
        self.assertFalse(self.store.prefs(123)['enabled']);self.assertFalse(self.store.prefs(999)['enabled'])
        self.user.update(subscription='inactive')
        await agent_ui.callback(self.u,self.c)
        self.assertFalse(self.store.prefs(123)['enabled'])
        self.assertTrue(self.u.callback_query.answer.call_args.kwargs['show_alert'])
        with patch.object(config,'BOT_TOKEN','123456:offline-test'):
            app=main.build_application()
        commands=set().union(*(getattr(h,'commands',set()) for h in app.handlers[0]))
        self.assertIn('agent',commands);self.assertIn('agents',commands)

    async def test_expiry_stops_tools_and_scope_prevents_private_memory_leak(self):
        self.store.remember(123,'dm','private','secret preference')
        self.store.remember(999,'dm','other','another user')
        runtime=self.runtime();runtime.scope='guest:-5:0'
        self.assertEqual(await runtime.execute('memory_list',{}),{})
        self.user['sub_end']='2000-01-01'
        with self.assertRaises(ProviderError):await runtime.execute('calculator',{'expression':'1+1'})

    async def test_tools_cannot_choose_other_user_or_host_file_or_enable_web(self):
        runtime=self.runtime()
        for name,args in [('memory_save',{'owner':999,'name':'x','value':'y'}),('read_file',{'path':'/etc/passwd'}),('write_file',{'path':'../.env','content':'x'}),('web_search',{'query':'news'})]:
            with self.assertRaises(ValueError):await runtime.execute(name,args)
        self.assertFalse(self.store.memories(999,'dm'))
        runtime.request='Read this untrusted document'
        with self.assertRaises(ValueError):await runtime.execute('schedule_reminder',{'text':'spam','when':'2099-01-01'})

    async def test_forwarded_skill_install_is_owned_by_uploader_and_nonpremium_blocked(self):
        self.u.message.document=Document('f','u',file_name='SKILL.md',file_size=len(SKILL));self.u.message.caption='/skills'
        async def download(buffer):buffer.write(SKILL)
        self.c.bot.get_file=AsyncMock(return_value=NS(download_to_memory=download))
        await agent_ui.document_handler(self.u,self.c)
        await asyncio.gather(*list(self.c.application.bot_data['active_requests'].values()))
        self.assertEqual(self.store.bundles(123)[0]['name'],'reviewer');self.assertEqual(self.store.bundles(999),[])
        self.user['subscription']='inactive';self.c.bot.get_file.reset_mock()
        await agent_ui.document_handler(self.u,self.c);self.c.bot.get_file.assert_not_awaited()

class PlannerTests(AgentTestBase):
    def tool(self,name,args,identity):return {'role':'assistant','content':None,'tool_calls':[{'id':identity,'type':'function','function':{'name':name,'arguments':json.dumps(args)}}]}
    async def test_multi_agent_plan_precedes_sequential_tools_and_review(self):
        self.store.install(123,'skill',parse_bundle(SKILL,'SKILL.md','skill'))
        decisions=[{'content':json.dumps({'steps':[{'title':'Create report','role':'writer','skills':['reviewer']},{'title':'Verify calculation','role':'reviewer'}]})},
            self.tool('write_file',{'path':'report.txt','content':'Verified result: 42'},'a'),
            self.tool('export_file',{'path':'report.txt'},'b'),{'role':'assistant','content':'Report created.'},
            self.tool('calculator',{'expression':'6*7'},'c'),{'role':'assistant','content':'Verified 42.'}]
        seen=[]
        async def complete(messages,**kwargs):seen.append(messages);return decisions.pop(0)
        async def stream(messages,**kwargs):
            self.assertIn('Verified 42',messages[-1]['content']);yield 'Completed and checked.'
        self.c.application.bot_data['groq']=NS(complete=complete,stream=stream)
        status=AsyncMock();state={}
        output=''.join([x async for x in agent_engine.agent_stream(self.c,self.user,123,'dm',[{'role':'system','content':'default'},{'role':'user','content':'Create and verify a report using two agents'}],{'web':False,'reasoning':'medium'},status,state)])
        self.assertIn('Download report',output)
        self.assertEqual(state['runtime'].calls,3)
        self.assertIn('Check the evidence',seen[1][0]['content'])
        statuses=[call.args[0] for call in status.call_args_list]
        self.assertIn('1. Create report',statuses[1]);self.assertIn('2. Verify calculation',statuses[1])
        self.assertEqual(self.store.last_run(123)['status'],'completed')
        self.assertEqual(base64.b64decode(state['runtime'].files['report.txt']),b'Verified result: 42')

    async def test_agent_dispatch_in_dm_group_inline_guest_and_opt_out(self):
        calls=[]
        async def fake(context,user,owner,scope,messages,settings,on_status,state):calls.append((owner,scope));yield 'Agent result'
        async def normal(*args,**kwargs):yield 'Normal result'
        self.c.application.bot_data.update(groq=NS(stream=normal),web=None)
        with patch.object(agent_engine,'agent_stream',fake),patch.object(main,'charge_request'),patch('inline_mode.charge_request'):
            for group in (False,True):
                u,c=fixture(group);u.message.get_bot=lambda:self.c.bot
                c.application.bot_data=self.c.application.bot_data
                chats=ChatStore(Path(self.tmp.name)/f'chats{group}.db');c.application.bot_data['chat_store']=chats
                try:await main.generate_answer(u,c,self.user,'agent Hello')
                finally:chats.close()
            for mode in ('inline','guest'):
                self.c.application.bot_data['global_settings']['mode']=mode
                item={'query':'agent Hi','owner':123,'mode':mode,'pages':None,'running':True,'agent_scope':'guest:-99:0' if mode=='guest' else 'inline'}
                await generate_inline(self.c,'id','token',item,self.user)
            self.assertEqual(len(calls),4)
            self.store.set_pref(123,enabled=False)
            self.assertFalse(agent_engine.enabled(self.c,self.user,123))

    async def test_provider_plan_decision_drops_hidden_reasoning(self):
        def handle(request):
            body=json.loads(request.content);self.assertFalse(body['include_reasoning'])
            return httpx.Response(200,json={'choices':[{'message':{'content':'{"steps":[]}','reasoning':'private hidden content'}}]})
        client=GroqClient('fake',httpx.AsyncClient(transport=httpx.MockTransport(handle)))
        try:result=await client.complete([{'role':'user','content':'plan'}],json_mode=True)
        finally:await client.close()
        self.assertNotIn('reasoning',result)

    async def test_tool_budget_stops_actions_and_review_reports_unfinished_work(self):
        plan={'content':json.dumps({'steps':[{'title':'First task'},{'title':'Second task'}]})}
        decisions=[plan,self.tool('calculator',{'expression':'1+1'},'a')]
        async def complete(*args,**kwargs):return decisions.pop(0)
        async def stream(messages,**kwargs):
            self.assertIn('unfinished',messages[-1]['content']);yield 'Only the first calculation ran.'
        self.c.application.bot_data['groq']=NS(complete=complete,stream=stream)
        state={}
        with patch.object(config,'AGENT_MAX_TOOL_CALLS',1):
            output=[x async for x in agent_engine.agent_stream(self.c,self.user,123,'dm',[{'role':'user','content':'Two calculations'}],{'web':False,'reasoning':'medium'},AsyncMock(),state)]
        self.assertEqual(state['runtime'].calls,1);self.assertEqual(self.store.last_run(123)['status'],'partial')

    async def test_cancel_after_plan_records_cancelled_run(self):
        self.c.application.bot_data['groq']=NS(complete=AsyncMock(return_value={'content':json.dumps({'steps':[{'title':'Work'}]})}))
        async def status(text):
            if text.startswith('📋'):raise asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            async for _ in agent_engine.agent_stream(self.c,self.user,123,'dm',[{'role':'user','content':'Task'}],{'web':False,'reasoning':'medium'},status,{}):pass
        self.assertEqual(self.store.last_run(123)['status'],'cancelled')

class SchedulerTests(AgentTestBase):
    async def test_one_shot_persists_and_sends_once(self):
        now=time.time();when=dt.datetime.fromtimestamp(now+60,dt.timezone.utc).isoformat()
        job=create_reminder(self.store,123,'Study',when=when,now=now)
        await agent_scheduler.tick(self.c.application,now+61)
        await agent_scheduler.tick(self.c.application,now+62)
        self.c.bot.send_message.assert_awaited_once()
        self.assertEqual(self.c.bot.send_message.call_args.kwargs['chat_id'],123)
        self.assertEqual(self.store.schedules(123),[])

    async def test_cron_timezone_owner_cancellation_and_expiry(self):
        now=dt.datetime(2026,10,6,0,0,tzinfo=dt.timezone.utc).timestamp()
        next_time=next_cron('30 18 * * *','Asia/Kolkata',now)
        self.assertEqual(dt.datetime.fromtimestamp(next_time,dt.timezone.utc).hour,13)
        with self.assertRaises(ValueError):next_cron('* * * * *','Asia/Kolkata',now)
        result=create_reminder(self.store,123,'Read',cron='30 18 * * *',now=now)
        self.assertFalse(self.store.cancel_schedule(999,result['id']))
        self.c.application.bot=self.c.bot;self.user['sub_end']='2000-01-01'
        await agent_scheduler.tick(self.c.application,next_time+1)
        self.assertEqual(self.store.schedules(123)[0]['status'],'paused');self.c.bot.send_message.assert_not_awaited()

    async def test_timeout_is_uncertain_not_automatically_resent(self):
        self.c.application.bot=self.c.bot;self.c.bot.send_message.side_effect=TimedOut()
        self.store.add_schedule(123,'Due',None,'Asia/Kolkata',0)
        await agent_scheduler.tick(self.c.application);await agent_scheduler.tick(self.c.application)
        self.c.bot.send_message.assert_awaited_once()
        self.assertEqual(self.store.schedules(123)[0]['status'],'uncertain')

    async def test_process_restart_marks_sending_job_uncertain(self):
        identity=self.store.add_schedule(123,'Due',None,'Asia/Kolkata',0);self.store.claim(identity)
        reopened=AgentStore(Path(self.tmp.name)/'agents.db')
        try:self.assertEqual(reopened.schedules(123)[0]['status'],'uncertain')
        finally:reopened.close()

class SandboxTests(unittest.IsolatedAsyncioTestCase):
    def test_container_flags_and_calculator_does_not_eval_code(self):
        command=Sandbox().command('qtfixed-test')
        for flag in ('--network=none','--read-only','--cap-drop=ALL','--security-opt=no-new-privileges','--user=65534:65534','--pids-limit=128'):self.assertIn(flag,command)
        self.assertNotIn('-v',command);self.assertNotIn('--privileged',command)
        self.assertEqual(calculate('6*7'),42)
        for expression in ('__import__("os").system("id")','2**99999'):
            with self.assertRaises(ValueError):calculate(expression)
    async def test_disabled_sandbox_never_starts_host_process(self):
        with patch.object(config,'SANDBOX_ENABLED',False),patch('agent_sandbox.asyncio.create_subprocess_exec',AsyncMock()) as spawn:
            with self.assertRaises(ValueError):await Sandbox().execute('print(1)',{})
            spawn.assert_not_awaited()

    async def test_rootful_without_runsc_and_missing_cgroup_limits_are_rejected(self):
        info={'SecurityOptions':['name=seccomp'],'MemoryLimit':True,'PidsLimit':True,'CpuCfsQuota':True}
        process=NS(returncode=0,communicate=AsyncMock(return_value=(json.dumps(info).encode(),b'')))
        with patch('agent_sandbox.asyncio.create_subprocess_exec',AsyncMock(return_value=process)),patch.object(config,'SANDBOX_RUNTIME',''):
            with self.assertRaisesRegex(ValueError,'rootless'):await Sandbox().check_isolation()
            info['SecurityOptions'].append('name=rootless');info['MemoryLimit']=False
            process.communicate.return_value=(json.dumps(info).encode(),b'')
            with self.assertRaisesRegex(ValueError,'resource limits'):await Sandbox().check_isolation()
            info['MemoryLimit']=True;process.communicate.return_value=(json.dumps(info).encode(),b'')
            await Sandbox().check_isolation()

class RecoveryTests(AgentTestBase):
    async def test_normal_final_answer_edits_existing_status_and_survives_close(self):
        self.store.set_pref(123,enabled=False)
        self.u.message.get_bot=lambda:self.c.bot
        status=NS(message_id=99,edit_text=AsyncMock(),delete=AsyncMock())
        self.u.message.reply_text.return_value=status
        async def stream(*args,**kwargs):yield 'Complete answer'
        self.c.application.bot_data.update(groq=NS(stream=stream),web=None)
        chats=ChatStore(Path(self.tmp.name)/'chats.db');self.c.application.bot_data['chat_store']=chats
        try:
            with patch.object(main,'charge_request'):await main.generate_answer(self.u,self.c,self.user,'Question')
            self.assertEqual(self.c.bot._post.call_args.args[0],'editMessageText')
            self.assertEqual(self.c.bot._post.call_args.kwargs['data']['message_id'],99)
            status.delete.assert_not_awaited()
            self.assertEqual(self.store.last_answer(123,main.history_key(self.u)),'Complete answer')
        finally:chats.close()

    async def test_inline_final_timeout_keeps_result_for_retry_without_model(self):
        self.c.application.bot_data['global_settings'].update(mode='guest',streaming=False)
        self.store.set_pref(123,enabled=False)
        calls=[]
        async def stream(*args,**kwargs):calls.append(1);yield 'Saved answer'
        self.c.application.bot_data.update(groq=NS(stream=stream),web=None)
        item={'query':'agent Hi','owner':123,'mode':'guest','pages':None,'running':True,'created':time.monotonic()}
        self.c.bot._post.side_effect=TimedOut()
        with patch('inline_mode.charge_request') as charge:
            await generate_inline(self.c,'id','token',item,self.user)
            self.assertTrue(item['pages']);charge.assert_not_called()
            self.c.bot._post.side_effect=None
            self.c.application.bot_data['inline_sessions']={'token':item}
            with patch('inline_mode.user_data_cache',{'123':self.user}):
                await start_inline(self.c,'id','token',123)
                await asyncio.gather(*list(self.c.application.bot_data['active_requests'].values()))
            charge.assert_called_once()
        self.assertEqual(len(calls),1);self.assertEqual(self.store.last_answer(123,'external-last'),'Saved answer')
