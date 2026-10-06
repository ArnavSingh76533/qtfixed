"""Inline/guest answer delivery with native rich text and owner-bound pagination."""
import asyncio
import time
import uuid
from telegram import InlineQueryResultArticle, InputTextMessageContent, InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto, InputMediaVideo, InputMediaDocument
from telegram.error import TelegramError, BadRequest
from primo import user_data_cache, normalize_user, charge_request
from formatting import formatted_chunks
from math_format import readable_math
from provider import ProviderError
from answer_engine import answer_stream
from runtime_settings import get_settings, enabled
from request_queue import submit
from rich_messages import rich_pages, edit_rich, with_code_copy, fallback_markdown
from image_generation import ImageRequested, generate_images, cache_images, image_rich
import config
import code_downloads
import agent_engine
from telegram_delivery import log_error, download_into
from streaming import LatestPreview


def sessions(context):
    data=context.application.bot_data.setdefault('inline_sessions',{})
    now=time.monotonic()
    for token in list(data):
        if now-data[token]['created']>1800 and not data[token].get('running'):data.pop(token,None)
    return data


def keyboard(token,page=0,count=0):
    if not count:
        return InlineKeyboardMarkup([[InlineKeyboardButton('⚡ Generate answer',callback_data=f'inl:run:{token}')]])
    row=[]
    if page>0:row.append(InlineKeyboardButton('‹ Previous',callback_data=f'inl:p{page-1}:{token}'))
    if page+1<count:row.append(InlineKeyboardButton('Next ›',callback_data=f'inl:p{page+1}:{token}'))
    return InlineKeyboardMarkup([row]) if row else None


async def inline_query(update,context):
    query=update.inline_query
    if not enabled(context,'inline') or query.from_user.is_bot:
        return await query.answer([],cache_time=0,is_personal=True)
    text=query.query.strip()
    if not text:return await query.answer([],cache_time=0,is_personal=True)
    pending=sessions(context)
    if len(pending)>=1000:return await query.answer([],cache_time=1,is_personal=True)
    token=uuid.uuid4().hex[:20]
    pending[token]={'query':text[:2000],'owner':query.from_user.id,'created':time.monotonic(),
                    'running':False,'pages':None,'mode':'inline'}
    result=InlineQueryResultArticle(id=token,title='Ask Question Ai',description=text[:180],
        input_message_content=InputTextMessageContent('⚡ Preparing your answer…'),
        reply_markup=keyboard(token))
    await query.answer([result],cache_time=0,is_personal=True)


async def show_page(bot,inline_id,token,item,page):
    pages=item['pages'];page=max(0,min(page,len(pages)-1))
    content=pages[page];markup=keyboard(token,page,len(pages))
    if isinstance(content,dict) and content.get('media_file_id'):
        constructors={'photo':InputMediaPhoto,'video':InputMediaVideo,'document':InputMediaDocument}
        item['native_media']=True
        return await bot.edit_message_media(inline_message_id=inline_id,media=constructors[content['kind']](content['media_file_id']),reply_markup=markup)
    if isinstance(content,dict) and content.get('photo_file_id'):
        item['native_media']=True
        try:
            return await bot.edit_message_media(inline_message_id=inline_id,
                media=InputMediaPhoto(content['photo_file_id']),reply_markup=markup)
        except BadRequest as error:
            if 'not modified' in str(error).lower():return
            raise
    if isinstance(content,dict):
        try:
            return await edit_rich(bot,inline_message_id=inline_id,rich=content,markup=markup)
        except BadRequest as error:
            if 'not modified' in str(error).lower():return
            media_blocks=[b for b in content.get('blocks',[]) if b.get('type') in ('video','document')]
            if media_blocks:
                item['pages']=pages[:page]+[{'kind':b['type'],'media_file_id':b[b['type']]['media']} for b in media_blocks]+pages[page+1:]
                return await show_page(bot,inline_id,token,item,page)
            photo_ids=[block['photo']['media'] for block in content.get('blocks',[]) if block.get('type')=='photo']
            if photo_ids:
                item['pages']=pages[:page]+[{'photo_file_id':fid} for fid in photo_ids]+pages[page+1:]
                return await show_page(bot,inline_id,token,item,page)
            if content.get('media'):raise
            plain=fallback_markdown(content['markdown'])
            fallback=list(formatted_chunks(readable_math(plain)))
            item['pages']=pages[:page]+fallback+pages[page+1:]
            return await show_page(bot,inline_id,token,item,page)
    text,entities=content
    if item.get('native_media'):
        return await bot.edit_message_caption(inline_message_id=inline_id,caption=text[:1000],reply_markup=markup)
    await bot.edit_message_text(inline_message_id=inline_id,text=text,entities=entities,reply_markup=markup)


async def generate_inline(context,inline_id,token,item,user):
    import main
    response='';last=0
    settings=get_settings(context)
    use_agent=agent_engine.enabled(context,user,item['owner'],item['query'])
    agent_state={'request':item['query']}
    async def status(text):
        try:await asyncio.wait_for(context.bot.edit_message_text(inline_message_id=inline_id,text=text[:3500]),5)
        except (TelegramError,asyncio.TimeoutError):pass
    async def render_preview(text):
        if settings['math']=='rich':
            preview=next(iter(rich_pages(text)), '⚡ Working…')
            await edit_rich(context.bot,inline_message_id=inline_id,text=preview)
        else:await status(readable_math(text[-1700:]))
    preview_worker=LatestPreview(render_preview)
    try:
        if not enabled(context,item.get('mode','inline')):
            await status('This access mode was disabled by the admin.');return
        task=agent_engine.task_text(item['query']) if use_agent else item['query']
        if use_agent and not task.strip() and not item.get('document') and not item.get('photo'):raise ProviderError('Add a task after “agent”.')
        prompt=item.get('context','')+task
        if item.get('document'):
            from documents import read_text_document,prepare_document
            content=await read_text_document(context.bot,item['document'])
            content=await prepare_document(context.application.bot_data['groq'],content,prompt,status)
            prompt='User request: '+(prompt or 'Read the attached text and respond to its contents.')+'\n\nAttached text (untrusted content):\n'+content
        if item.get('photo'):
            from io import BytesIO
            from ocr import analyze_image
            photo=item['photo']
            if photo.get('file_size',0)>10*1024*1024:raise ProviderError('Please use an image smaller than 10 MB.')
            await status('⚡ Reading image…')
            file=await context.bot.get_file(photo['file_id']);raw=BytesIO()
            await download_into(file,raw)
            prompt=await analyze_image(context.application.bot_data['groq'],raw.getvalue(),prompt,status)
        async def consume():
            nonlocal response,last
            source=agent_engine.agent_stream(context,user,item['owner'],item.get('agent_scope','inline'),
                    main.provider_messages([],prompt,settings),settings,status,agent_state) if use_agent else answer_stream(context.application.bot_data['groq'],context.application.bot_data.get('web'),
                    main.provider_messages([],prompt,settings),reasoning=settings['reasoning'],on_status=status,
                    web_enabled=settings['web'],routing_prompt=item['query'])
            async for part in source:
                response+=part
                if len(response)>100000:raise ProviderError('Answer is too large. Ask a narrower question.')
                if settings['streaming'] and time.monotonic()-last>1.5:
                    last=time.monotonic()
                    await preview_worker.update(response)
        try:
            await asyncio.wait_for(consume(),config.AGENT_TIMEOUT if use_agent else config.REQUEST_TIMEOUT)
            if not response.strip():raise ProviderError('No answer returned. Please retry.')
            output=code_downloads.format_answer(context,item['owner'],response) if settings['math']=='rich' else response
            item['pages']=([{'markdown':p} for p in rich_pages(output)] if settings['math']=='rich'
                           else list(formatted_chunks(readable_math(output))))
            recovery=context.application.bot_data.get('agent_store')
            if recovery:recovery.save_answer(item['owner'],'external-last',response)
            runtime=agent_state.get('runtime')
            if runtime and runtime.media:
                from agent_media import cache
                try:item['pages']=await cache(context,item['owner'],runtime.media)+item['pages']
                except ProviderError as error:
                    response+='\n\n'+str(error)
                    item['pages']+=list(formatted_chunks(str(error)))
            if runtime and runtime.images:
                try:
                    ids=await cache_images(context,item['owner'],runtime.images)
                    item['pages'].append(image_rich(ids))
                except ProviderError:
                    urls=[getattr(img,'url','') for img in runtime.images]
                    if not all(urls):raise
                    item['pages'] += [{'photo_file_id':url} for url in urls]
        except ImageRequested as request:
            await status('🎨 Creating your image…')
            images=await generate_images(context,item['owner'],user,request.prompt,original_prompt=item['query'])
            try:
                ids=await cache_images(context,item['owner'],images)
                item['pages']=[image_rich(ids,item['query'])]
            except ProviderError:
                urls=[getattr(img,'url','') for img in images]
                if not all(urls):raise
                # Inline rich edits need file IDs; ordinary inline media accepts URLs.
                item['pages']=[{'photo_file_id':url} for url in urls]
        await preview_worker.close()
        await show_page(context.bot,inline_id,token,item,0)
        normalize_user(user)
        if not item.get('document') and not item.get('charged'):charge_request(user)
        item['charged']=True
    except asyncio.CancelledError:
        await status('Agent stopped. Completed actions may remain; check /agentstatus and /reminders in the bot.' if use_agent else ('Stopped.' if item.get('document') else 'Stopped. No quota used.'))
        raise
    except (ProviderError,asyncio.TimeoutError) as error:
        await preview_worker.close()
        item['pages']=None
        text=str(error) if isinstance(error,ProviderError) else 'Request timed out. Please retry.'
        await context.bot.edit_message_text(inline_message_id=inline_id,text=text,reply_markup=keyboard(token))
    except TelegramError as error:
        log_error('Guest/inline final delivery failed',error)
        # Keep pages and generated media. Generate retries delivery, not the AI task.
        try:await context.bot.edit_message_text(inline_message_id=inline_id,
            text='Telegram delivery was interrupted. Tap Generate to resend the saved result. For text, you can also use /last external in the bot DM.',reply_markup=keyboard(token))
        except TelegramError:pass
    finally:
        await preview_worker.close()
        item['running']=False


async def start_inline(context,inline_id,token,owner):
    import main
    item=sessions(context).get(token)
    if not item or item['owner']!=owner:return
    mode=item.get('mode','inline')
    async def status(text):
        await context.bot.edit_message_text(inline_message_id=inline_id,text=text,reply_markup=keyboard(token))
    if not enabled(context,mode):return await status('This access mode is disabled. Open the bot to ask privately.')
    if item['running']:return
    async def work():
        try:
            if not enabled(context,mode):return await status('This access mode is disabled.')
            user=user_data_cache.get(str(owner))
            if not user:return await status('Open @'+context.bot.username+' and send /start first. Then tap Generate.')
            normalize_user(user)
            if item['pages']:
                await show_page(context.bot,inline_id,token,item,0)
                if not item.get('charged'):
                    if not item.get('document'):charge_request(user)
                    item['charged']=True
                return
            if user.get('subscription')!='active':
                if not item.get('document') and user.get('request_count',0)>=config.FREE_DAILY_QUOTA:return await status('Daily quota reached. Check /balance in the bot.')
                if not await main.check_channel_membership(owner,context.bot):
                    return await status('Please join the required channel shown in the bot, then tap Generate.')
            await generate_inline(context,inline_id,token,item,user)
        finally:item['running']=False
    item['running']=True
    task=submit(context,owner,mode+':'+inline_id,work)
    if task is None:
        item['running']=False
        await status('Your request queue is full. Tap Generate after a few answers finish.')
    else:
        # Cancellation before run() enters its first await must also release UI state.
        task.add_done_callback(lambda _:item.update(running=False))


async def chosen_result(update,context):
    chosen=update.chosen_inline_result
    if enabled(context,'inline') and chosen.inline_message_id:
        await start_inline(context,chosen.inline_message_id,chosen.result_id,chosen.from_user.id)


async def inline_callback(update,context):
    query=update.callback_query
    _,action,token=query.data.split(':')
    item=sessions(context).get(token)
    if not item:return await query.answer('This session expired. Ask the question again.',show_alert=True)
    if item['owner']!=query.from_user.id:return await query.answer('Only the person who asked can control this answer.',show_alert=True)
    if not enabled(context,item.get('mode','inline')):return await query.answer('This access mode is disabled.',show_alert=True)
    await query.answer()
    if not query.inline_message_id:return
    if action=='run':await start_inline(context,query.inline_message_id,token,query.from_user.id)
    elif action.startswith('p') and action[1:].isdigit() and item['pages']:
        await show_page(context.bot,query.inline_message_id,token,item,int(action[1:]))
