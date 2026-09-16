import asyncio
import logging
import os
import datetime
from io import BytesIO
import primo
from storage import read_json, write_json
import config  # Loads .env before primo/broadcast read configuration.
from telegram import (Update, InlineKeyboardButton, InlineKeyboardMarkup,
                      LinkPreviewOptions, BotCommand, MenuButtonCommands, BotCommandScopeChat)
from telegram.error import BadRequest, TelegramError, RetryAfter
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters, InlineQueryHandler, ChosenInlineResultHandler, ChatMemberHandler, TypeHandler
from filelock import FileLock
from primo import (normalize_user, charge_request, initialize_cache, flush_cache_to_file,
                   backup_user_data, user_data_cache, DATA_DIR, BOT_USERNAME, ADMIN_ID,
                   start, balance, generate_promo, claim_promo, reset_all_counts,
                   handle_group_addition, allow_group, disallow_group, load_group_data, save_group_data, bot_membership_changed, register_group)
from broadcast import stats, button_callback, broadcast, ads, send_ad_message, campaigns, campaign_command, BroadcastManager
from chat_store import ChatStore
from provider import GroqClient, ProviderError
from formatting import formatted_chunks, units
from streaming import StreamPreview
from web_search import FeloClient
from answer_engine import answer_stream
from math_format import readable_math, segments, render_equation, latex_text
from ocr import recognize, OCR_PROMPT
from inline_mode import inline_query, chosen_result, inline_callback
from runtime_settings import get_settings, save_settings, initialize as initialize_settings
from request_queue import submit, cancel, owner_of
from rich_messages import send_rich, rich_pages
from image_generation import ImageClient, ImageRequested, generate_images, deliver_images
from guest_mode import guest_update

logger = logging.getLogger(__name__)
logging.getLogger('httpx').setLevel(logging.WARNING)
LOG_CHANNEL_ID = os.environ.get('LOG_CHANNEL_ID', '-1002224010991')
CHANNEL_ID = os.environ.get('CHANNEL_ID', '-1002081366095')
aggregated_logs = []

async def check_channel_membership(user_id, bot):
    if not CHANNEL_ID:
        return True
    try:
        member = await bot.get_chat_member(CHANNEL_ID, int(user_id))
        return member.status in ['member', 'administrator', 'creator'] or (member.status == 'restricted' and member.is_member)
    except Exception as e:
        logging.error(f"Error checking channel membership: {e}")
        return False

async def set_log_channel(update, context):
    global LOG_CHANNEL_ID
    if str(update.effective_user.id) != ADMIN_ID:
        await update.effective_message.reply_text('Only the bot admin can change logging.')
        return
    if len(context.args) != 1:
        await update.effective_message.reply_text('Usage: /setlogchannel <channel_id|@username|off>')
        return
    value = context.args[0]
    if value.lower() == 'off':
        value = ''
    elif not (value.lstrip('-').isdigit() or value.startswith('@')):
        await update.effective_message.reply_text('Use a numeric chat ID, @username, or off.')
        return
    saved=read_json(DATA_DIR / 'bot_settings.json',dict)
    saved['log_channel_id']=value
    write_json(DATA_DIR / 'bot_settings.json', saved)
    LOG_CHANNEL_ID = primo.LOG_CHANNEL_ID = value
    if not value:
        aggregated_logs.clear()
    await update.effective_message.reply_text('Log channel updated.' if value else 'Logging disabled.')

def history_key(update):
    message = update.effective_message
    return f"{update.effective_chat.id}:{message.message_thread_id or 0}:{update.effective_user.id}"


async def eligible_user(update, context):
    if not update.effective_message or not update.effective_user or update.effective_message.sender_chat:
        return None
    user_id = str(update.effective_user.id)
    if update.effective_chat.type in ('group', 'supergroup'):
        groups = load_group_data()
        chat_id = str(update.effective_chat.id)
        if chat_id not in groups:
            register_group(update.effective_chat)
            groups=load_group_data()
        if not groups[chat_id].get('is_allowed', False):
            await update.effective_message.reply_text('This group is disabled. A group administrator must send /allowgroup. Use /groupstatus for details.')
            return None
    user = user_data_cache.get(user_id)
    if user is None:
        await update.effective_message.reply_text("Please start me in DM first.", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Start in DM", url=f"https://t.me/{BOT_USERNAME}?start=true")]]))
        return None
    normalize_user(user)
    if user.get('subscription') != 'active':
        if user.get('request_count', 0) >= config.FREE_DAILY_QUOTA:
            await update.effective_message.reply_text(f"Daily limit reached ({config.FREE_DAILY_QUOTA} questions). Your quota resets 24 hours after the first question in this window.")
            return None
        if not await check_channel_membership(user_id, context.bot):
            await update.effective_message.reply_text("Please join the channel, then try again.", reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Join Channel", url=os.environ.get('CHANNEL_URL', 'https://t.me/BotCommunityHub'))]]))
            return None
    return user


async def record_log(bot, text):
    if not LOG_CHANNEL_ID:
        return
    aggregated_logs.append(text)
    if len(aggregated_logs) < 50:
        return
    batch = aggregated_logs[:50]
    del aggregated_logs[:50]
    try:
        await bot.send_document(chat_id=LOG_CHANNEL_ID,
            document=BytesIO('\n\n'.join(batch).encode('utf-8')),
            filename='question_logs.txt', caption=f"{len(batch)} questions logged")
    except TelegramError:
        logger.warning("Could not send question log batch")


def store(context):
    return context.application.bot_data['chat_store']


def busy(context, user_id):
    return next((task for key,task in context.application.bot_data['active_requests'].items() if owner_of(key)==user_id and not task.done()),None)


def answer_keyboard(owner, answer):
    rows = [[InlineKeyboardButton('↻ Retry latest', callback_data=f'chat:retry:{owner}',style='primary'),
             InlineKeyboardButton('＋ New chat', callback_data=f'chat:new:{owner}',style='success')]]
    return InlineKeyboardMarkup(rows)


async def deliver_text(message, answer, owner=None):
    chunks = list(formatted_chunks(readable_math(answer)))
    if not chunks:
        raise ProviderError('The answer was empty. Please retry.')
    for i, (text, entities) in enumerate(chunks):
        kwargs = {'link_preview_options': LinkPreviewOptions(is_disabled=True)}
        if owner is not None and i == len(chunks)-1:
            kwargs['reply_markup'] = answer_keyboard(owner, answer)
        for attempt in range(3):
            try:
                try:
                    await message.reply_text(text, entities=entities, **kwargs)
                except BadRequest:
                    await message.reply_text(text, **kwargs)
                break
            except RetryAfter as error:
                if attempt == 2:
                    raise
                delay = error.retry_after
                await asyncio.sleep((delay.total_seconds() if hasattr(delay,'total_seconds') else delay)+1)


async def deliver_answer(message, answer, owner=None, math_mode='rich'):
    if math_mode!='rich':
        return await deliver_text(message,answer,owner)
    pages=list(rich_pages(answer))
    if not pages:raise ProviderError('The answer was empty. Please retry.')
    for i,page in enumerate(pages):
        markup=answer_keyboard(owner,answer) if owner is not None and i==len(pages)-1 else None
        try:
            await send_rich(message,page,markup)
        except BadRequest:
            # Older/local Bot API servers may not implement rich messages.
            # Never silently send raw LaTeX; retain a readable text fallback.
            await deliver_text(message,page,owner if i==len(pages)-1 else None)


def provider_messages(history, prompt, settings):
    instructions = config.SYSTEM_PROMPT + config.FORMATTING_PROMPT + {
        'concise':' Keep answers brief unless the user asks for detail.',
        'detailed':' Give thorough explanations, steps, and examples when useful.',
        'balanced':' Follow the original response-length instructions above.'}[settings['style']]
    kept = []
    count = len(prompt)
    # Retain whole user/assistant exchanges, newest first.
    for index in range(len(history)-2, -1, -2):
        pair = history[index:index+2]
        size = sum(len(m['content']) for m in pair)
        if count + size > config.MAX_CONTEXT_CHARS:
            break
        kept = pair + kept
        count += size
    return [{'role':'system','content':instructions}] + kept + [{'role':'user','content':prompt}]


async def typing_heartbeat(message, bot):
    while True:
        try:
            await bot.send_chat_action(message.chat_id, 'typing', message_thread_id=message.message_thread_id)
        except TelegramError:
            pass
        await asyncio.sleep(4)


async def generate_answer(update, context, user, prompt, retry=False, force_web=False, photo=False, force_image=False):
    key = history_key(update)
    history, _ = store(context).get(key)
    settings = get_settings(context)
    base = history[:-2] if retry else history
    preview = StreamPreview(update.effective_message, context.bot, update.effective_user.id, settings['streaming'], rich=settings['math']=='rich')
    heartbeat = None
    response = ''
    original_prompt=prompt
    try:
        await preview.start()
        heartbeat = asyncio.create_task(typing_heartbeat(update.effective_message, context.bot))
        if photo:
            source = update.effective_message
            if not source.photo and not (source.document and source.document.mime_type and source.document.mime_type.startswith('image/')):
                source = source.reply_to_message
            item = source.photo[-1] if source.photo else source.document
            if item.file_size and item.file_size > 10*1024*1024:
                raise ProviderError('Please send an image smaller than 10 MB.')
            photo_file = await context.bot.get_file(item.file_id)
            raw = BytesIO()
            await photo_file.download_to_memory(raw)
            await preview.set_status('⚡ Reading image…')
            extracted = await recognize(raw.getvalue())
            prompt = OCR_PROMPT + extracted + ('\n\nUser instructions: '+prompt if prompt else '')
        async def consume():
            nonlocal response
            async for piece in answer_stream(context.application.bot_data['groq'],
                    context.application.bot_data.get('web'), provider_messages(base,prompt,settings),
                    reasoning=settings['reasoning'],force_web=force_web,on_status=preview.set_status):
                response += piece
                if len(response) > 120000:
                    raise ProviderError('The response was too large. Please ask a narrower question.')
                await preview.update(response)
        try:
            if force_image:raise ImageRequested(prompt)
            await asyncio.wait_for(consume(), timeout=config.REQUEST_TIMEOUT)
        except ImageRequested as request:
            await preview.set_status('🎨 Creating your image…')
            images=await generate_images(context,update.effective_user.id,user,request.prompt,original_prompt=original_prompt)
            await deliver_images(update.effective_message,images,request.prompt)
            normalize_user(user);charge_request(user)
            return
        if not response.strip():
            raise ProviderError('The assistant returned an empty answer. Please retry.')
        await deliver_answer(update.effective_message, response, update.effective_user.id, settings.get('math','rich'))
        # No await between the successful delivery and state update.
        normalize_user(user)
        charge_request(user)
        updated = base + [{'role':'user','content':prompt}, {'role':'assistant','content':response}]
        max_turns = 35 if user.get('subscription') == 'active' else 6
        updated = updated[-2*max_turns:]
        # Also bound disk history size by whole exchanges.
        while len(updated) > 2 and sum(len(m['content']) for m in updated) > 160000:
            updated = updated[2:]
        store(context).save(key, updated, settings)
        await record_log(context.bot, f'User {update.effective_user.id} sent: {prompt}\nBot replied: {response}')
    except asyncio.CancelledError:
        await update.effective_message.reply_text('Stopped. This request was not added to your history or quota.')
    except (ProviderError, asyncio.TimeoutError) as error:
        text = str(error) if isinstance(error, ProviderError) else 'The answer took too long. Please retry.'
        await update.effective_message.reply_text(text + '\nThis request was not added to your history or quota.')
    except TelegramError:
        logger.warning('Telegram answer delivery failed')
    except Exception:
        logger.exception('Answer processing failed')
        await update.effective_message.reply_text('Could not finish saving or delivering the answer. Please contact the admin if it keeps happening.')
    finally:
        if heartbeat:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        await preview.close()
        active=context.application.bot_data['active_requests']
        if active.get(update.effective_user.id) is asyncio.current_task():
            active.pop(update.effective_user.id,None)


async def launch_question(update, context, retry=False, prompt=None, force_web=False, photo=False, force_image=False):
    uid=update.effective_user.id
    if update.effective_user.is_bot:return
    if not retry and not photo and (not prompt or not prompt.strip()):
        await update.effective_message.reply_text('Type your question after /ask.');return
    async def work():
        # Recheck AFTER acquiring the per-user queue lock: quota cannot overshoot.
        user=await eligible_user(update,context)
        if user is None:return
        question=(prompt or '').strip()
        if retry:
            history,_=store(context).get(history_key(update))
            if len(history)<2:
                await update.effective_message.reply_text('There is no completed answer to retry in this chat.');return
            question=history[-2]['content']
        await generate_answer(update,context,user,question,retry,force_web,photo,force_image)
    if submit(context,uid,history_key(update),work) is None:
        await update.effective_message.reply_text('Your request queue is full. Please let a few answers finish.')


async def image_generate_command(update,context):
    reply=update.effective_message.reply_to_message
    prompt=' '.join(context.args or []) or (getattr(reply,'text',None) or '')
    await launch_question(update,context,prompt=prompt,force_image=True)


async def handle_message(update, context):
    if not update.effective_user or not update.message:
        return
    text = update.message.text
    if update.effective_chat.type != 'private' and config.GROUP_MENTIONS_ONLY:
        mention = '@' + context.bot.username
        reply = update.message.reply_to_message
        if mention.lower() not in text.lower() and not (reply and reply.from_user and reply.from_user.id == context.bot.id):
            return
        import re
        text = re.sub(re.escape(mention), '', text, flags=re.I).strip()
    await launch_question(update, context, prompt=text)


async def ask_command(update, context):
    reply = update.effective_message.reply_to_message
    is_photo = bool(reply and (reply.photo or (reply.document and (reply.document.mime_type or '').startswith('image/'))))
    prompt = ' '.join(context.args) or (getattr(reply,'text',None) or '')
    await launch_question(update, context, prompt=prompt, photo=is_photo)


async def image_command(update, context):
    source = update.effective_message
    if not source.photo and not (source.document and (source.document.mime_type or '').startswith('image/')):
        source = source.reply_to_message
    if not source or not (source.photo or (source.document and (source.document.mime_type or '').startswith('image/'))):
        await update.effective_message.reply_text('Send a photo, or reply to one with /ocr.')
        return
    await launch_question(update,context,prompt=' '.join(context.args or []) or source.caption or '',photo=True)


async def web_command(update,context):
    reply=update.effective_message.reply_to_message
    prompt=' '.join(context.args) or (getattr(reply,'text',None) or '')
    await launch_question(update,context,prompt=prompt,force_web=True)


async def group_status(update,context):
    chat=update.effective_chat
    if chat.type not in ('group','supergroup'):
        await update.effective_message.reply_text('Use /groupstatus inside the group.');return
    group=load_group_data().get(str(chat.id))
    me=await context.bot.get_me()
    registered='not registered yet (first question will register it)' if group is None else ('enabled' if group.get('is_allowed') else 'disabled: admin must use /allowgroup')
    await update.effective_message.reply_text(f'Group: {registered}\nChat ID: {chat.id}\n'
        f'Ordinary-text filter: {"mentions/replies only" if config.GROUP_MENTIONS_ONLY else "all delivered text"}\n'
        f'BotFather privacy: {"off" if me.can_read_all_group_messages else "on (or bot may be a group admin)"}\n'
        f'Try /ask@{me.username} hello. For all ordinary messages/photos, make the bot admin or disable Group Privacy in BotFather.')


async def migrate_group(update,context):
    message=update.effective_message
    old=message.migrate_from_chat_id or message.chat_id
    new=message.migrate_to_chat_id or message.chat_id
    groups=load_group_data()
    if str(old) in groups and str(new) not in groups:
        groups[str(new)]=groups[str(old)].copy()
        save_group_data(groups)


async def retry_command(update, context):
    await launch_question(update, context, retry=True)


async def stop_command(update, context):
    scope=None if update.effective_chat.type=='private' else history_key(update)
    if not await cancel(context,update.effective_user.id,scope):
        await update.effective_message.reply_text('No answer is currently running in this chat.')


async def reset_conversation(update, context):
    await cancel(context,update.effective_user.id,history_key(update))
    store(context).clear(history_key(update))
    await update.effective_message.reply_text('New conversation started.')
    await send_ad_message(context.bot, update.effective_chat.id)


async def forget_command(update, context):
    if busy(context, update.effective_user.id):
        await update.effective_message.reply_text('Use /stop first, then /forget.')
        return
    store(context).clear(history_key(update), forget=True)
    await update.effective_message.reply_text('Saved conversation and settings for this chat/topic were cleared. Registration, quota, and any existing admin logs are unchanged.')


async def export_command(update, context):
    history, _ = store(context).get(history_key(update))
    if not history:
        await update.effective_message.reply_text('No saved conversation in this chat.')
        return
    text = '# Question Ai conversation\n\n' + '\n\n'.join(f"## {m['role'].title()}\n\n{m['content']}" for m in history)
    await update.effective_message.reply_document(BytesIO(text.encode('utf-8')), filename='conversation.md')


def is_admin(update):
    return bool(update.effective_user and str(update.effective_user.id)==ADMIN_ID)

async def settings_command(update, context, edit=False):
    if not is_admin(update):return
    if not update.effective_chat or update.effective_chat.type!='private':
        await update.effective_message.reply_text('Open my private chat to manage global settings.');return
    settings=get_settings(context)
    keyboard=InlineKeyboardMarkup([
        [InlineKeyboardButton(('✓ ' if settings['mode']==mode else '')+mode.title(),callback_data='admin:mode:'+mode)
         for mode in ('inline','guest','off')],
        [InlineKeyboardButton('Streaming: '+('ON' if settings['streaming'] else 'OFF'),callback_data='admin:stream:toggle')],
        [InlineKeyboardButton('Answer style: '+settings['style'],callback_data='admin:style:next')],
        [InlineKeyboardButton('Reasoning: '+settings['reasoning'],callback_data='admin:reason:next')],
        [InlineKeyboardButton('Formatting: '+settings['math'],callback_data='admin:math:next')]])
    text=('Admin settings · applies to everyone\n\n'
          'Access mode: '+settings['mode'].title()+'\n'
          'Only the selected mode accepts requests. Off disables both. Normal private/group commands stay available.\n\n'
          'For Guest, enable Guest Mode in BotFather’s bot settings. For Inline, enable Inline Mode and set inline feedback to Enabled. '
          'BotFather settings must match your selection here.')
    if edit:
        try:await update.callback_query.edit_message_text(text,reply_markup=keyboard)
        except BadRequest as error:
            if 'not modified' not in str(error).lower():raise
    else:await update.effective_message.reply_text(text,reply_markup=keyboard)

async def admin_callback(update,context):
    query=update.callback_query
    if not is_admin(update) or not update.effective_chat or update.effective_chat.type!='private':
        await query.answer('Admin only.',show_alert=True);return
    _,action,value=query.data.split(':')
    settings=get_settings(context)
    if action=='mode' and value in ('inline','guest','off'):settings['mode']=value
    elif action=='stream':settings['streaming']=not settings['streaming']
    elif action in ('style','reason','math'):
        field,values={'style':('style',['balanced','concise','detailed']),
                      'reason':('reasoning',['low','medium','high']),
                      'math':('math',['rich','unicode'])}[action]
        settings[field]=values[(values.index(settings[field])+1)%len(values)]
    else:
        await query.answer();return
    previous=get_settings(context)['mode']
    save_settings(context,settings)
    if settings['mode']!=previous:
        tasks=[task for key,task in context.application.bot_data.get('active_requests',{}).items()
               if isinstance(key,tuple) and str(key[1]).startswith(('inline:','guest:'))]
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
    await query.answer('Saved for everyone.')
    await settings_command(update,context,edit=True)


async def chat_callback(update, context):
    query = update.callback_query
    _, action, owner = query.data.split(':')
    if str(query.from_user.id) != owner:
        await query.answer('This control belongs to another user.', show_alert=True)
        return
    await query.answer()
    if action in ('stream','style','reason','math','settings'):
        if is_admin(update):await settings_command(update,context)
        return
    handler={'stop':stop_command,'retry':retry_command,'new':reset_conversation}.get(action)
    if handler:await handler(update,context)


async def model_command(update, context):
    if not is_admin(update) or update.effective_chat.type!='private':return
    await update.effective_message.reply_text(f'Model: {config.GROQ_MODEL}\nProvider: Groq\nThe admin can change GROQ_MODEL in .env.')


async def privacy_command(update, context):
    await update.effective_message.reply_text(
        'Text questions and recent history are sent to the configured AI service. Photos go to the configured OCR service unless local OCR is selected. Current-context questions may be sent to Felo. Image-generation prompts are sent to fal or getimg and logged as text when logging is enabled. Inline/guest answers never include your private chat history. Conversation history is stored locally, isolated by user, chat, and topic. Settings are global and managed by the bot admin. '
        + ('The admin has question logging enabled. ' if LOG_CHANNEL_ID else 'Question logging is disabled. ')
        + 'Use /export to download saved history, /new to clear it, or /forget to remove this chat’s stored history. Quota, registration, and any existing admin logs remain.')


async def help_command(update, context):
    text = ('Question Ai • Text, photos, math, and web\n\n'
            '/web <question> — search current information\n/ocr — reply to a photo to extract and solve\n/groupstatus — diagnose group setup\n'
            'Ask naturally, upload a photo, or describe an image to create.\n\n'
            '/ask <question> — ask in any enabled chat\n'
            '/stop — stop the current answer\n/retry — regenerate the latest answer (uses one question)\n'
            '/new or /reset — start a new conversation\n/image <description> — create an image\n'
            '/export — download recent history\n/forget — clear this chat’s history/settings\n'
            '/balance — check premium and quota\n/claim <code> — redeem premium\n/privacy — data handling\n'
            '/allowgroup and /disallowgroup — group admin controls\n\n'
            'Free: 40 successful questions per 24-hour window; remembers 6 exchanges. '
            'Premium: unlimited questions; remembers 35 exchanges. Images: free users get 1 per prompt; premium users get 4. Image requests use one question. History survives restarts. '
            'In groups, use /ask@queryaibot, reply to the bot, or send ordinary text/photos when Telegram privacy permits.')
    if str(update.effective_user.id) == ADMIN_ID and update.effective_chat.type=='private':
        text += '\n\nAdmin: /settings (global settings and Inline/Guest/Off), /model, /broadcast, /campaigns, /campaign, /ads, /stats, /gencharlie037, /resetcount, /setlogchannel. See README for campaign options.'
    await update.effective_message.reply_text(text)


async def welcome_callback(update,context):
    await update.callback_query.answer()
    action=update.callback_query.data.split(':')[1]
    if action=='settings':await settings_command(update,context)
    elif action=='help':await help_command(update,context)
    elif action=='balance':
        user=user_data_cache.get(str(update.effective_user.id))
        if user:
            normalize_user(user)
            text='Premium · unlimited questions · 4 images per prompt' if user.get('subscription')=='active' else f"{max(0,config.FREE_DAILY_QUOTA-user.get('request_count',0))} questions remaining · 1 image per prompt"
            await update.effective_message.reply_text(text)


async def maintenance():
    while True:
        await asyncio.sleep(3600)
        try:
            backup_user_data()
        except OSError:
            logger.exception('Backup failed')


async def post_init(application):
    global LOG_CHANNEL_ID
    saved = read_json(DATA_DIR / 'bot_settings.json', dict)
    if 'log_channel_id' in saved:
        LOG_CHANNEL_ID = primo.LOG_CHANNEL_ID = saved['log_channel_id']
    initialize_settings(application)
    application.bot_data['active_requests'] = {}
    application.bot_data['chat_store'] = ChatStore(DATA_DIR / 'chats.sqlite3')
    application.bot_data['groq'] = GroqClient(config.GROQ_API_KEY)
    application.bot_data['web'] = FeloClient()
    application.bot_data['images'] = ImageClient()
    application.bot_data['broadcast_manager'] = BroadcastManager(DATA_DIR / 'campaigns.sqlite3', application.bot)
    application.bot_data['maintenance_task'] = asyncio.create_task(maintenance())
    commands = [('start','Start Question Ai'), ('ask','Ask a question'), ('new','New conversation'),
                ('stop','Stop reply'), ('retry','Retry latest answer'), ('image','Create an image'),
                ('web','Search current information'), ('ocr','Read a photo'), ('groupstatus','Check group setup'),
                ('balance','Quota and premium'), ('export','Export conversation'), ('help','Command guide')]
    try:
        await application.bot.set_my_commands([BotCommand(*c) for c in commands])
        await application.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        if ADMIN_ID.isdigit():
            await application.bot.set_my_commands([BotCommand(*c) for c in commands]+[BotCommand('settings','Global admin settings'),BotCommand('model','Model configuration')],scope=BotCommandScopeChat(chat_id=int(ADMIN_ID)))
    except TelegramError:
        logger.warning('Could not set Telegram command menu')


async def post_stop(application):
    tasks = list(application.bot_data.get('active_requests', {}).values())
    maintenance_task = application.bot_data.get('maintenance_task')
    if maintenance_task:
        tasks.append(maintenance_task)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    manager = application.bot_data.get('broadcast_manager')
    if manager:
        await manager.close()


async def post_shutdown(application):
    images=application.bot_data.get('images')
    if images:await images.close()
    web = application.bot_data.get('web')
    if web:
        await web.close()
    groq = application.bot_data.get('groq')
    if groq:
        await groq.close()
    chat_store = application.bot_data.get('chat_store')
    if chat_store:
        chat_store.close()
    flush_cache_to_file()


async def error_handler(update, context):
    logger.error('Update failed: %s', type(context.error).__name__)


def build_application():
    app = (Application.builder().token(config.BOT_TOKEN).concurrent_updates(False)
           .post_init(post_init).post_stop(post_stop).post_shutdown(post_shutdown).build())
    handlers = {'start':start, 'help':help_command, 'image':image_generate_command, 'flux':image_generate_command, 'flux2':image_generate_command, 'ask':ask_command, 'web':web_command, 'ocr':image_command, 'groupstatus':group_status, 'stop':stop_command,
                'retry':retry_command, 'new':reset_conversation, 'reset':reset_conversation,
                'forget':forget_command, 'export':export_command, 'settings':settings_command,
                'model':model_command, 'privacy':privacy_command, 'balance':balance,
                'claim':claim_promo, 'gencharlie037':generate_promo, 'resetcount':reset_all_counts,
                'setlogchannel':set_log_channel, 'allowgroup':allow_group, 'disallowgroup':disallow_group,
                'stats':stats, 'ads':ads, 'broadcast':broadcast, 'campaigns':campaigns, 'campaign':campaign_command}
    for name, handler in handlers.items():
        app.add_handler(CommandHandler(name, handler))
    app.add_handler(TypeHandler(Update,guest_update),group=-1)
    app.add_handler(CallbackQueryHandler(welcome_callback,pattern=r'^welcome:(help|balance|settings)$'))
    app.add_handler(CallbackQueryHandler(admin_callback,pattern=r'^admin:'))
    app.add_handler(ChatMemberHandler(bot_membership_changed,ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(InlineQueryHandler(inline_query))
    app.add_handler(ChosenInlineResultHandler(chosen_result))
    app.add_handler(CallbackQueryHandler(inline_callback,pattern=r'^inl:(run|p[0-9]+):[a-f0-9]+$'))
    app.add_handler(CallbackQueryHandler(chat_callback, pattern=r'^chat:(stop|retry|new|settings|stream|style|reason|math):\d+$'))
    app.add_handler(CallbackQueryHandler(button_callback, pattern=r'^(bc:|refresh_stats)'))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, image_command))
    app.add_handler(MessageHandler(filters.StatusUpdate.MIGRATE, migrate_group))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, handle_group_addition))
    app.add_error_handler(error_handler)
    return app


def main():
    config.validate()
    with FileLock(str(DATA_DIR / 'bot.instance.lock'), timeout=0):
        initialize_cache()
        build_application().run_polling(allowed_updates=sorted(set(Update.ALL_TYPES)|{'guest_message'}))


if __name__ == '__main__':
    main()
