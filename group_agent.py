"""One bot-owner system prompt for all group and guest conversations."""
import time
import config
from storage import read_json, write_json
from documents import read_text_document
from provider import ProviderError

PROMPT_CHARS = 60000


def instructions(context, chat_id=None):
    from runtime_settings import get_settings
    if not get_settings(context)['group_agent']:return ''
    return context.application.bot_data.get('external_system_prompt','')


def initialize(application):
    saved=read_json(config.DATA_DIR/'system_prompt.json',dict)
    # Retain the former global prompt on upgrade; leave all old records untouched.
    text=saved.get('text') if 'text' in saved else read_json(config.DATA_DIR/'group_agents.json',dict).get('all','')
    application.bot_data['external_system_prompt']=text if isinstance(text,str) else ''


def save_prompt(context,text):
    write_json(config.DATA_DIR/'system_prompt.json',{'text':text})
    context.application.bot_data['external_system_prompt']=text
    context.application.bot_data.pop('pending_system_prompt',None)


def awaiting(context,update):
    import main
    pending=context.application.bot_data.get('pending_system_prompt') or {}
    return (main.is_admin(update) and pending.get('chat_id')==update.effective_chat.id
            and time.monotonic()<pending.get('until',0))


async def request_upload(update,context):
    context.application.bot_data['pending_system_prompt']={'chat_id':update.effective_chat.id,'until':time.monotonic()+600}
    await update.effective_message.reply_text('Send a UTF-8 text file with any filename in this chat within 10 minutes. Its text replaces the system prompt for all group and guest replies. Normal bot DMs keep the default. /agents clear restores the default.')


async def agents_command(update,context):
    import main
    if not main.is_admin(update):return
    args=context.args or []
    if args:
        if args==['clear']:
            save_prompt(context,'')
            return await update.effective_message.reply_text('Default system prompt restored for groups and guest mode.')
        if args==['status']:
            return await update.effective_message.reply_text('System prompt: '+('custom (groups and guest mode).' if instructions(context) else 'default.'))
        return await update.effective_message.reply_text('Use /agents, /agents status, or /agents clear.')
    message=update.effective_message
    source=message if getattr(message,'document',None) else message.reply_to_message
    doc=getattr(source,'document',None)
    if not doc:return await request_upload(update,context)
    try:text=await read_text_document(context.bot,doc,max_chars=PROMPT_CHARS)
    except ProviderError as error:return await message.reply_text(str(error))
    save_prompt(context,text)
    # Installing a prompt also activates it; the admin can toggle it in settings.
    from runtime_settings import get_settings,save_settings
    settings=get_settings(context);settings['group_agent']=True;save_settings(context,settings)
    await message.reply_text('Custom system prompt saved for every group and guest reply. Normal bot DMs still use the default. Change or disable it in /settings; /agents clear restores the default.')


async def uploaded_document(update,context):
    """One dispatcher for prompt uploads and normal .txt question attachments."""
    import main
    message=update.effective_message
    caption=(message.caption or '').strip()
    parts=caption.split()
    command=parts[0] if parts else ''
    explicit=command.split('@')[0].lower() in ('/agents','/agent')
    if explicit:
        suffix=command.partition('@')[2]
        if suffix and suffix.lower()!=context.bot.username.lower():return
    if explicit or awaiting(context,update):
        if not main.is_admin(update):return
        context.args=parts[1:] if explicit else []
        return await agents_command(update,context)
    if (getattr(message.document,'mime_type',None) or '').startswith('image/'):
        return await main.image_command(update,context)
    return await main.text_document_command(update,context)

# Existing command remains a compatibility alias.
agent_command=agents_command
agent_upload=uploaded_document
