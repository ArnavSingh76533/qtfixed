import asyncio
import logging
import os
from io import BytesIO
import primo
from storage import read_json, write_json
import config  # Loads .env before primo/broadcast read configuration.
from telegram import (Update, InlineKeyboardButton, InlineKeyboardMarkup, CopyTextButton,
                      LinkPreviewOptions, BotCommand, MenuButtonCommands)
from telegram.error import BadRequest, TelegramError, RetryAfter
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from filelock import FileLock
from primo import (normalize_user, charge_request, initialize_cache, flush_cache_to_file,
                   backup_user_data, user_data_cache, DATA_DIR, BOT_USERNAME, ADMIN_ID,
                   start, balance, generate_promo, claim_promo, reset_all_counts,
                   handle_group_addition, allow_group, disallow_group, load_group_data)
from broadcast import stats, button_callback, broadcast, ads, send_ad_message, campaigns, campaign_command, BroadcastManager
from chat_store import ChatStore
from provider import GroqClient, ProviderError
from formatting import formatted_chunks, units
from streaming import StreamPreview

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
    write_json(DATA_DIR / 'bot_settings.json', {'log_channel_id':value})
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
        if not load_group_data().get(str(update.effective_chat.id), {}).get('is_allowed', False):
            return None
    user = user_data_cache.get(user_id)
    if user is None:
        await update.effective_message.reply_text("Please start me in DM first.", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Start in DM", url=f"https://t.me/{BOT_USERNAME}?start=true")]]))
        return None
    normalize_user(user)
    if user.get('subscription') != 'active':
        if user.get('request_count', 0) >= 20:
            await update.effective_message.reply_text("Daily limit reached (20 questions). Your quota resets 24 hours after the first question in this window.")
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
    return context.application.bot_data['active_requests'].get(user_id)


def answer_keyboard(owner, answer):
    rows = [[InlineKeyboardButton('↻ Retry latest', callback_data=f'chat:retry:{owner}'),
             InlineKeyboardButton('＋ New chat', callback_data=f'chat:new:{owner}')],
            [InlineKeyboardButton('⚙ Settings', callback_data=f'chat:settings:{owner}')]]
    if answer and units(answer) <= 256:
        rows.append([InlineKeyboardButton('Copy answer', copy_text=CopyTextButton(answer))])
    return InlineKeyboardMarkup(rows)


async def deliver_answer(message, answer, owner=None):
    chunks = list(formatted_chunks(answer))
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


async def generate_answer(update, context, user, prompt, retry=False):
    key = history_key(update)
    history, settings = store(context).get(key)
    base = history[:-2] if retry else history
    preview = StreamPreview(update.effective_message, context.bot, update.effective_user.id, settings['streaming'])
    heartbeat = None
    response = ''
    try:
        await preview.start()
        heartbeat = asyncio.create_task(typing_heartbeat(update.effective_message, context.bot))
        async def consume():
            nonlocal response
            async for piece in context.application.bot_data['groq'].stream(
                    provider_messages(base, prompt, settings), reasoning=settings['reasoning']):
                response += piece
                if len(response) > 120000:
                    raise ProviderError('The response was too large. Please ask a narrower question.')
                await preview.update(response)
        await asyncio.wait_for(consume(), timeout=config.REQUEST_TIMEOUT)
        if not response.strip():
            raise ProviderError('Groq returned an empty answer. Please retry.')
        await deliver_answer(update.effective_message, response, update.effective_user.id)
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
        context.application.bot_data['active_requests'].pop(update.effective_user.id, None)


async def launch_question(update, context, retry=False, prompt=None):
    active = context.application.bot_data['active_requests']
    uid = update.effective_user.id
    if uid in active:
        await update.effective_message.reply_text('You already have a reply in progress. Use /stop before asking another question.')
        return
    if len(active) >= config.MAX_CONCURRENT_REQUESTS:
        await update.effective_message.reply_text('The bot is busy. Please try again shortly.')
        return
    user = await eligible_user(update, context)
    if user is None:
        return
    if retry:
        history, _ = store(context).get(history_key(update))
        if len(history) < 2:
            await update.effective_message.reply_text('There is no completed answer to retry in this chat.')
            return
        prompt = history[-2]['content']
    if not prompt or not prompt.strip():
        await update.effective_message.reply_text('Type your question after /ask.')
        return
    task = asyncio.create_task(generate_answer(update, context, user, prompt.strip(), retry))
    active[uid] = task


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
    await launch_question(update, context, prompt=' '.join(context.args))


async def retry_command(update, context):
    await launch_question(update, context, retry=True)


async def stop_command(update, context):
    task = busy(context, update.effective_user.id)
    if task and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    else:
        await update.effective_message.reply_text('No answer is currently running.')


async def reset_conversation(update, context):
    task = busy(context, update.effective_user.id)
    if task and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    store(context).clear(history_key(update))
    await update.effective_message.reply_text('New conversation started. Your settings are kept.')
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


async def settings_command(update, context):
    _, settings = store(context).get(history_key(update))
    uid = update.effective_user.id
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton('Streaming: ' + ('ON' if settings['streaming'] else 'OFF'), callback_data=f'chat:stream:{uid}')],
        [InlineKeyboardButton('Style: '+settings['style'], callback_data=f'chat:style:{uid}')],
        [InlineKeyboardButton('Reasoning: '+settings['reasoning'], callback_data=f'chat:reason:{uid}')]])
    await update.effective_message.reply_text('Chat settings\nModel: '+config.GROQ_MODEL+'\nTap to change. Changes apply to this chat/topic.', reply_markup=keyboard)


async def chat_callback(update, context):
    query = update.callback_query
    _, action, owner = query.data.split(':')
    if str(query.from_user.id) != owner:
        await query.answer('This control belongs to another user.', show_alert=True)
        return
    await query.answer()
    if action in ('stream','style','reason'):
        if busy(context, query.from_user.id):
            await query.message.reply_text('Use /stop before changing settings.')
            return
        key = history_key(update)
        history, settings = store(context).get(key)
        if action == 'stream':
            settings['streaming'] = not settings['streaming']
        else:
            field, values = ('style',['balanced','concise','detailed']) if action == 'style' else ('reasoning',['low','medium','high'])
            settings[field] = values[(values.index(settings[field])+1) % len(values)]
        store(context).save(key, history, settings)
        await settings_command(update, context)
    else:
        handlers = {'stop':stop_command, 'retry':retry_command, 'new':reset_conversation, 'settings':settings_command}
        handler = handlers.get(action)
        if handler:
            await handler(update, context)


async def model_command(update, context):
    await update.effective_message.reply_text(f'Model: {config.GROQ_MODEL}\nProvider: Groq\nThe admin can change GROQ_MODEL in .env.')


async def privacy_command(update, context):
    await update.effective_message.reply_text(
        'Text questions and recent history are sent to Groq. Conversation history and settings are stored locally, isolated by user, chat, and topic. '
        + ('The admin has question logging enabled. ' if LOG_CHANNEL_ID else 'Question logging is disabled. ')
        + 'Use /export to download saved history, /new to clear it, or /forget to also reset chat settings. Quota, registration, and any existing admin logs remain.')


async def help_command(update, context):
    text = ('Question Ai • Text assistant\n\n'
            '/ask <question> — ask in any enabled chat\n'
            '/stop — stop the current answer\n/retry — regenerate the latest answer (uses one question)\n'
            '/new or /reset — start a new conversation\n/settings — streaming, style, reasoning effort\n'
            '/model — show the configured model\n/export — download recent history\n/forget — clear this chat’s history/settings\n'
            '/balance — check premium and quota\n/claim <code> — redeem premium\n/privacy — data handling\n'
            '/allowgroup and /disallowgroup — group admin controls\n\n'
            'Free: 20 successful questions per 24-hour window; remembers 6 exchanges. '
            'Premium: unlimited questions; remembers 35 exchanges. History survives restarts. '
            'In groups, mention the bot, reply to it, or use /ask.')
    if str(update.effective_user.id) == ADMIN_ID:
        text += '\n\nAdmin: /broadcast, /campaigns, /campaign, /ads, /stats, /gencharlie037, /resetcount, /setlogchannel. See README for campaign options.'
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
    application.bot_data['active_requests'] = {}
    application.bot_data['chat_store'] = ChatStore(DATA_DIR / 'chats.sqlite3')
    application.bot_data['groq'] = GroqClient(config.GROQ_API_KEY)
    application.bot_data['broadcast_manager'] = BroadcastManager(DATA_DIR / 'campaigns.sqlite3', application.bot)
    application.bot_data['maintenance_task'] = asyncio.create_task(maintenance())
    commands = [('start','Start Question Ai'), ('ask','Ask a question'), ('new','New conversation'),
                ('stop','Stop reply'), ('retry','Retry latest answer'), ('settings','Chat settings'),
                ('balance','Quota and premium'), ('export','Export conversation'), ('help','Command guide')]
    try:
        await application.bot.set_my_commands([BotCommand(*c) for c in commands])
        await application.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
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
    handlers = {'start':start, 'help':help_command, 'ask':ask_command, 'stop':stop_command,
                'retry':retry_command, 'new':reset_conversation, 'reset':reset_conversation,
                'forget':forget_command, 'export':export_command, 'settings':settings_command,
                'model':model_command, 'privacy':privacy_command, 'balance':balance,
                'claim':claim_promo, 'gencharlie037':generate_promo, 'resetcount':reset_all_counts,
                'setlogchannel':set_log_channel, 'allowgroup':allow_group, 'disallowgroup':disallow_group,
                'stats':stats, 'ads':ads, 'broadcast':broadcast, 'campaigns':campaigns, 'campaign':campaign_command}
    for name, handler in handlers.items():
        app.add_handler(CommandHandler(name, handler))
    app.add_handler(CallbackQueryHandler(chat_callback, pattern=r'^chat:(stop|retry|new|settings|stream|style|reason):\d+$'))
    app.add_handler(CallbackQueryHandler(button_callback, pattern=r'^(bc:|refresh_stats)'))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, handle_group_addition))
    app.add_error_handler(error_handler)
    return app


def main():
    config.validate()
    with FileLock(str(DATA_DIR / 'bot.instance.lock'), timeout=0):
        initialize_cache()
        build_application().run_polling()


if __name__ == '__main__':
    main()
