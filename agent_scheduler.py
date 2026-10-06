"""Persistent reminder delivery; cron expressions never become operating-system jobs."""
import asyncio
import datetime as dt
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from croniter import croniter
from telegram.error import Forbidden, NetworkError, RetryAfter, TelegramError
from telegram_delivery import log_error

def timezone(name):
    try:return ZoneInfo(name)
    except (ValueError,TypeError,ZoneInfoNotFoundError):raise ValueError('Use an IANA timezone such as Asia/Kolkata or Europe/London.') from None

def next_cron(expression,tz,now):
    if len(expression.split())!=5 or not croniter.is_valid(expression):raise ValueError('Use a five-field cron expression: minute hour day month weekday.')
    start=dt.datetime.fromtimestamp(now,timezone(tz))
    iterator=croniter(expression,start,max_years_between_matches=2)
    first=iterator.get_next(dt.datetime).timestamp()
    second=iterator.get_next(dt.datetime).timestamp()
    if second-first<300:raise ValueError('Recurring reminders must be at least five minutes apart.')
    return first

def create_reminder(store,owner,text,when=None,cron=None,tz=None,now=None):
    now=time.time() if now is None else now
    if not isinstance(text,str) or not text.strip() or len(text)>2000:raise ValueError('Reminder text must contain 1–2,000 characters.')
    tz=tz or store.prefs(owner)['timezone'];zone=timezone(tz)
    if bool(when)==bool(cron):raise ValueError('Choose either one ISO date/time or a cron expression.')
    if cron:due=next_cron(cron,tz,now)
    else:
        try:stamp=dt.datetime.fromisoformat(when.replace('Z','+00:00'))
        except (ValueError,AttributeError):raise ValueError('Use an ISO date/time, for example 2026-10-07T18:30:00+05:30.') from None
        if stamp.tzinfo is None:
            stamp=stamp.replace(tzinfo=zone)
            # Reject nonexistent DST wall times rather than moving a reminder silently.
            if dt.datetime.fromtimestamp(stamp.timestamp(),zone).replace(tzinfo=None)!=stamp.replace(tzinfo=None):raise ValueError('That local time does not exist because of daylight saving time.')
        due=stamp.timestamp()
        if due<now+10 or due>now+366*86400:raise ValueError('Choose a time between 10 seconds and one year from now.')
    identity=store.add_schedule(owner,text.strip(),cron,tz,due)
    return {'id':identity,'next_run':dt.datetime.fromtimestamp(due,zone).isoformat(),'timezone':tz,'cron':cron,'destination':'your bot DM'}

async def tick(application,now=None):
    import primo
    from agent_tools import cleanup_artifacts
    cleanup_artifacts(application)
    store=application.bot_data['agent_store'];provided_now=now is not None;now=time.time() if now is None else now
    for job in store.due(now):
        if not store.claim(job['id']):continue
        user=primo.user_data_cache.get(str(job['owner']))
        if user:primo.normalize_user(user)
        if not user or user.get('subscription')!='active' or not store.prefs(job['owner'])['enabled']:
            store.schedule_status(job['id'],'paused');continue
        try:
            await application.bot.send_message(chat_id=job['owner'],text='⏰ '+job['text'],parse_mode=None)
        except RetryAfter as error:
            wait=error.retry_after.total_seconds() if hasattr(error.retry_after,'total_seconds') else float(error.retry_after)
            store.schedule_status(job['id'],'active',now+wait+1,job['failures']+1)
        except Forbidden:
            store.schedule_status(job['id'],'blocked')
        except NetworkError as error:
            # sendMessage may have succeeded. Retrying could send duplicate reminders.
            store.schedule_status(job['id'],'uncertain');log_error('Reminder delivery uncertain',error)
        except TelegramError as error:
            store.schedule_status(job['id'],'failed');log_error('Reminder rejected',error)
        else:
            if job['cron']:
                sent_at=now if provided_now else max(now,time.time())
                store.schedule_status(job['id'],'active',next_cron(job['cron'],job['timezone'],sent_at))
            else:store.schedule_status(job['id'],'done')

async def run(application):
    while True:
        try:await tick(application)
        except asyncio.CancelledError:raise
        except Exception as error:log_error('Scheduler tick failed',error)
        await asyncio.sleep(10)
