from config import DATA_DIR
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
    
    await update.message.reply_text(
        'Welcome to Question Ai! 🤖 I\'m here to assist you with all sorts of questions, from math and science to general knowledge and programming. '
        'To get started, just send me your questions in text format and ask for help. Let\'s embark on a learning journey together! 🚀'
    )

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
                f"   ⤷ Questions left: {max(0, 20 - user.get('request_count', 0))}/20"
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
            f"   ⤷ Questions left: {max(0, 20 - user.get('request_count', 0))}/20"
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
    # If in group, show group welcome message
    if update.message.chat.type in ['group', 'supergroup']:
        welcome_message = (
            "Thanks for adding me! 👋\n"
            "To use me in this group, please:\n"
            "1️⃣ Start me in DM first (click button below)\n"
            "2️⃣ Group admin must use /allowgroup to enable group usage\n\n"
            "❗️ Group admins can use /disallowgroup to disable group usage"
        )
        await update.message.reply_text(welcome_message)
        return
    
    # If in private chat, proceed with normal user registration
    await log_user_data(update, context)

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

async def handle_group_addition(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Skip if not a group chat
    if update.message.chat.type not in ['group', 'supergroup']:
        return

    if not any(member.id == context.bot.id for member in update.message.new_chat_members):
        return
    chat = update.message.chat
    chat_id = str(chat.id)
    
    # Load existing group data
    groups = load_group_data()
    
    # Check if group is already registered
    if chat_id not in groups:
        group_info = {
            'name': chat.title,
            'added_time': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'link': chat.invite_link if chat.invite_link else 'Private Group',
            'is_allowed': False
        }
        groups[chat_id] = group_info
        save_group_data(groups)
        
        # Send notification to log channel
        log_message = (
            f"🔔 Bot added to new group!\n"
            f"📝 Group Name: {chat.title}\n"
            f"🆔 Group ID: {chat_id}\n"
            f"🔗 Invite Link: {chat.invite_link if chat.invite_link else 'Private Group'}\n"
            f"⏰ Added Time: {group_info['added_time']}"
        )
        await notify_log(context.bot, log_message)
        
        # Create "Start in DM" button
        keyboard = [[InlineKeyboardButton(
            "Start me in DM first", 
            url=f"https://t.me/{BOT_USERNAME}?start=true"
        )]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # Send welcome message in group
        welcome_message = (
            "Thanks for adding me! 👋\n"
            "To use me in this group, please:\n"
            "1️⃣ Start me in DM first (click button below)\n"
            "2️⃣ Group admin must use /allowgroup to enable group usage\n\n"
            "❗️ Group admins can use /disallowgroup to disable group usage"
        )
        await update.message.reply_text(welcome_message, reply_markup=reply_markup)

async def allow_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("This command can only be used in groups!")
        return
        
    # Check if user is admin
    user_id = update.effective_user.id
    chat_id = str(update.message.chat.id)
    chat = update.message.chat
    
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        if member.status not in ['creator', 'administrator']:
            await update.message.reply_text("⚠️ Only group administrators can use this command!")
            return
            
        groups = load_group_data()
        if chat_id not in groups:
            # Add group data if not present
            groups[chat_id] = {
                'name': chat.title,
                'added_time': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'link': chat.invite_link if chat.invite_link else 'Private Group',
                'is_allowed': True  # Set to True immediately
            }
            save_group_data(groups)
            await update.message.reply_text("✅ Bot has been enabled for this group!")
        else:
            groups[chat_id]['is_allowed'] = True
            save_group_data(groups)
            await update.message.reply_text("✅ Bot has been enabled for this group!")
            
    except Exception as e:
        logger.error(f"Error in allow_group: {e}")
        await update.message.reply_text("An error occurred. Please try again later.")

async def disallow_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("This command can only be used in groups!")
        return
        
    # Check if user is admin
    user_id = update.effective_user.id
    chat_id = str(update.message.chat.id)
    
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        if member.status not in ['creator', 'administrator']:
            await update.message.reply_text("⚠️ Only group administrators can use this command!")
            return
            
        groups = load_group_data()
        if chat_id in groups:
            groups[chat_id]['is_allowed'] = False
            save_group_data(groups)
            await update.message.reply_text("❌ Bot has been disabled for this group!")
        else:
            await update.message.reply_text("❌ Please remove and add the bot to the group again!")
            
    except Exception as e:
        logger.error(f"Error in disallow_group: {e}")
        await update.message.reply_text("An error occurred. Please try again later.")
