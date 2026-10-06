from config import DATA_DIR, FREE_DAILY_QUOTA
import json
import string
import secrets
import logging
import datetime
import shutil
import glob
import os
from filelock import FileLock
from collections import defaultdict
from pathlib import Path
from telegram.error import TelegramError
from storage import read_json, write_json
from telegram.ext import ContextTypes
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

DATA_DIR.mkdir(parents=True, exist_ok=True)
user_file = str(DATA_DIR / 'user_data.json')
promo_file = str(DATA_DIR / 'promo_codes.json')
GROUP_DATA_FILE = str(DATA_DIR / 'group_data.json')
LOG_CHANNEL_ID = os.environ.get('LOG_CHANNEL_ID', '-1002224010991')
BOT_USERNAME = os.environ.get('BOT_USERNAME', 'queryaibot').lstrip('@')
ADMIN_ID = os.environ.get('ADMIN_ID', '629986639')
user_data_cache = {}


def load_user_data():
    return read_json(user_file, dict, recover=True)


def save_user_data(data):
    if not isinstance(data, dict):
        raise ValueError("User data must be a dictionary")
    write_json(user_file, data)


def load_promo_codes():
    return read_json(promo_file, list)


def save_promo_codes(codes):
    write_json(promo_file, codes)


def save_promo_code(code):
    codes = load_promo_codes()
    expiry = (datetime.date.today() + datetime.timedelta(days=30)).isoformat()
    codes.append({'code': code, 'expiry': expiry, 'used': False})
    save_promo_codes(codes)


def normalize_user(user, now=None):
    """Expire premium and reset a 24-hour quota window before every use."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if user.get('subscription') == 'active':
        try:
            expired = datetime.date.fromisoformat(user.get('sub_end') or '') < now.date()
        except (TypeError, ValueError):
            expired = True
        if expired:
            user.update(subscription='inactive', sub_end=None)
    try:
        count = int(user.get('request_count', 0))
    except (TypeError, ValueError):
        count = 0
    user['request_count'] = max(0, count)
    if user.get('last_request_time') and not user.get('last_activity'):
        user['last_activity'] = user['last_request_time']
    timestamp = user.get('quota_started_at') or user.get('last_request_time')
    if timestamp:
        try:
            started = datetime.datetime.fromisoformat(timestamp)
            if started.tzinfo is None:
                started = started.replace(tzinfo=datetime.timezone.utc)
            if now - started >= datetime.timedelta(hours=24):
                user.update(request_count=0, last_request_time=None, quota_started_at=None)
        except (TypeError, ValueError):
            user.update(request_count=0, last_request_time=None, quota_started_at=None)
    elif user['request_count']:
        user.update(request_count=0, quota_started_at=None)
    return user


def charge_request(user):
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    if not user.get('quota_started_at'):
        user['quota_started_at'] = user.get('last_request_time') or now
    user['request_count'] = user.get('request_count', 0) + 1
    user['last_request_time'] = now
    user['last_activity'] = now
    flush_cache_to_file()


async def notify_log(bot, text):
    if LOG_CHANNEL_ID:
        try:
            await bot.send_message(chat_id=LOG_CHANNEL_ID, text=text)
        except TelegramError:
            logger.warning("Could not deliver admin notification")

def generate_promo_code():
    prefix = 'GPT-'
    suffix = '-GPT'
    random_chars = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8))
    return prefix + random_chars + suffix

async def log_user_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # If command is used in group, prompt for DM
    if update.message.chat.type in ['group', 'supergroup']:
        keyboard = [[InlineKeyboardButton(
            "Start me in DM first", 
            url=f"https://t.me/{BOT_USERNAME}?start=true"
        )]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        message = (
            "⚠️ Please start me in direct message first!\n"
            "Click the button below to start a chat with me in DM."
        )
        await update.message.reply_text(message, reply_markup=reply_markup)
        return

    # Only proceed with user registration if in private chat
    user_id = str(update.message.from_user.id)
    if user_id not in user_data_cache:
        user_data_cache[user_id] = {
            'user_id': user_id,
            'request_count': 0,
            'last_request_time': None,
            'subscription': 'inactive',
            'sub_end': None
        }
        flush_cache_to_file()
        await notify_log(context.bot, f"A new user with user ID {user_id} has started the bot.")
    
    user_data_cache[user_id]['dm_started']=True
    flush_cache_to_file()
    from runtime_settings import get_settings
    mode=get_settings(context)['mode']
    caption = ("<b>Question Ai</b>\n<i>A little curiosity. Endless possibilities.</i>\n\n"
               "<b>Understand</b> — clear answers, step by step.\n"
               "<b>Explore</b> — fresh information from the web.\n"
               "<b>Create</b> — turn your ideas into images.\n\n"
               "Send a question or a photo to begin.")
    if mode=='inline':caption+='\n\nBring me into any conversation: <code>@'+context.bot.username+' your question</code>'
    elif mode=='guest':caption+='\n\nMention <b>@'+context.bot.username+'</b> in a chat and I’ll reply there.'
    rows=[[InlineKeyboardButton('Guide',callback_data='welcome:help'),InlineKeyboardButton('My balance',callback_data='welcome:balance')]]
    if str(user_id)==ADMIN_ID:rows.append([InlineKeyboardButton('Admin settings',callback_data='welcome:settings')])
    elif user_data_cache[user_id].get('subscription')=='active':rows.append([InlineKeyboardButton('My agent settings',callback_data='welcome:settings')])
    markup=InlineKeyboardMarkup(rows)
    banner = Path(__file__).resolve().parent / 'assets' / 'welcome.png'
    if banner.exists():
        try:
            with banner.open('rb') as photo:
                await update.message.reply_photo(photo,caption=caption,parse_mode=ParseMode.HTML,reply_markup=markup)
            return
        except TelegramError:
            logger.warning('Welcome photo could not be sent; using text')
    await update.message.reply_text(caption,parse_mode=ParseMode.HTML,reply_markup=markup)


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    user = user_data_cache.get(user_id)
    if user is None:
        await update.message.reply_text("Please use /start first.")
        return
    normalize_user(user)
    flush_cache_to_file()
    subscription_status = user.get('subscription', 'inactive')

    if subscription_status == 'active':
        subscription_end_str = user.get('sub_end', '')
        subscription_end_date = datetime.datetime.strptime(subscription_end_str, '%Y-%m-%d')
        days_left = (subscription_end_date.date() - datetime.datetime.now(datetime.timezone.utc).date()).days
        
        if days_left < 0:
            user['subscription'] = 'inactive'
            user['request_count'] = 0
            user['sub_end'] = None
            response = (
                f"🟣 **Your Subscription**:\n"
                f"   ⤷ You have no active subscription. Please contact the admin by clicking [here](https://t.me/yucant) to buy. 💬\n"
                f"🟣 **Your Questions Pack**:\n"
                f"   ⤷ Questions left: {max(0, FREE_DAILY_QUOTA - user.get('request_count', 0))}/{FREE_DAILY_QUOTA}"
            )
        else:
            response = (
                f"🟣 **Your Subscription**:\n"
                f"    Subscribed ✅\n"
                f"   ⤷ Days Left: {days_left}\n"
                f"🟣 **Your Questions Pack**:\n"
                f"   ⤷ Questions left: Unlimited ♾️"
            )
    else:
        response = (
            f"🟣 **Your Subscription**:\n"
            f"   ⤷ You have no active subscription. Please contact the admin by clicking [here](https://t.me/yucant) to buy. 💬\n"
            f"🟣 **Your Questions Pack**:\n"
            f"   ⤷ Questions left: {max(0, FREE_DAILY_QUOTA - user.get('request_count', 0))}/{FREE_DAILY_QUOTA}"
        )
    
    await update.message.reply_text(response, parse_mode=ParseMode.MARKDOWN)

async def generate_promo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != ADMIN_ID:
        await update.message.reply_text("Only the bot admin can generate promo codes.")
        return
    promo_code = generate_promo_code()
    save_promo_code(promo_code)
    await update.message.reply_text(
        f"Here is your promo code: `{promo_code}`. Use it to claim your premium subscription. Please copy and paste this: `/claim {promo_code}`.",
        parse_mode=ParseMode.MARKDOWN
    )

async def claim_promo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /claim <code>")
        return
    promo_code = context.args[0].strip().upper()
    user = user_data_cache.get(user_id)
    if user is None:
        await update.message.reply_text("Please use /start first.")
        return
    normalize_user(user)
    subscription_status = user.get('subscription', 'inactive')

    if subscription_status == 'active':
        await update.message.reply_text("You already have an active premium subscription. You cannot claim another promo code.")
        return

    promo_codes = load_promo_codes()
    promo_details = next((item for item in promo_codes if isinstance(item, dict) and item.get('code') == promo_code and not item.get('used', False)), None)
    if promo_details:
        try:
            expiry = datetime.date.fromisoformat(promo_details.get('expiry') or '')
        except (TypeError, ValueError):
            await update.message.reply_text('This promo code has an invalid expiry. Please contact the admin.')
            return
        if datetime.datetime.now(datetime.timezone.utc).date() <= expiry:
            user['subscription'] = 'active'
            user['sub_end'] = promo_details['expiry']
            promo_details['used'] = True
            save_promo_codes(promo_codes)
            flush_cache_to_file()
            await update.message.reply_text("Congratulations! You are now a pro user. Please do /balance to check your status.")
        else:
            await update.message.reply_text("Sorry, this promo code has expired.")
    else:
        await update.message.reply_text("Sorry, the promo code is either invalid or has already been claimed.")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type in ('group','supergroup'):
        register_group(update.effective_chat)
        await welcome_group(update.effective_chat,context,force=True)
        return
    await log_user_data(update,context)

def backup_user_data():
    if os.path.exists(user_file):
        timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        shutil.copy2(user_file, f"{user_file}.{timestamp}.backup")
        for old in sorted(glob.glob(f"{user_file}.*.backup"))[:-5]:
            os.remove(old)


def flush_cache_to_file():
    save_user_data(user_data_cache)


def initialize_cache():
    user_data_cache.clear()
    user_data_cache.update(load_user_data())


async def reset_all_counts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    # Add your admin/owner ID here for security
    
    if user_id != ADMIN_ID:
        await update.message.reply_text("⚠️ Sorry, only the bot admin can use this command.")
        return
        
    try:
        # Reset counts in cache
        for user_data in user_data_cache.values():
            user_data['request_count'] = 0
            user_data['last_request_time'] = None
            user_data['quota_started_at'] = None
            
        # Force an immediate cache flush
        flush_cache_to_file()
        
        await update.message.reply_text(
            "✅ Successfully reset request counts for all users!\n"
            f"📊 Users affected: {len(user_data_cache)}"
        )
        
        # Log the action
        await notify_log(context.bot, f"🔄 Admin ({user_id}) manually reset all user request counts.")
        
    except Exception as e:
        logger.error(f"Error in reset_all_counts: {e}")
        await update.message.reply_text("❌ An error occurred while resetting counts. Please check logs.")

def load_group_data():
    return read_json(GROUP_DATA_FILE, dict)


def save_group_data(data):
    write_json(GROUP_DATA_FILE, data)

def register_group(chat, joined=False):
    groups=load_group_data()
    key=str(chat.id)
    previous=groups.get(key,{})
    now=datetime.datetime.now(datetime.timezone.utc).isoformat()
    info={**previous,'name':getattr(chat,'title',None) or key,
          'added_time':previous.get('added_time',now),
          'link':getattr(chat,'invite_link',None) or previous.get('link') or 'Private Group',
          'is_allowed':True if joined else previous.get('is_allowed',True),
          'is_member':True}
    # A removal/re-add is a new welcome; duplicate Telegram join updates are not.
    if joined and previous.get('is_member') is False:
        info.pop('welcome_sent_at',None)
    groups[key]=info
    save_group_data(groups)
    return info


async def welcome_group(chat,context,force=False):
    groups=load_group_data()
    info=groups.get(str(chat.id),{})
    if info.get('welcome_sent_at') and not force:return
    username=context.bot.username or BOT_USERNAME
    text=('⚡ Thanks for adding Question Ai!\n\n'
          f'This group is saved. Ask with /ask@{username} hello, send a photo, or use /web for current information.\n'
          'New users should open me in private and send /start once.\n'
          'Use /groupstatus for setup help. Group admins can use /disallowgroup or /allowgroup.')
    await context.bot.send_message(chat.id,text,reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton('Open Question Ai',url=f'https://t.me/{username}?start=group')]]))
    # Reload after the await so unrelated group updates cannot be overwritten.
    groups=load_group_data()
    if str(chat.id) in groups:
        groups[str(chat.id)]['welcome_sent_at']=datetime.datetime.now(datetime.timezone.utc).isoformat()
        save_group_data(groups)


async def handle_group_addition(update: Update,context: ContextTypes.DEFAULT_TYPE):
    message=update.effective_message
    if not message or message.chat.type not in ('group','supergroup'):return
    if not any(member.id==context.bot.id for member in message.new_chat_members):return
    register_group(message.chat,joined=True)
    await welcome_group(message.chat,context)


async def bot_membership_changed(update,context):
    change=update.my_chat_member
    if not change or change.chat.type not in ('group','supergroup'):return
    def present(member):
        return member.status in ('member','administrator','creator') or (member.status=='restricted' and member.is_member)
    was,now=present(change.old_chat_member),present(change.new_chat_member)
    if now and not was:
        register_group(change.chat,joined=True)
        await welcome_group(change.chat,context)
    elif was and not now:
        groups=load_group_data()
        if str(change.chat.id) in groups:
            groups[str(change.chat.id)]['is_member']=False
            groups[str(change.chat.id)].pop('welcome_sent_at',None)
            save_group_data(groups)


async def set_group_allowed(update,context,allowed):
    message=update.effective_message
    chat=update.effective_chat
    if chat.type not in ('group','supergroup'):
        await message.reply_text('This command can only be used in groups.');return
    sender=getattr(message,'sender_chat',None)
    anonymous_admin=sender is not None and sender.id==chat.id
    try:
        if not anonymous_admin and str(update.effective_user.id)!=ADMIN_ID:
            member=await context.bot.get_chat_member(chat.id,update.effective_user.id)
            if member.status not in ('administrator','creator'):
                await message.reply_text('Only group administrators can use this command.');return
    except TelegramError:
        logger.warning('Could not verify group administrator')
        await message.reply_text('Could not verify your group-admin status. Make sure the bot is still in this group, then retry.');return
    try:
        register_group(chat)
        groups=load_group_data()
        groups[str(chat.id)]['is_allowed']=allowed
        save_group_data(groups)
    except (OSError,ValueError):
        logger.exception('Could not save group configuration')
        await message.reply_text('Could not save group data. The bot owner should check DATA_DIR permissions and group_data.json.');return
    await message.reply_text('✅ Group enabled. Send /ask hello.' if allowed else 'Group disabled. An admin can re-enable it with /allowgroup.')


async def allow_group(update,context):
    await set_group_allowed(update,context,True)


async def disallow_group(update,context):
    await set_group_allowed(update,context,False)
