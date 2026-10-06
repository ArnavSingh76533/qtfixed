import asyncio
import json
import tempfile
import time
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image
from telegram import Document
from telegram.error import BadRequest, NetworkError
import main, config, code_downloads, ocr
from chat_store import ChatStore
from documents import read_text_document, prepare_document
from image_generation import GeneratedImage, deliver_images, image_intent
from inline_mode import generate_inline, start_inline, show_page
from provider import GroqClient, ProviderError
from rich_messages import with_code_copy, rich_pages, fallback_markdown
from runtime_settings import get_settings
from test_group_ocr_inline_web import fixture


def downloadable(content):
    async def download(buffer):buffer.write(content)
    return NS(file_size=len(content), download_to_memory=download)


class FileTests(unittest.IsolatedAsyncioTestCase):
    async def test_file_over_old_limit_reads_every_section_including_tail(self):
        data='a'*65000+' IMPORTANT_TAIL'
        _,c=fixture(False)
        c.bot.get_file=AsyncMock(return_value=downloadable(data.encode()))
        text=await read_text_document(c.bot,{'file_id':'file'})
        seen=[]
        async def stream(messages,**kwargs):
            seen.append(messages[-1]['content'])
            self.assertFalse(kwargs['allow_web']);self.assertFalse(kwargs['allow_image'])
            yield 'Tail is important' if 'IMPORTANT_TAIL' in messages[-1]['content'] else 'Section reviewed'
        result=await prepare_document(NS(stream=stream),text,'What is important?')
        self.assertGreater(len(seen),1)
        self.assertIn('IMPORTANT_TAIL',seen[-1]);self.assertIn('Tail is important',result)
        self.assertIn('condensed findings',result)

    async def test_document_bypasses_exhausted_quota_but_ordinary_question_does_not(self):
        u,c=fixture(False)
        user={'subscription':'inactive','request_count':40}
        with patch.object(main,'user_data_cache',{'123':user}),patch.object(main,'normalize_user'),patch.object(main,'check_channel_membership',AsyncMock(return_value=True)):
            self.assertIsNone(await main.eligible_user(u,c))
            self.assertIs(await main.eligible_user(u,c,quota_exempt=True),user)

    async def test_guest_file_at_quota_limit_runs_and_is_not_charged(self):
        _,c=fixture(False)
        seen=[]
        async def stream(messages,**kwargs):seen.append(messages);yield 'The uploaded file says hello.'
        c.application.bot_data.update(global_settings={'mode':'guest','streaming':False},groq=NS(stream=stream),web=None)
        c.bot.get_file=AsyncMock(return_value=downloadable(b'Hello from the file'))
        item={'query':'Summarize','owner':123,'mode':'guest','document':{'file_id':'file'},'created':time.monotonic(),'pages':None,'running':False}
        c.application.bot_data['inline_sessions']={'token':item}
        user={'subscription':'inactive','request_count':40}
        tasks=[]
        def submit(context,owner,key,work):
            task=asyncio.create_task(work());tasks.append(task);return task
        with patch('inline_mode.submit',submit),patch('inline_mode.user_data_cache',{'123':user}),patch('inline_mode.normalize_user'),patch.object(main,'check_channel_membership',AsyncMock(return_value=True)),patch('inline_mode.charge_request') as charge:
            await start_inline(c,'same-inline-id','token',123)
            await asyncio.gather(*tasks)
            charge.assert_not_called()
        self.assertIn('Hello from the file',seen[0][-1]['content'])
        self.assertEqual(c.bot._post.call_args.kwargs['data']['inline_message_id'],'same-inline-id')

    async def test_file_retry_remains_quota_free_and_error_has_no_quota_boilerplate(self):
        u,c=fixture(False);u.message.get_bot=lambda:c.bot
        async def stream(*args,**kwargs):yield 'Summary'
        c.application.bot_data.update(groq=NS(stream=stream),web=None)
        with tempfile.TemporaryDirectory() as d:
            store=ChatStore(Path(d)/'chats.db');c.application.bot_data['chat_store']=store
            try:
                with patch.object(main,'read_text_document',AsyncMock(return_value='File content')),patch.object(main,'charge_request') as charge:
                    await main.generate_answer(u,c,{},'Summarize',document={'file_id':'x'})
                    charge.assert_not_called()
                self.assertTrue(store.get(main.history_key(u))[1]['last_quota_exempt'])
                jobs=[]
                def submit(context,owner,key,work):jobs.append(work);return NS()
                with patch.object(main,'submit',submit),patch.object(main,'eligible_user',AsyncMock(return_value={})) as eligible,patch.object(main,'generate_answer',AsyncMock()) as answer:
                    await main.launch_question(u,c,retry=True)
                    await jobs[0]()
                    self.assertTrue(eligible.call_args.kwargs['quota_exempt'])
                    self.assertTrue(answer.call_args.kwargs['quota_exempt'])
                    self.assertEqual(answer.call_args.kwargs['routing_prompt'],'Summarize')
                with patch.object(main,'read_text_document',AsyncMock(side_effect=ProviderError('Invalid file'))):
                    await main.generate_answer(u,c,{},'',document={'file_id':'x'})
                self.assertEqual(u.message.reply_text.call_args.args[0],'Invalid file')
            finally:store.close()


class PromptTests(unittest.IsolatedAsyncioTestCase):


    async def test_legacy_custom_system_prompt_is_ignored_in_all_modes(self):
        seen=[]
        async def stream(messages,**kwargs):seen.append(messages[0]['content']);yield 'Answer'
        for group in (False,True):
            u,c=fixture(group);u.message.get_bot=lambda:c.bot
            c.application.bot_data.update(external_system_prompt='UNIQUE_CUSTOM_SYSTEM',groq=NS(stream=stream),web=None)
            with tempfile.TemporaryDirectory() as d:
                store=ChatStore(Path(d)/'chats.db');c.application.bot_data['chat_store']=store
                try:
                    with patch.object(main,'charge_request'):
                        await main.generate_answer(u,c,{},'hi')
                    self.assertNotIn('UNIQUE_CUSTOM_SYSTEM',seen[-1])
                    self.assertIn(config.SYSTEM_PROMPT,seen[-1])
                finally:store.close()
        for mode in ('guest','inline'):
            _,c=fixture(False)
            c.application.bot_data.update(external_system_prompt='UNIQUE_CUSTOM_SYSTEM',global_settings={'mode':mode,'streaming':False},groq=NS(stream=stream),web=None)
            item={'query':'Hi','owner':123,'mode':mode,'pages':None,'running':True}
            with patch('inline_mode.charge_request'):
                await generate_inline(c,'id','token',item,{})
            self.assertNotIn('UNIQUE_CUSTOM_SYSTEM',seen[-1])
            self.assertIn(config.SYSTEM_PROMPT,seen[-1])


class WholeCodeTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_program_one_download_exact_source_with_owner_and_expiry(self):
        u,c=fixture(False)
        code='print("hello 😀")\n'*1800
        result=code_downloads.format_answer(c,123,'```python\n'+code+'```')
        self.assertEqual(result.count('Download complete code'),1)
        self.assertNotIn('Copy code',result)
        self.assertEqual(len(list(rich_pages(result))),1)
        self.assertIn('https://t.me/queryaibot?start=code_',fallback_markdown(result))
        token,item=next(iter(c.application.bot_data['code_downloads'].items()))
        self.assertEqual(item['code'],code)
        c.args=['code_'+token];await code_downloads.start_with_code(u,c)
        self.assertEqual(u.message.reply_document.call_args.args[0].getvalue(),code.encode())
        self.assertEqual(u.message.reply_document.call_args.kwargs['filename'],'code_1.py')
        u.message.reply_document.reset_mock();item['owner']=999
        await code_downloads.start_with_code(u,c);u.message.reply_document.assert_not_awaited()
        item['created']-=3601
        await code_downloads.start_with_code(u,c)
        self.assertIn('expired',u.message.reply_text.call_args.args[0])

    def test_two_independent_short_commands_keep_two_complete_copy_buttons(self):
        text='Fast:\n```bash\nffmpeg -i in.mp4 -c copy out.mp4\n```\nAccurate:\n```bash\nffmpeg -i in.mp4 -c:v libx264 out.mp4\n```'
        answer=with_code_copy(text)
        self.assertEqual(answer.count('type="copy_text"'),2)
        self.assertNotIn('part',answer)


class VisionTests(unittest.IsolatedAsyncioTestCase):
    async def test_photo_pipeline_reviews_vision_with_main_chat_model(self):
        u,c=fixture(False);u.message.get_bot=lambda:c.bot
        u.message.photo=[NS(file_id='image',file_size=3)]
        c.bot.get_file=AsyncMock(return_value=downloadable(b'raw'))
        seen=[]
        async def stream(messages,**kwargs):seen.append(messages);yield 'The diagram shows a triangle.'
        groq=NS(stream=stream,describe_image=AsyncMock(return_value='Three labelled sides form a triangle.'))
        c.application.bot_data.update(groq=groq,web=None)
        with tempfile.TemporaryDirectory() as d:
            store=ChatStore(Path(d)/'chats.db');c.application.bot_data['chat_store']=store
            try:
                with patch.object(ocr,'recognize',AsyncMock(return_value='A?')),patch.object(config,'VISION_ENABLED',True),patch.object(main,'charge_request'):
                    await main.generate_answer(u,c,{},'Explain the diagram',photo=True)
                groq.describe_image.assert_awaited_once_with(b'raw','Explain the diagram','A?')
                self.assertIn('Three labelled sides',seen[0][-1]['content'])
                self.assertIn('A?',seen[0][-1]['content'])
                self.assertIn('The diagram shows a triangle.',str(c.bot._post.call_args))
            finally:store.close()

    async def test_weak_ocr_or_diagram_uses_vision_and_sends_both_to_main_model(self):
        for transcript,question in (('x?','Solve this'),('Readable text from a labelled image '*4,'Describe the chart')):
            groq=NS(describe_image=AsyncMock(return_value='The chart slopes upward.'))
            with patch.object(ocr,'recognize',AsyncMock(return_value=transcript)),patch.object(config,'VISION_ENABLED',True):
                prompt=await ocr.analyze_image(groq,b'raw',question)
            groq.describe_image.assert_awaited_once_with(b'raw',question,transcript)
            messages=main.provider_messages([],prompt,{'style':'balanced'})
            self.assertIn(transcript,messages[-1]['content'])
            self.assertIn('The chart slopes upward.',messages[-1]['content'])
            self.assertIn(question,messages[-1]['content'])

    async def test_usable_ocr_skips_vision_failure_is_honest_and_disable_is_respected(self):
        groq=NS(describe_image=AsyncMock(side_effect=ProviderError('offline')))
        with patch.object(ocr,'recognize',AsyncMock(return_value='This is a clearly legible sentence from a page.')),patch.object(config,'VISION_ENABLED',True):
            await ocr.analyze_image(groq,b'raw','Transcribe')
            groq.describe_image.assert_not_awaited()
            result=await ocr.analyze_image(groq,b'raw','Describe the scene')
            self.assertIn('Visual analysis was unavailable',result)
        groq.describe_image.reset_mock()
        with patch.object(ocr,'recognize',AsyncMock(return_value='x')),patch.object(config,'VISION_ENABLED',False):
            await ocr.analyze_image(groq,b'raw','Describe')
            groq.describe_image.assert_not_awaited()
        with patch.object(ocr,'recognize',AsyncMock(side_effect=ProviderError('OCR offline'))),patch.object(config,'VISION_ENABLED',True):
            with self.assertRaises(ProviderError):await ocr.analyze_image(groq,b'raw','Describe')

    async def test_vision_uses_real_image_bytes_and_returns_only_observations(self):
        calls=[];raw=BytesIO();Image.new('RGB',(4,4),'blue').save(raw,format='PNG')
        def handle(request):
            calls.append(json.loads(request.content))
            return httpx.Response(200,json={'choices':[{'message':{'content':'A blue square.','reasoning':'private'}}]})
        client=GroqClient('test-only',httpx.AsyncClient(transport=httpx.MockTransport(handle)))
        try:self.assertEqual(await client.describe_image(raw.getvalue(),'What color?','?'),'A blue square.')
        finally:await client.close()
        self.assertEqual(calls[0]['model'],config.GROQ_VISION_MODEL)
        content=calls[0]['messages'][-1]['content']
        self.assertTrue(content[1]['image_url']['url'].startswith('data:image/jpeg;base64,'))
        self.assertIn('What color?',content[0]['text'])


class ImageDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def test_send_show_image_routing_avoids_programming_and_real_photo_search(self):
        for prompt in ('Send image of moon','show me a picture of the moon','Could you give me an image of a mountain'):
            self.assertTrue(image_intent(prompt),prompt)
        for prompt in ('Write a program to show an image','Give code to resize a photo','Find an existing photo of the moon'):
            self.assertFalse(image_intent(prompt),prompt)

    async def test_guest_rich_rejection_falls_back_in_same_inline_message(self):
        _,c=fixture();c.bot.edit_message_media=AsyncMock()
        c.bot._post.side_effect=BadRequest('rich not supported')
        c.application.bot_data.update(global_settings={'mode':'guest'},groq=NS(),web=None)
        item={'query':'Send image of moon','owner':123,'mode':'guest','pages':None,'running':True}
        with patch('inline_mode.generate_images',AsyncMock(return_value=[b'image'])),patch('inline_mode.cache_images',AsyncMock(return_value=['cached-photo'])),patch('inline_mode.charge_request') as charge:
            await generate_inline(c,'guest-inline-id','token',item,{})
            charge.assert_called_once()
        kwargs=c.bot.edit_message_media.call_args.kwargs
        self.assertEqual(kwargs['inline_message_id'],'guest-inline-id')
        self.assertEqual(kwargs['media'].media,'cached-photo')
        c.bot.send_message.assert_not_awaited()

    async def test_cache_unavailable_uses_verified_url_without_dm_upload(self):
        _,c=fixture();c.bot.edit_message_media=AsyncMock()
        c.application.bot_data.update(global_settings={'mode':'guest'},groq=NS(),web=None)
        item={'query':'Send image of moon','owner':123,'mode':'guest','pages':None,'running':True}
        url='https://fal.media/test/moon.jpg'
        with patch('inline_mode.generate_images',AsyncMock(return_value=[GeneratedImage(b'image',url)])),patch('inline_mode.cache_images',AsyncMock(side_effect=ProviderError('cache unavailable'))),patch('inline_mode.charge_request') as charge:
            await generate_inline(c,'guest-id','token',item,{})
            charge.assert_called_once()
        self.assertEqual(c.bot.edit_message_media.call_args.kwargs['media'].media,url)
        self.assertEqual(c.bot.edit_message_media.call_args.kwargs['inline_message_id'],'guest-id')

    async def test_premium_gallery_fallback_retains_all_four_images(self):
        from image_generation import image_rich
        _,c=fixture();c.bot.edit_message_media=AsyncMock();c.bot._post.side_effect=BadRequest('unsupported')
        item={'pages':[image_rich(['a','b','c','d'])]}
        await show_page(c.bot,'same-id','token',item,0)
        self.assertEqual(len(item['pages']),4)
        await show_page(c.bot,'same-id','token',item,3)
        self.assertEqual(c.bot.edit_message_media.call_args.kwargs['media'].media,'d')

    async def test_normal_chat_rich_rejection_uses_native_photo_but_network_error_is_not_replayed(self):
        u,c=fixture();c.bot._post.side_effect=BadRequest('unsupported')
        with patch('image_generation.cache_images',AsyncMock(return_value=['cached'])):
            await deliver_images(u.message,[b'image'],'Moon',c,123)
            u.message.reply_photo.assert_awaited_once()
            u.message.reply_photo.reset_mock();c.bot._post.side_effect=NetworkError('lost response')
            with self.assertRaises(NetworkError):await deliver_images(u.message,[b'image'],'Moon',c,123)
            u.message.reply_photo.assert_not_awaited()
