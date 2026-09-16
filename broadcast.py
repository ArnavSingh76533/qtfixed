"""Persistent, admin-only broadcast campaigns. Never starts sending on import."""
import asyncio
import datetime as dt
import json
import logging
import random
import shlex
import sqlite3
import uuid
from urllib.parse import urlparse
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError
from primo import user_data_cache, load_group_data, ADMIN_ID, DATA_DIR, normalize_user
from storage import read_json, write_json
from formatting import formatted_chunks, units

logger = logging.getLogger(__name__)


def markup(buttons):
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, url=url)] for label, url in buttons]) if buttons else None


def parse_broadcast(text, is_reply=False):
    """Flags go before --; message body keeps its original newlines and spacing."""
    raw = text.partition(' ')[2].strip()
    prefix, separator, body = raw.partition(' -- ')
    if not separator and raw.startswith('-- '):
        prefix, body, separator = '', raw[3:], ' -- '
    tokens = shlex.split(prefix)
    options = {'audience':[], 'segment':'all', 'days':None, 'random':None,
               'delay':1.0, 'silent':False, 'pin':False, 'buttons':[]}
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in ('-user','-group'):
            options['audience'].append(token[1:])
        elif token in ('-premium','-free'):
            if options['segment'] != 'all':
                raise ValueError('Choose only one of -premium or -free.')
            options['segment'] = token[1:]
        elif token in ('-silent','-pin'):
            options[token[1:]] = True
        elif token in ('-active','-random','-delay','--button'):
            i += 1
            if i >= len(tokens):
                if token == '-delay':  # Original flag remains valid.
                    options['delay'] = 1.0; break
                raise ValueError(f'{token} requires a value.')
            value = tokens[i]
            if token == '--button':
                label, sep, url = value.partition('|')
                if not sep or not label.strip() or urlparse(url.strip()).scheme not in ('https','http','tg'):
                    raise ValueError('Button format: --button "Title|https://example.com"')
                options['buttons'].append([label.strip(), url.strip()])
            elif token == '-delay':
                try:
                    options['delay'] = float(value)
                except ValueError:
                    i -= 1; options['delay'] = 1.0
            else:
                number = int(value)
                if number < 1:
                    raise ValueError('Counts and days must be positive.')
                options['days' if token == '-active' else 'random'] = number
        elif token.startswith('r') and token[1:].isdigit():
            options['random'] = int(token[1:])
            if options['random'] < 1:
                raise ValueError('Random count must be positive.')
        elif not separator and not token.startswith('-'):
            body = ' '.join(tokens[i:]); break
        else:
            raise ValueError(f'Unknown flag: {token}. Separate message text with --.')
        i += 1
    if not options['audience']:
        raise ValueError('Choose -user, -group, or both.')
    if not 0.1 <= options['delay'] <= 30:
        raise ValueError('Delay must be between 0.1 and 30 seconds.')
    if len(options['buttons']) > 8:
        raise ValueError('Use at most 8 buttons.')
    if not body.strip() and not is_reply:
        raise ValueError('Add -- followed by text, or reply to a message to copy.')
    if body.strip() and is_reply:
        # Explicit text takes priority over the replied-to message.
        is_reply = False
    if units(body) > 3500:
        raise ValueError('Use at most 3500 text units per campaign message.')
    options['text'] = body.strip()
    options['copy'] = is_reply
    return options


class BroadcastManager:
    def __init__(self, path, bot):
        self.bot = bot
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS campaigns(id TEXT PRIMARY KEY, payload TEXT NOT NULL, state TEXT NOT NULL,
                created TEXT NOT NULL, admin_chat INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS recipients(campaign TEXT, chat TEXT, state TEXT NOT NULL, error TEXT,
                PRIMARY KEY(campaign,chat));
            CREATE TABLE IF NOT EXISTS blocked(chat TEXT PRIMARY KEY, reason TEXT);
        ''')
        self.db.execute("UPDATE recipients SET state='uncertain', error='Restart during delivery' WHERE state='sending'")
        self.db.execute("UPDATE campaigns SET state='paused' WHERE state='running'")
        self.db.commit()
        self.worker = None

    def targets(self, options):
        result = set()
        now = dt.datetime.now(dt.timezone.utc)
        if 'user' in options['audience']:
            for uid, original in user_data_cache.items():
                if original.get('dm_started') is False:continue
                user = normalize_user(original.copy(), now)
                premium = user.get('subscription') == 'active'
                if options['segment'] == 'premium' and not premium:
                    continue
                if options['segment'] == 'free' and premium:
                    continue
                if options['days']:
                    try:
                        last = dt.datetime.fromisoformat(original.get('last_activity') or original.get('last_request_time') or '')
                        if last.tzinfo is None:
                            last = last.replace(tzinfo=dt.timezone.utc)
                        if now-last > dt.timedelta(days=options['days']):
                            continue
                    except ValueError:
                        continue
                result.add(uid)
        if 'group' in options['audience']:
            result.update(k for k,v in load_group_data().items() if v.get('is_allowed') and v.get('is_member',True))
        blocked = {r[0] for r in self.db.execute('SELECT chat FROM blocked')}
        result = sorted(result-blocked)
        if options['random'] is not None:
            result = random.sample(result, min(len(result), options['random']))
        return result

    def create(self, payload, targets, admin_chat):
        campaign_id = uuid.uuid4().hex[:12]
        with self.db:
            self.db.execute('INSERT INTO campaigns VALUES (?,?,?,?,?)',
                (campaign_id, json.dumps(payload), 'draft', dt.datetime.now(dt.timezone.utc).isoformat(), admin_chat))
            self.db.executemany('INSERT INTO recipients VALUES (?,?,?,NULL)',
                [(campaign_id,str(t),'pending') for t in targets])
        return campaign_id

    def get(self, campaign_id):
        row = self.db.execute('SELECT * FROM campaigns WHERE id=?', (campaign_id,)).fetchone()
        return dict(row) if row else None

    def counts(self, campaign_id):
        return dict(self.db.execute('SELECT state,COUNT(*) FROM recipients WHERE campaign=? GROUP BY state', (campaign_id,)))

    def summary(self, campaign_id):
        row = self.get(campaign_id)
        if not row:
            return 'Campaign not found.'
        counts = self.counts(campaign_id)
        return (f"Campaign {campaign_id} • {row['state']}\n"
                f"Total: {sum(counts.values())} | Sent: {counts.get('sent',0)} | Pending: {counts.get('pending',0)}\n"
                f"Failed: {counts.get('failed',0)} | Blocked/skipped: {counts.get('skipped',0)} | Uncertain: {counts.get('uncertain',0)}\n"
                'Uncertain deliveries are never retried automatically.')

    def transition(self, campaign_id, action):
        row = self.get(campaign_id)
        if not row:
            raise ValueError('Campaign not found.')
        state = row['state']
        if action == 'start':
            if state not in ('draft','paused'):
                raise ValueError('Only draft or paused campaigns can start.')
            running = self.db.execute("SELECT id FROM campaigns WHERE state='running'").fetchone()
            if running:
                raise ValueError('Pause the running campaign first.')
            if self.worker and not self.worker.done():
                raise ValueError('The previous delivery is finishing. Try again shortly.')
            with self.db:
                self.db.execute("UPDATE campaigns SET state='running' WHERE id=?", (campaign_id,))
            self.worker = asyncio.create_task(self.run(campaign_id))
        elif action in ('pause','cancel'):
            if state in ('completed','cancelled'):
                raise ValueError('This campaign is already finished.')
            with self.db:
                self.db.execute('UPDATE campaigns SET state=? WHERE id=?',
                    ('paused' if action == 'pause' else 'cancelled', campaign_id))
        elif action == 'retry_failed':
            if state == 'running' or (self.worker and not self.worker.done()):
                raise ValueError('Pause and wait for the current delivery first.')
            if state == 'cancelled':
                raise ValueError('A cancelled campaign cannot be restarted.')
            with self.db:
                self.db.execute("UPDATE recipients SET state='pending',error=NULL WHERE campaign=? AND state='failed'", (campaign_id,))
                self.db.execute("UPDATE campaigns SET state='paused' WHERE id=?", (campaign_id,))
        else:
            raise ValueError('Use status, start, pause, resume, cancel, retry_failed, or report.')

    async def send(self, target, payload):
        kwargs = {'chat_id':target, 'disable_notification':payload['silent'],
                  'reply_markup':markup(payload['buttons'])}
        if payload['copy']:
            result = await self.bot.copy_message(from_chat_id=payload['source_chat'],
                message_id=payload['source_message'], **kwargs)
        else:
            text, entities = next(formatted_chunks(payload['text']))
            try:
                result = await self.bot.send_message(text=text, entities=entities,
                    link_preview_options=LinkPreviewOptions(is_disabled=True), **kwargs)
            except BadRequest as error:
                if 'entit' not in str(error).lower():
                    raise
                result = await self.bot.send_message(text=text, **kwargs)
        if payload['pin']:
            try:
                await self.bot.pin_chat_message(target, result.message_id, disable_notification=True)
            except TelegramError:
                # The message is delivered even if pin permission is missing.
                logger.warning('Campaign message sent, but pin failed')
        return result

    def recipient_state(self, campaign_id, target, state, error=None):
        with self.db:
            self.db.execute('UPDATE recipients SET state=?,error=? WHERE campaign=? AND chat=?',
                            (state,error,campaign_id,str(target)))

    async def run(self, campaign_id):
        row = self.get(campaign_id)
        payload = json.loads(row['payload'])
        try:
            while self.get(campaign_id)['state'] == 'running':
                target = self.db.execute("SELECT chat FROM recipients WHERE campaign=? AND state='pending' ORDER BY chat LIMIT 1", (campaign_id,)).fetchone()
                if not target:
                    with self.db:
                        self.db.execute("UPDATE campaigns SET state='completed' WHERE id=?", (campaign_id,))
                    await self.bot.send_message(row['admin_chat'], self.summary(campaign_id))
                    return
                target = target[0]
                # Recheck group permission and blocked status at delivery time.
                if self.db.execute('SELECT 1 FROM blocked WHERE chat=?',(target,)).fetchone() or (target.startswith('-') and not (load_group_data().get(target,{}).get('is_allowed') and load_group_data().get(target,{}).get('is_member',True))):
                    self.recipient_state(campaign_id,target,'skipped','Blocked or disabled')
                    continue
                self.recipient_state(campaign_id,target,'sending')
                try:
                    await self.send(target,payload)
                    self.recipient_state(campaign_id,target,'sent')
                except RetryAfter as error:
                    self.recipient_state(campaign_id,target,'pending','Rate limited')
                    delay = error.retry_after
                    await asyncio.sleep((delay.total_seconds() if hasattr(delay,'total_seconds') else delay)+1)
                    continue
                except Forbidden:
                    self.recipient_state(campaign_id,target,'skipped','Bot blocked or no access')
                    with self.db:
                        self.db.execute('INSERT OR REPLACE INTO blocked VALUES (?,?)',(target,'No access'))
                except BadRequest:
                    self.recipient_state(campaign_id,target,'failed','BadRequest')
                except NetworkError:
                    self.recipient_state(campaign_id,target,'uncertain','Network outcome unknown')
                except TelegramError as error:
                    self.recipient_state(campaign_id,target,'failed',type(error).__name__)
                await asyncio.sleep(payload['delay'])
        except asyncio.CancelledError:
            with self.db:
                self.db.execute("UPDATE recipients SET state='uncertain',error='Interrupted delivery' WHERE campaign=? AND state='sending'", (campaign_id,))
                self.db.execute("UPDATE campaigns SET state='paused' WHERE id=? AND state='running'", (campaign_id,))
            raise
        except Exception:
            logger.exception('Campaign worker stopped')
            with self.db:
                self.db.execute("UPDATE campaigns SET state='paused' WHERE id=? AND state='running'",(campaign_id,))
                self.db.execute("UPDATE recipients SET state='uncertain' WHERE campaign=? AND state='sending'",(campaign_id,))

    async def close(self):
        if self.worker and not self.worker.done():
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        self.db.close()


def manager(context):
    return context.application.bot_data['broadcast_manager']


async def admin_only(update):
    if str(update.effective_user.id) == ADMIN_ID:
        return True
    if update.callback_query:
        await update.callback_query.answer('Only the bot admin can use this.', show_alert=True)
    else:
        await update.effective_message.reply_text('Only the bot admin can use this command.')
    return False


def campaign_keyboard(cid):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('▶ Start / resume',callback_data=f'bc:start:{cid}',style='success'),
         InlineKeyboardButton('⏸ Pause',callback_data=f'bc:pause:{cid}')],
        [InlineKeyboardButton('Refresh status',callback_data=f'bc:status:{cid}'),
         InlineKeyboardButton('Cancel',callback_data=f'bc:cancel:{cid}',style='danger')]])


async def broadcast(update, context):
    if not await admin_only(update):
        return
    try:
        message = update.effective_message
        payload = parse_broadcast(message.text, bool(message.reply_to_message))
        if payload['copy']:
            payload.update(source_chat=message.chat_id, source_message=message.reply_to_message.message_id)
        targets = manager(context).targets(payload)
        if not targets:
            raise ValueError('No eligible recipients match these options.')
        # Show the exact outgoing content only to the requesting admin chat.
        await manager(context).send(message.chat_id, {**payload, 'pin':False, 'silent':True})
        cid = manager(context).create(payload, targets, message.chat_id)
        await message.reply_text('Preview above. Sending begins only when you press Start.\n'+manager(context).summary(cid),
                                 reply_markup=campaign_keyboard(cid))
    except (ValueError, TelegramError) as error:
        text = str(error) if isinstance(error,ValueError) else 'Telegram could not create the preview. Check message access and button links.'
        await update.effective_message.reply_text(text+'\nExample: /broadcast -user -- **Hello!**\nUse /help or README for all options.')


async def campaigns(update, context):
    if not await admin_only(update):
        return
    rows = manager(context).db.execute('SELECT id,state,created FROM campaigns ORDER BY created DESC LIMIT 15').fetchall()
    text = '\n'.join(f"{r['id']} • {r['state']} • {r['created'][:10]}" for r in rows)
    await update.effective_message.reply_text(text or 'No campaigns yet. Use /broadcast.')


async def campaign_command(update, context):
    if not await admin_only(update):
        return
    if len(context.args) != 2:
        await update.effective_message.reply_text('Usage: /campaign <id> status|start|pause|resume|cancel|retry_failed|report')
        return
    cid, action = context.args
    if action == 'report':
        import csv
        from io import StringIO, BytesIO
        output = StringIO();writer=csv.writer(output);writer.writerow(['chat_id','status','error'])
        writer.writerows(manager(context).db.execute('SELECT chat,state,error FROM recipients WHERE campaign=?',(cid,)))
        await update.effective_message.reply_document(BytesIO(output.getvalue().encode()), filename=f'campaign-{cid[:20]}.csv')
        return
    try:
        if action != 'status':
            manager(context).transition(cid, 'start' if action == 'resume' else action)
        await update.effective_message.reply_text(manager(context).summary(cid),reply_markup=campaign_keyboard(cid))
    except ValueError as error:
        await update.effective_message.reply_text(str(error))


async def button_callback(update, context):
    if not await admin_only(update):
        return
    query=update.callback_query
    await query.answer()
    if query.data == 'refresh_stats':
        await stats(update,context);return
    _, action, cid = query.data.split(':')
    try:
        if action != 'status':
            manager(context).transition(cid,action)
        await query.edit_message_text(manager(context).summary(cid),reply_markup=campaign_keyboard(cid))
    except ValueError as error:
        await query.message.reply_text(str(error))
    except BadRequest as error:
        if 'not modified' not in str(error).lower():
            raise


async def stats(update, context):
    if not await admin_only(update):
        return
    now=dt.datetime.now(dt.timezone.utc)
    premium=sum(normalize_user(u.copy(),now).get('subscription')=='active' for u in user_data_cache.values())
    groups=load_group_data()
    await update.effective_message.reply_text(
        f'Bot statistics\nUsers: {len(user_data_cache)}\nActive premium: {premium}\n'
        f'Groups: {len(groups)} ({sum(bool(g.get("is_allowed")) for g in groups.values())} enabled)\n'
        f'AI requests in progress: {len(context.application.bot_data.get("active_requests",{}))}',
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('Refresh',callback_data='refresh_stats')]]))


async def ads(update, context):
    if not await admin_only(update):
        return
    text=update.effective_message.text.partition(' ')[2].strip()
    if text == 'off':
        write_json(DATA_DIR/'ads.json',{})
        await update.effective_message.reply_text('Ads disabled.');return
    if not text or units(text)>3500:
        await update.effective_message.reply_text('Usage: /ads <Markdown text> or /ads off');return
    write_json(DATA_DIR/'ads.json',{'message':text})
    for chunk,entities in formatted_chunks(text):
        await update.effective_message.reply_text(chunk,entities=entities)
    await update.effective_message.reply_text('Saved. Shown after /new or /reset.')


async def send_ad_message(bot, chat_id):
    try:
        saved=read_json(DATA_DIR/'ads.json',dict)
        if saved.get('message'):
            for chunk,entities in formatted_chunks(saved['message']):
                await bot.send_message(chat_id,chunk,entities=entities)
    except (ValueError,TelegramError):
        logger.warning('Could not deliver ad')
