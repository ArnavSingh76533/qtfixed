"""Personal premium settings and direct Telegram skill/agent uploads."""
import asyncio
import time
from io import BytesIO
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
import primo
import config
from skill_bundles import parse_bundle, MAX_BUNDLE
from agent_scheduler import timezone, create_reminder
from agent_engine import scope_for
from provider import ProviderError
from documents import LimitedBuffer

def store(context):return context.application.bot_data['agent_store']

def premium(owner):
    user=primo.user_data_cache.get(str(owner))
    if user:primo.normalize_user(user)
    return bool(user and user.get('subscription')=='active')

async def allowed(update,context):
    if not update.effective_user or not update.effective_message:return False
    if update.effective_chat.type!='private':
        await update.effective_message.reply_text('Open my private chat to manage your agent, skills, memory and reminders.');return False
    if not premium(update.effective_user.id):
        await update.effective_message.reply_text('Agent mode and skills are for active bot premium subscribers. Use /balance to check.');return False
    return True

async def settings(update,context,edit=False):
    if not await allowed(update,context):return
    owner=update.effective_user.id;prefs=store(context).prefs(owner)
    keyboard=InlineKeyboardMarkup([
        [InlineKeyboardButton('Agent mode: '+('ON' if prefs['enabled'] else 'OFF'),callback_data='personal:toggle')],
        [InlineKeyboardButton('Every request: '+('ON' if prefs['always_on'] else 'OFF'),callback_data='personal:always')],
        [InlineKeyboardButton('Owner instructions: '+('inherit' if prefs['inherit_defaults'] else 'personal only'),callback_data='personal:inherit')],
        [InlineKeyboardButton('Upload skill',callback_data='personal:skill'),InlineKeyboardButton('Upload agent',callback_data='personal:agent')],
        [InlineKeyboardButton('My skills / agents',callback_data='personal:list')],
        [InlineKeyboardButton('Memory',callback_data='personal:memory'),InlineKeyboardButton('Reminders',callback_data='personal:reminders')]])
    text=('Your premium agent\n\n'+('Enabled' if prefs['enabled'] else 'Disabled')+' in bot DM, groups, inline and guest replies. /agent on uses it for every request; /agent off disables it. /agent auto uses only requests starting with “agent”. '
        'Your choice affects only you; the owner still controls Inline/Guest availability and web access.\n'
        'Timezone: '+prefs['timezone']+' (/timezone to change)\n'
        'Tools: research, calculation, files, memory and reminders. '
        'Code sandbox: '+('configured (Docker must be available)' if config.SANDBOX_ENABLED else 'not enabled by owner')+'\n\n'
        '/skills and /agents manage your uploads. /agentstatus shows the latest task list. '
        'Switching OFF cancels your running requests; reminders pause while off. Resume them with /reminders resume ID.')
    if edit:
        try:await update.callback_query.edit_message_text(text,reply_markup=keyboard)
        except BadRequest as error:
            if 'not modified' not in str(error).lower():raise
    else:await update.effective_message.reply_text(text,reply_markup=keyboard)

async def callback(update,context):
    query=update.callback_query
    # Settings can only be operated by the owner of this private conversation.
    if not update.effective_chat or update.effective_chat.type!='private' or update.effective_chat.id!=query.from_user.id or not premium(query.from_user.id):
        return await query.answer('Active premium and your own bot DM are required.',show_alert=True)
    await query.answer();action=query.data.split(':',1)[1];owner=query.from_user.id
    if action=='settings':return await settings(update,context,edit=True)
    if action=='toggle':
        current=store(context).prefs(owner);store(context).set_pref(owner,enabled=not current['enabled'])
        if current['enabled']:
            from request_queue import cancel
            await cancel(context,owner)
        return await settings(update,context,edit=True)
    if action in ('always','inherit'):
        field='always_on' if action=='always' else 'inherit_defaults'
        current=store(context).prefs(owner);store(context).set_pref(owner,**{field:not current[field]})
        return await settings(update,context,edit=True)
    if action in ('skill','agent'):return await request_upload(update,context,action)
    if action=='list':
        rows=store(context).effective_bundles(owner)
        return await update.effective_message.reply_text('\n'.join(f'{r["kind"]}: {r["name"]} · '+('ON' if r['enabled'] else 'OFF') for r in rows) or 'No skills or agents installed. Use Upload skill/agent.')
    if action=='memory':context.args=[];return await memory_command(update,context)
    if action=='reminders':context.args=[];return await reminders_command(update,context)

async def request_upload(update,context,kind):
    context.application.bot_data.setdefault('agent_uploads',{})[update.effective_user.id]={'kind':kind,'until':time.monotonic()+600}
    await update.effective_message.reply_text('Upload or forward '+('a SKILL.md file (YAML name/description required), or a ZIP containing SKILL.md and its resources.' if kind=='skill' else 'a UTF-8 agent instruction file, or a ZIP containing AGENT.md/AGENTS.md and resources.')+' Maximum 2 MB; send within 10 minutes. It will be installed for your agent only. /skills cancel ends upload mode.')

async def bundle_command(update,context,kind):
    if not await allowed(update,context):return
    args=context.args or [];owner=update.effective_user.id
    if len(args)==2 and args[0]=='default':
        import main
        if not main.is_admin(update):return await update.effective_message.reply_text('Bot owner only.')
        try:store(context).set_default(owner,kind,None if args[1]=='clear' else args[1])
        except ValueError as error:return await update.effective_message.reply_text(str(error))
        return await update.effective_message.reply_text('Owner default saved. Premium users inherit it unless they install a personal resource of this kind or disable inheritance.')
    if args and args[0]=='cancel':
        context.application.bot_data.setdefault('agent_uploads',{}).pop(owner,None)
        return await update.effective_message.reply_text('Upload mode cancelled.')
    if args==['example'] and kind=='skill':
        example=b'---\nname: careful-review\ndescription: Review code for errors and verify fixes with tests.\n---\nRead the supplied code. Identify concrete defects. Fix them, run tests in the sandbox when available, and report what was verified.\n'
        return await update.effective_message.reply_document(BytesIO(example),filename='SKILL.md')
    if len(args)==2 and args[0] in ('enable','disable','remove'):
        changed=store(context).change_bundle(owner,kind,args[1],args[0])
        return await update.effective_message.reply_text('Updated.' if changed else 'No matching upload in your account.')
    if not args or args==['list']:
        rows=store(context).bundles(owner,kind)
        label='/skills' if kind=='skill' else '/agents'
        text='\n'.join(f'{r["name"]} · '+('ON' if r['enabled'] else 'OFF') for r in rows) or 'None installed.'
        return await update.effective_message.reply_text(text+f'\n\n{label} add — upload/forward a file or ZIP\n{label} enable NAME\n{label} disable NAME\n{label} remove NAME\nPersonal resources override owner defaults of the same kind. Owner: {label} default NAME or {label} default clear.')
    if args==['add']:return await request_upload(update,context,kind)
    await update.effective_message.reply_text('Use '+('/skills' if kind=='skill' else '/agents')+' add, list, enable NAME, disable NAME, or remove NAME.')

async def skills_command(update,context):return await bundle_command(update,context,'skill')
async def agents_command(update,context):return await bundle_command(update,context,'agent')

async def document_handler(update,context):
    import main
    message=update.effective_message;owner=update.effective_user.id if update.effective_user else None
    pending=context.application.bot_data.setdefault('agent_uploads',{})
    item=pending.get(owner)
    caption=(message.caption or '').strip().split()
    command=(caption[0].split('@')[0].lower() if caption else '')
    explicit=command in ('/skills','/agents')
    if explicit and '@' in caption[0] and caption[0].partition('@')[2].lower()!=context.bot.username.lower():return
    if explicit or (item and time.monotonic()<item['until'] and update.effective_chat.type=='private'):
        if not await allowed(update,context):return
        kind=('skill' if command=='/skills' else 'agent') if explicit else item['kind']
        doc=message.document
        if (doc.file_size or 0)>MAX_BUNDLE:return await message.reply_text('Maximum skill/agent upload size is 2 MB.')
        # Read over the network outside the update dispatcher; same owner queue prevents races.
        async def install():
            if not premium(owner):return
            try:
                file=await context.bot.get_file(doc.file_id)
                from telegram_delivery import download_into
                raw=LimitedBuffer();await download_into(file,raw)
                bundle=await asyncio.to_thread(parse_bundle,raw.getvalue(),doc.file_name or 'my-agent',kind)
                if not premium(owner):return
                store(context).install(owner,kind,bundle)
                if main.is_admin(update):store(context).set_default(owner,kind,bundle['name'])
                pending.pop(owner,None)
                await message.reply_text(f'Installed your {kind}: {bundle["name"]}. Turn Agent mode ON in /settings to use it. Scripts only run inside the configured sandbox.')
            except (ValueError,ProviderError) as error:await message.reply_text(str(error))
        from request_queue import submit
        if submit(context,owner,'upload',install) is None:await message.reply_text('Your queue is full; try the upload again shortly.')
        return
    if (getattr(message.document,'mime_type',None) or '').startswith('image/'):return await main.image_command(update,context)
    return await main.text_document_command(update,context)

async def memory_command(update,context):
    if not await allowed(update,context):return
    owner=update.effective_user.id;args=context.args or []
    if args==['clear']:store(context).forget(owner,'dm');return await update.effective_message.reply_text('Your private-chat agent memories were cleared.')
    if len(args)==2 and args[0]=='delete':store(context).forget(owner,'dm',args[1]);return await update.effective_message.reply_text('Memory deleted.')
    values=store(context).memories(owner,'dm')
    text='\n'.join(f'{k}: {v}' for k,v in values.items()) or 'No private memories saved.'
    from formatting import formatted_chunks
    for page,_ in formatted_chunks(text+'\n\nAsk “agent remember …” with Agent mode ON. /memory clear or /memory delete NAME. Group/guest memory is isolated from DM memory.'):
        await update.effective_message.reply_text(page)

async def reminders_command(update,context):
    if not await allowed(update,context):return
    owner=update.effective_user.id;args=context.args or []
    if len(args)==2 and args[0]=='cancel':
        found=store(context).cancel_schedule(owner,args[1]);return await update.effective_message.reply_text('Cancelled.' if found else 'No matching reminder.')
    if len(args)==2 and args[0]=='resume':
        job=next((j for j in store(context).schedules(owner) if j['id']==args[1]),None)
        if not job:return await update.effective_message.reply_text('No matching reminder.')
        if not store(context).prefs(owner)['enabled']:return await update.effective_message.reply_text('Enable Agent mode first.')
        if job['status']=='uncertain':return await update.effective_message.reply_text('Delivery was uncertain. Cancel this reminder and create a new one if you still need it, to avoid an accidental duplicate.')
        from agent_scheduler import next_cron
        due=next_cron(job['cron'],job['timezone'],time.time()) if job['cron'] else max(time.time()+10,job['next_run'])
        store(context).schedule_status(job['id'],'active',due)
        return await update.effective_message.reply_text('Reminder resumed.')
    import datetime as dt
    rows=store(context).schedules(owner)
    text='\n'.join(f'{j["id"]} · {j["status"]} · {dt.datetime.fromtimestamp(j["next_run"],timezone(j["timezone"])).isoformat()}\n{j["text"][:160]}' for j in rows) or 'No reminders.'
    from formatting import formatted_chunks
    for page,_ in formatted_chunks(text+'\n\nStart with “agent remind me …” with Agent mode ON. /reminders cancel ID or /reminders resume ID. Reminders arrive in this bot DM; cron schedules send text, never execute host commands.'):
        await update.effective_message.reply_text(page)

async def timezone_command(update,context):
    if not await allowed(update,context):return
    value=' '.join(context.args or [])
    try:timezone(value)
    except ValueError as error:return await update.effective_message.reply_text(str(error))
    store(context).set_pref(update.effective_user.id,timezone=value)
    await update.effective_message.reply_text('Timezone updated for new reminders. Existing reminders keep their saved timezone.')

async def status_command(update,context):
    if not await allowed(update,context):return
    owner=update.effective_user.id
    row=store(context).run(owner,context.args[0]) if context.args else store(context).last_run(owner)
    if not row:return await update.effective_message.reply_text('No agent runs yet. Enable Agent mode in /settings.')
    import json
    plan=json.loads(row['plan'])
    await update.effective_message.reply_text(f'Agent run {row["id"]}: {row["status"]}\n'+'\n'.join(f'{i}. {step["title"]}' for i,step in enumerate(plan,1)))
    from agent_tools import recover_artifacts
    links=recover_artifacts(context,owner,row['id'])
    for path,url in links:
        await update.effective_message.reply_text(path+'\n'+url)
    if not links and store(context).artifacts(owner,row['id']):
        await update.effective_message.reply_text('These download links have expired or the bot restarted. Run the task again to recreate the files.')

async def agent_command(update,context):
    if not await allowed(update,context):return
    args=context.args or []
    if args not in (['on'],['off'],['auto']):
        return await update.effective_message.reply_text('/agent on — every request uses your premium agent\n/agent off — disable\n/agent auto — only “agent …” requests')
    owner=update.effective_user.id;mode=args[0]
    store(context).set_pref(owner,enabled=mode!='off',always_on=mode=='on')
    if mode=='off':
        from request_queue import cancel
        await cancel(context,owner)
    await settings(update,context)
