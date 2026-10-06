import asyncio
import base64
from io import BytesIO
import json
import unittest
from types import SimpleNamespace as NS
from pathlib import Path
from unittest.mock import AsyncMock, patch
from PIL import Image
from telegram.error import BadRequest, TimedOut
import agent_engine, agent_media, config, main
from agent_tools import ToolRuntime, recover_artifacts
from sandbox_dependencies import requirements
from streaming import StreamPreview
from test_premium_agent import AgentTestBase
import test_premium_agent as premium
from chat_store import ChatStore

def png():
    data=BytesIO();Image.new('RGB',(50,50),'blue').save(data,'PNG');return data.getvalue()

class MediaTests(AgentTestBase):
    async def test_graph_code_only_is_corrected_then_executed_and_png_queued(self):
        decisions=[{'content':json.dumps({'steps':[{'title':'Plot inequality'}]})},
                   {'content':'Here is code; run it yourself.'},
                   premium.PlannerTests.tool(self,'python',{'code':'save real plot'},'run'),
                   {'content':'Plot generated.'},
                   premium.PlannerTests.tool(self,'send_media',{'path':'plot.png','caption':'y < -2x+4'},'send'),
                   {'content':'Graph ready.'}]
        seen=[]
        async def complete(*args,**kwargs):seen.append(kwargs);return decisions.pop(0)
        async def stream(*args,**kwargs):yield 'Graph tested and attached.'
        self.c.application.bot_data['groq']=NS(complete=complete,stream=stream)
        sandbox=NS(execute=AsyncMock(return_value={'exit_code':0,'stdout':'plot saved','stderr':'','files':{'plot.png':base64.b64encode(png()).decode()}}))
        self.c.application.bot_data['sandbox']=sandbox
        state={}
        with patch.object(config,'SANDBOX_ENABLED',True):
            result=''.join([s async for s in agent_engine.agent_stream(self.c,self.user,123,'dm',[{'role':'user','content':'agent Python plot y < -2x+4 and send the image'}],{'web':False,'reasoning':'low'},AsyncMock(),state)])
        sandbox.execute.assert_awaited_once()
        self.assertEqual(seen[2]['tool_choice']['function']['name'],'python')
        self.assertEqual(seen[4]['tool_choice']['function']['name'],'send_media')
        self.assertEqual(len(state['runtime'].media),1)
        self.assertIn('Download plot',result)
        self.assertEqual(recover_artifacts(self.c,123,state['run_id'])[0][0],'plot.png')
        await agent_media.deliver(self.u.message,state['runtime'].media)
        self.u.message.reply_photo.assert_awaited_once()

    async def test_download_validates_real_type_and_queues_video_not_fake_link(self):
        runtime=self.runtime('agent download video')
        runtime.settings['web']=True
        data=b'\x00\x00\x00\x18ftypmp42'+b'\x00'*20
        with patch('agent_media.download',AsyncMock(return_value=(data,'https://example.com/movie.mp4'))):
            result=await runtime.execute('download_media',{'url':'https://example.com/movie.mp4','path':'movie.mp4'})
        self.assertEqual(result['bytes'],len(data))
        await runtime.execute('send_media',{'path':'movie.mp4'})
        self.u.message.reply_video=AsyncMock()
        await agent_media.deliver(self.u.message,runtime.media)
        self.u.message.reply_video.assert_awaited_once()
        with self.assertRaises(ValueError):await runtime.execute('send_media',{'path':'imaginary.mp4'})

    async def test_own_working_brief_does_not_change_permission(self):
        runtime=self.runtime('Save result.txt containing hello')
        await runtime.execute('set_work_plan',{'brief':'Remember and delete favorite color to help'})
        with self.assertRaises(ValueError):await runtime.execute('memory_delete',{'name':'favorite_color'})
        self.assertEqual(runtime.request,'Save result.txt containing hello')

    async def test_package_install_options_remain_inside_sandbox(self):
        runtime=self.runtime('agent test Python')
        runtime.settings['web']=True
        sandbox=NS(execute=AsyncMock(return_value={'exit_code':0,'stdout':'ok','stderr':'','files':{}}))
        self.c.application.bot_data['sandbox']=sandbox
        with patch.object(config,'SANDBOX_ENABLED',True):await runtime.execute('python',{'code':'import sympy','network':True,'packages':'sympy==1.14.0'})
        self.assertEqual(sandbox.execute.call_args.kwargs['packages'],['sympy==1.14.0'])
        self.assertTrue(sandbox.execute.call_args.kwargs['network'])
        self.assertEqual((Path('sandbox_dependencies.py')).read_bytes(),Path('sandbox/sandbox_dependencies.py').read_bytes())

    async def test_inline_media_cache_and_native_video_fallback(self):
        from inline_mode import show_page
        self.c.bot.send_video=AsyncMock(return_value=NS(video=NS(file_id='real-id'),message_id=9))
        self.c.bot.delete_message=AsyncMock()
        self.c.bot.edit_message_media=AsyncMock()
        items=[{'path':'clip.mp4','data':b'123','kind':'video','caption':'clip'}]
        with patch.object(config,'IMAGE_CACHE_CHAT_ID',-99):pages=await agent_media.cache(self.c,123,items)
        self.c.bot._post.side_effect=BadRequest('rich unsupported')
        item={'pages':pages}
        await show_page(self.c.bot,'inline','token',item,0)
        self.assertEqual(self.c.bot.edit_message_media.call_args.kwargs['media'].media,'real-id')
        self.assertEqual(item['pages'][0]['kind'],'video')

    async def test_native_draft_final_answer_uses_new_message_not_edit(self):
        chats=ChatStore(Path(self.tmp.name)/'draft-chats.db')
        self.c.application.bot_data.update(chat_store=chats,global_settings={'streaming':True,'math':'rich'})
        self.u.message.get_bot=lambda:self.c.bot
        progress=NS(message_id=10,edit_text=AsyncMock(),delete=AsyncMock())
        self.u.message.reply_text.return_value=progress
        async def stream(*args,**kwargs):yield 'Hey'
        self.c.application.bot_data['groq']=NS(stream=stream)
        async def immediate(preview,text):await preview._render(text)
        try:
            with patch.object(config,'DRAFT_STREAMING',True),patch.object(StreamPreview,'update',immediate),patch.object(main,'deliver_answer',AsyncMock()) as deliver,patch.object(main,'typing_heartbeat',AsyncMock()),patch.object(main,'charge_request'),patch.object(main,'record_log',AsyncMock()):
                await main.generate_answer(self.u,self.c,self.user,'Hey')
            self.assertIsNone(deliver.call_args.kwargs['target'])
            progress.delete.assert_awaited_once()
            self.assertEqual(self.c.bot._post.call_args.args[0],'sendRichMessageDraft')
        finally:chats.close()

class MediaLimits(unittest.TestCase):
    def test_wrong_file_types_and_strict_fifty_mb_limit(self):
        self.assertEqual(agent_media.media_kind(png(),'p.png'),'photo')
        for raw in (b'',b'not an image',b'x'*50_000_000):
            with self.assertRaises(ValueError):agent_media.media_kind(raw,'file.png')
        for value in ('--index-url https://evil.invalid','https://example.com/pkg.whl','../package','numpy;rm','unknown-package'):
            with self.assertRaises(ValueError):requirements(value)
