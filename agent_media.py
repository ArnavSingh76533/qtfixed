"""Validated task media, public downloads and same-chat delivery."""
import asyncio
from io import BytesIO
import http.client
import time
from urllib.parse import urljoin
from PIL import Image
from telegram import InputMediaPhoto, InputMediaVideo, InputMediaDocument
from telegram.error import BadRequest, TelegramError
from public_web import connect_public, web_url
from provider import ProviderError
import config

MAX_MEDIA=50_000_000  # Strictly below the user-requested 50 MB.

def media_kind(raw,path):
    if not raw or len(raw)>=MAX_MEDIA:raise ValueError('Media must be nonempty and smaller than 50 MB.')
    if raw.startswith((b'\x89PNG\r\n\x1a\n',b'\xff\xd8\xff',b'GIF87a',b'GIF89a')) or (raw[:4]==b'RIFF' and raw[8:12]==b'WEBP'):
        with Image.open(BytesIO(raw)) as im:
            if im.width*im.height>50_000_000:raise ValueError('Image dimensions are too large.')
            im.verify()
        return 'photo' if len(raw)<=10_000_000 and raw[:3]!=b'GIF' else 'document'
    if len(raw)>12 and raw[4:8]==b'ftyp':return 'video'
    if raw[:4]==b'\x1aE\xdf\xa3':return 'document'  # WebM/MKV downloads as original files.
    raise ValueError('This file is not a supported image or MP4/WebM video.')

def download_public(url):
    current=url;deadline=time.monotonic()+90
    for _ in range(6):
        parsed,port=web_url(current)
        cls=http.client.HTTPSConnection if parsed.scheme=='https' else http.client.HTTPConnection
        connection=cls(parsed.hostname,port,timeout=15)
        connection._create_connection=lambda address,timeout=None,*a,**k:connect_public(parsed.hostname,port,timeout or 15)
        try:
            target=parsed.path or '/'
            if parsed.query:target+='?'+parsed.query
            connection.request('GET',target,headers={'User-Agent':'QuestionAi/1.0','Accept-Encoding':'identity'})
            response=connection.getresponse()
            if response.status in (301,302,303,307,308):
                location=response.getheader('Location')
                if not location:raise ValueError('Media redirect has no destination.')
                current=urljoin(current,location);continue
            if response.status!=200:raise ValueError('Media download rejected (HTTP '+str(response.status)+').')
            declared=response.getheader('Content-Length')
            if declared and int(declared)>=MAX_MEDIA:raise ValueError('Media is not smaller than 50 MB.')
            raw=bytearray()
            while True:
                if time.monotonic()>deadline:raise ValueError('Media download timed out.')
                chunk=response.read(65536)
                if not chunk:break
                if len(raw)+len(chunk)>=MAX_MEDIA:raise ValueError('Media is not smaller than 50 MB.')
                raw.extend(chunk)
            return bytes(raw),current
        finally:connection.close()
    raise ValueError('Too many media redirects.')

async def download(url):return await asyncio.wait_for(asyncio.to_thread(download_public,url),110)

async def deliver(message,items):
    for item in items:
        data=BytesIO(item['data']);data.name=item['path'].rsplit('/',1)[-1]
        args={'caption':item['caption'][:900],'parse_mode':None}
        try:
            if item['kind']=='photo':await message.reply_photo(data,**args)
            elif item['kind']=='video':await message.reply_video(data,supports_streaming=True,**args)
            else:await message.reply_document(data,filename=data.name,**args)
        except BadRequest:
            data.seek(0)
            await message.reply_document(data,filename=data.name,**args)
        item['delivered']=True

async def cache(context,owner,items):
    import main
    target=config.IMAGE_CACHE_CHAT_ID or main.LOG_CHANNEL_ID
    if not target:raise ProviderError('Inline/guest task media needs IMAGE_CACHE_CHAT_ID or a writable log chat. Files remain available through /agentstatus in the bot.')
    pages=[]
    for item in items:
        if not item.get('file_id'):
            data=BytesIO(item['data']);data.name=item['path'].rsplit('/',1)[-1]
            args={'chat_id':target,'disable_notification':True}
            try:
                if item['kind']=='photo':sent=await context.bot.send_photo(photo=data,**args)
                elif item['kind']=='video':sent=await context.bot.send_video(video=data,supports_streaming=True,**args)
                else:sent=await context.bot.send_document(document=data,filename=data.name,**args)
            except BadRequest:
                data.seek(0);sent=await context.bot.send_document(document=data,filename=data.name,**args);item['kind']='document'
            kind=item['kind'];item['file_id']=sent.photo[-1].file_id if kind=='photo' else getattr(sent,kind).file_id
            try:await context.bot.delete_message(chat_id=target,message_id=sent.message_id)
            except TelegramError:pass
        kind=item['kind'];pages.append({'blocks':[{'type':kind,kind:{'type':kind,'media':item['file_id']}}]})
    return pages
