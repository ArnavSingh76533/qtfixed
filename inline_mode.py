"""Fast inline query results; generate only after selection or an explicit button."""
import asyncio
import time
import uuid
from telegram import InlineQueryResultArticle, InputTextMessageContent, InlineKeyboardMarkup, InlineKeyboardButton, LinkPreviewOptions
from telegram.error import TelegramError
from primo import user_data_cache, normalize_user, charge_request
from formatting import formatted_chunks
from math_format import readable_math
from provider import ProviderError
from answer_engine import answer_stream
import config


def sessions(context):
    data=context.application.bot_data.setdefault('inline_sessions',{})
    now=time.monotonic()
    for token in list(data):
        if now-data[token]['created']>1800 and not data[token].get('running'):
            data.pop(token,None)
    return data


def keyboard(token, page=0, count=0):
    if not count:
        return InlineKeyboardMarkup([[InlineKeyboardButton('⚡ Generate answer',callback_data=f'inl:run:{token}')]])
    row=[]
    if page>0:row.append(InlineKeyboardButton('‹ Previous',callback_data=f'inl:p{page-1}:{token}'))
    if page+1<count:row.append(InlineKeyboardButton('Next ›',callback_data=f'inl:p{page+1}:{token}'))
    return InlineKeyboardMarkup([row]) if row else None


async def inline_query(update,context):
    query=update.inline_query
    text=query.query.strip()
    if not text:return await query.answer([],cache_time=0,is_personal=True)
    pending=sessions(context)
    if len(pending)>=1000:
        return await query.answer([],cache_time=1,is_personal=True)
    token=uuid.uuid4().hex[:20]
    pending[token]={'query':text[:2000],'owner':query.from_user.id,'created':time.monotonic(),'running':False,'pages':None}
    result=InlineQueryResultArticle(id=token,title='⚡ Ask Question Ai',description=text[:180],
        input_message_content=InputTextMessageContent('⚡ '+text[:180]+'\nPreparing answer… Tap Generate if it does not start.'),
        reply_markup=keyboard(token))
    await query.answer([result],cache_time=0,is_personal=True)


async def show_page(bot,inline_id,token,item,page):
    pages=item['pages']
    page=max(0,min(page,len(pages)-1))
    text,entities=pages[page]
    await bot.edit_message_text(inline_message_id=inline_id,text=text,entities=entities,
        link_preview_options=LinkPreviewOptions(is_disabled=True),reply_markup=keyboard(token,page,len(pages)))


async def generate_inline(context,inline_id,token,item,user):
    import main
    response='';last=0
    try:
        async def consume():
            nonlocal response,last
            async for part in answer_stream(context.application.bot_data['groq'],context.application.bot_data.get('web'),
                    main.provider_messages([],item['query'],{'style':'balanced'})):
                response+=part
                if len(response)>100000:raise ProviderError('Answer is too large. Ask a narrower question.')
                if time.monotonic()-last>1.5:
                    last=time.monotonic()
                    try:await context.bot.edit_message_text(inline_message_id=inline_id,text=response[-1700:] or '⚡ Working…')
                    except TelegramError:pass
        await asyncio.wait_for(consume(),config.REQUEST_TIMEOUT)
        pages=list(formatted_chunks(readable_math(response)))
        if not pages:raise ProviderError('No answer returned. Please retry.')
        item['pages']=pages
        await show_page(context.bot,inline_id,token,item,0)
        normalize_user(user);charge_request(user)
    except asyncio.CancelledError:
        await context.bot.edit_message_text(inline_message_id=inline_id,text='Stopped. No quota used.')
    except (ProviderError,asyncio.TimeoutError) as error:
        item['pages']=None
        message=str(error) if isinstance(error,ProviderError) else 'Request timed out. Please retry.'
        await context.bot.edit_message_text(inline_message_id=inline_id,text=message,reply_markup=keyboard(token))
    except TelegramError:
        item['pages']=None
    finally:
        item['running']=False
        context.application.bot_data['active_requests'].pop(item['owner'],None)


async def start_inline(context,inline_id,token,owner):
    import main
    item=sessions(context).get(token)
    if not item or item['owner']!=owner:return
    if item['pages']:
        await show_page(context.bot,inline_id,token,item,0);return
    if item['running']:return
    active=context.application.bot_data['active_requests']
    async def status(text):
        await context.bot.edit_message_text(inline_message_id=inline_id,text=text,reply_markup=keyboard(token))
    if owner in active:
        return await status('You already have an answer running. Use /stop in the bot, then tap Generate.')
    if len(active)>=config.MAX_CONCURRENT_REQUESTS:
        return await status('The bot is busy. Tap Generate again shortly.')
    user=user_data_cache.get(str(owner))
    if not user:
        return await status('Open @'+context.bot.username+' and send /start first. Then tap Generate.')
    normalize_user(user)
    if user.get('subscription')!='active':
        if user.get('request_count',0)>=config.FREE_DAILY_QUOTA:return await status('Daily quota reached. Check /balance in the bot.')
        if not await main.check_channel_membership(owner,context.bot):
            return await status('Please join the required channel shown in the bot, then tap Generate.')
    # Eligibility awaited network I/O; recheck before atomically reserving the user slot.
    if owner in active or item['running'] or len(active)>=config.MAX_CONCURRENT_REQUESTS:return
    item['running']=True
    active[owner]=asyncio.create_task(generate_inline(context,inline_id,token,item,user))


async def chosen_result(update,context):
    chosen=update.chosen_inline_result
    if chosen.inline_message_id:
        await start_inline(context,chosen.inline_message_id,chosen.result_id,chosen.from_user.id)


async def inline_callback(update,context):
    query=update.callback_query
    _,action,token=query.data.split(':')
    item=sessions(context).get(token)
    if not item:
        return await query.answer('This inline session expired. Ask the question again.',show_alert=True)
    if item['owner']!=query.from_user.id:
        return await query.answer('Only the person who asked can control this answer.',show_alert=True)
    await query.answer()
    if not query.inline_message_id:return
    if action=='run':await start_inline(context,query.inline_message_id,token,query.from_user.id)
    elif action.startswith('p') and action[1:].isdigit() and item['pages']:
        await show_page(context.bot,query.inline_message_id,token,item,int(action[1:]))
