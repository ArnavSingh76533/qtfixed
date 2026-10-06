"""Bot-owner managed group instructions; uploaded markdown is never executed."""
import config
from storage import read_json, write_json
from documents import read_text_document
from provider import ProviderError


def instructions(context, chat_id):
    from runtime_settings import get_settings
    if not get_settings(context)['group_agent']: return ''
    saved=context.application.bot_data.get('group_agents',{})
    return saved.get(str(chat_id),saved.get('all',''))


def initialize(application):
    saved=read_json(config.DATA_DIR/'group_agents.json',dict)
    application.bot_data['group_agents']={str(k):v for k,v in saved.items() if isinstance(v,str) and len(v)<=16000}


async def agent_command(update,context):
    import main
    if not main.is_admin(update):return
    message=update.effective_message
    args=list(context.args or [])
    target=str(update.effective_chat.id) if update.effective_chat.type in ('group','supergroup') else 'all'
    if args and (args[0]=='all' or (args[0].startswith('-') and args[0][1:].isdigit())):
        target=args.pop(0)
    action=args[0].lower() if args else ''
    saved=dict(context.application.bot_data.get('group_agents',{}))
    if action=='clear':
        saved.pop(target,None)
    elif action=='status':
        exists=bool(saved.get(target))
        return await message.reply_text(f'Group agent ({target}): '+('custom instructions saved.' if exists else 'using default system prompt.'))
    elif action:
        return await message.reply_text('Use /agent [all|-group_id] [status|clear], or reply to agent.md with /agent.')
    else:
        source=message if getattr(message,'document',None) else message.reply_to_message
        doc=getattr(source,'document',None)
        if not doc or (doc.file_name or '').lower()!='agent.md':
            return await message.reply_text('Upload agent.md with caption /agent, or reply to it with /agent. In DM it applies to all groups; /agent -group_id targets one group. /agent clear restores the default.')
        try:saved[target]=await read_text_document(context.bot,doc,max_chars=16000)
        except ProviderError as error:return await message.reply_text(str(error))
    write_json(config.DATA_DIR/'group_agents.json',saved)
    context.application.bot_data['group_agents']=saved
    await message.reply_text(f'Group agent ({target}) '+('reset to default.' if action=='clear' else 'instructions saved.'))


async def agent_upload(update,context):
    # Caption commands are not dispatched by PTB's normal CommandHandler.
    import main
    if not main.is_admin(update):return
    message=update.effective_message
    caption=(message.caption or '').strip()
    command,*args=caption.split()
    if command.split('@')[0].lower()!='/agent':return
    suffix=command.partition('@')[2]
    if suffix and suffix.lower()!=context.bot.username.lower():return
    context.args=args
    await agent_command(update,context)
