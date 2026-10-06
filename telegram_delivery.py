"""Retry known-safe Telegram operations without duplicating uncertain sends."""
import asyncio
import logging
import random
import re
import traceback
import httpx
from telegram.error import BadRequest, NetworkError, RetryAfter
from telegram.ext import ExtBot
from telegram.request import HTTPXRequest
import config

logger=logging.getLogger(__name__)

def describe_error(error):
    text=str(error)
    text=re.sub(r'https?://\S+', '[URL]',text)
    text=re.sub(r'\b(?:gsk_[\w-]+|\d{6,}:[\w-]{20,})','[credential]',text)
    return f'{type(error).__name__}: {text[:350]}'

def log_error(label,error):
    frames=traceback.extract_tb(error.__traceback__)[-6:]
    location=' → '.join(f'{f.filename.rsplit("/",1)[-1]}:{f.lineno} {f.name}' for f in frames)
    logger.error('%s | %s | %s',label,describe_error(error),location)

def safe_retry(endpoint,error):
    name=endpoint.lower()
    if name.startswith(('get','edit','delete','sendchataction','sendmessagedraft','sendrichmessagedraft','set')):
        return True
    # These failures occur before the request is sent. Read/write timeouts may
    # mean a new message was delivered; never replay those blindly.
    cause=error.__cause__
    while cause:
        if isinstance(cause,(httpx.ConnectError,httpx.ConnectTimeout,httpx.PoolTimeout)):return True
        cause=cause.__cause__
    return False

async def call_with_retry(endpoint,call,attempts=None):
    attempts=attempts or config.TELEGRAM_RETRIES
    for attempt in range(attempts):
        try:return await call()
        except BadRequest as error:
            if endpoint.lower().startswith('edit') and 'not modified' in str(error).lower():return True
            raise
        except RetryAfter as error:
            delay=error.retry_after
            delay=delay.total_seconds() if hasattr(delay,'total_seconds') else float(delay)
            if attempt+1==attempts or delay>60:raise
            logger.warning('Telegram %s rate limited; retry %d/%d',endpoint,attempt+2,attempts)
            await asyncio.sleep(delay+0.25)
        except NetworkError as error:
            if attempt+1==attempts or not safe_retry(endpoint,error):raise
            logger.warning('Telegram %s transient failure; retry %d/%d (%s)',endpoint,attempt+2,attempts,type(error).__name__)
            await asyncio.sleep(min(8,2**attempt)+random.uniform(0,0.25))

class ReliableBot(ExtBot):
    async def _post(self,endpoint,data=None,**kwargs):
        async def send():return await super(ReliableBot,self)._post(endpoint,data,**kwargs)
        return await call_with_retry(endpoint,send)

async def download_into(file,buffer):
    async def read():
        buffer.seek(0);buffer.truncate(0)
        return await file.download_to_memory(buffer)
    return await call_with_retry('getFileDownload',read)

def make_bot():
    # Separate polling connections; slow uploads must not starve updates/edits.
    request=HTTPXRequest(connection_pool_size=max(64,config.MAX_CONCURRENT_REQUESTS*8),
        read_timeout=45,write_timeout=60,connect_timeout=20,pool_timeout=20,media_write_timeout=120)
    polling=HTTPXRequest(connection_pool_size=2,read_timeout=40,write_timeout=20,connect_timeout=20,pool_timeout=10)
    return ReliableBot(config.BOT_TOKEN,request=request,get_updates_request=polling)
