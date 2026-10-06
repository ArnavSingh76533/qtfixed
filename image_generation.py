"""Asynchronous image providers, bounded downloads and per-prompt entitlements."""
import asyncio
import logging
import re
from io import BytesIO
from urllib.parse import urlsplit
import httpx
from PIL import Image
from telegram import InputMediaPhoto
from telegram.error import TelegramError, BadRequest
import config
from provider import ProviderError

logger=logging.getLogger(__name__)
SIZES={'-p1':('portrait_4_3',768,1024),'-p2':('portrait_16_9',576,1024),
       '-l1':('landscape_4_3',1024,768),'-l2':('landscape_16_9',1024,576),
       '-s1':('square',1024,1024),'-s2':('square_hd',1024,1024),'-hd':('square_hd',1024,1024)}
IMAGE_INTENT=re.compile(r'^\s*(?:please\s+)?(?:(?:can|could|would)\s+you\s+)?(?:generate|create|draw|make|design|paint|imagine|send|show|give)\b.{0,90}\b(?:image|picture|photo|artwork|illustration|poster|wallpaper|logo|portrait)\b|^\s*(?:draw|paint|imagine)\s+|\b(?:image|photo|picture)\s+(?:banao|bana do|banado)\b',re.I|re.S)

class GeneratedImage(bytes):
    def __new__(cls, data, url):
        result=super().__new__(cls,data);result.url=url;return result


class ImageRequested(Exception):
    def __init__(self,prompt):self.prompt=prompt

def available():return bool(config.FAL_API_KEY or config.GETIMG_API_KEY)
def image_intent(prompt):
    if re.search(r'\b(find|search|existing|actual|real photograph|original photo)\b',prompt,re.I):return False
    noun=re.search(r'\b(image|picture|photo|artwork|illustration|poster|wallpaper|logo|portrait)\b',prompt,re.I)
    if noun and re.search(r'\b(code|script|function|program|api)\b',prompt[:noun.start()],re.I):return False
    return bool(IMAGE_INTENT.search(prompt))
def image_count(user):return 4 if user.get('subscription')=='active' else 1

def parse_prompt(prompt):
    words=prompt.strip().split(maxsplit=1)
    size=SIZES['-l2']
    if words and words[0] in SIZES:
        size=SIZES[words[0]];prompt=words[1] if len(words)>1 else ''
    if not prompt.strip():raise ProviderError('Use /image followed by what you want to create.')
    if len(prompt)>4000:raise ProviderError('Please keep the image prompt under 4,000 characters.')
    return prompt.strip(),size

class ImageClient:
    def __init__(self,client=None):
        self.client=client or httpx.AsyncClient(timeout=httpx.Timeout(90,connect=15),follow_redirects=False)
    async def close(self):await self.client.aclose()

    async def download(self,url):
        parts=urlsplit(url)
        host=(parts.hostname or '').lower()
        allowed=('fal.media','fal.ai','getimg.ai')
        if parts.scheme!='https' or parts.username or parts.port not in (None,443) or not any(host==h or host.endswith('.'+h) for h in allowed):
            raise ProviderError('The image service returned an unsupported download address.')
        data=bytearray()
        async with self.client.stream('GET',url) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data)>10*1024*1024:raise ProviderError('The generated image is too large to send.')
        def verify():
            with Image.open(BytesIO(data)) as im:
                if im.width*im.height>25_000_000:raise ValueError('oversized image')
                im.verify()
        await asyncio.to_thread(verify)
        return GeneratedImage(bytes(data),url)

    async def fal(self,prompt,size,count):
        if not config.FAL_API_KEY:raise ProviderError('Primary image provider is not configured.')
        r=await self.client.post('https://fal.run/fal-ai/flux/schnell',
            headers={'Authorization':'Key '+config.FAL_API_KEY},
            json={'prompt':prompt,'image_size':size[0],'num_inference_steps':4,'num_images':count,
                  'output_format':'jpeg','enable_safety_checker':True})
        r.raise_for_status();result=r.json()
        # Do not use fallback to evade a provider content rejection.
        if any(result.get('has_nsfw_concepts') or []):
            raise ContentRejected('The image service could not generate this prompt. Please try a different description.')
        urls=[i['url'] for i in result.get('images',[]) if isinstance(i,dict) and isinstance(i.get('url'),str)]
        if len(urls)!=count:raise ValueError('incomplete image batch')
        return await asyncio.gather(*(self.download(u) for u in urls))

    async def getimg(self,prompt,size,count):
        if not config.GETIMG_API_KEY:raise ProviderError('Backup image provider is not configured.')
        async def one():
            r=await self.client.post('https://api.getimg.ai/v1/flux-schnell/text-to-image',
                headers={'Authorization':'Bearer '+config.GETIMG_API_KEY},json={
                    'prompt':prompt,'width':size[1],'height':size[2],'steps':4,
                    'response_format':'url','output_format':'jpeg'})
            r.raise_for_status();result=r.json()
            if not isinstance(result.get('url'),str):raise ValueError('missing image')
            return await self.download(result['url'])
        results=await asyncio.gather(*(one() for _ in range(count)),return_exceptions=True)
        error=next((r for r in results if isinstance(r,BaseException)),None)
        if error:raise error
        return results

    async def generate(self,prompt,count):
        if not available():raise ProviderError('Image generation is not configured yet. Please contact the bot admin.')
        prompt,size=parse_prompt(prompt)
        try:
            return await self.fal(prompt,size,count)
        except ContentRejected:raise
        except (httpx.HTTPError,ValueError,KeyError,TypeError,OSError,ProviderError):
            logger.warning('Primary image provider unavailable; trying backup')
        try:
            return await self.getimg(prompt,size,count)
        except (httpx.HTTPError,ValueError,KeyError,TypeError,OSError,ProviderError):
            raise ProviderError('Image generation is temporarily unavailable. No quota was used; please try again later.') from None

class ContentRejected(ProviderError):pass

async def log_prompt(context,owner,prompt):
    import main
    if not main.LOG_CHANNEL_ID:return
    # Plain text; never include keys or raw provider payloads.
    text=f'Image request\nUser: {owner}\nPrompt: {prompt}'
    for start in range(0,len(text),2000):
        try:await context.bot.send_message(chat_id=main.LOG_CHANNEL_ID,text=text[start:start+2000],parse_mode=None)
        except TelegramError:logger.warning('Could not deliver image prompt log')

async def generate_images(context,owner,user,prompt,original_prompt=None):
    await log_prompt(context,owner,original_prompt or prompt)
    return await asyncio.wait_for(context.application.bot_data['images'].generate(prompt,image_count(user)),180)

async def deliver_images(message,images,prompt,context=None,owner=None,target=None):
    if context is not None:
        from rich_messages import api
        try:ids=await cache_images(context,owner,images)
        except ProviderError:
            ids=[getattr(img,'url','') for img in images]
        try:
            if not all(ids):raise BadRequest('No cached rich media')
            if target:
                await api(context.bot,'editMessageText',chat_id=message.chat_id,message_id=target.message_id,
                    rich_message=image_rich(ids,prompt),reply_markup={'inline_keyboard':[]})
            else:
                await api(context.bot,'sendRichMessage',chat_id=message.chat_id,
                    message_thread_id=message.message_thread_id,rich_message=image_rich(ids,prompt),
                    reply_parameters={'message_id':message.message_id,'allow_sending_without_reply':True})
            return
        except BadRequest:
            # Definitive rejection: use a normal photo/album in the same chat.
            # Do not retry network timeouts here: the rich message may have arrived.
            logger.warning('Rich image rejected; sending native photo in the same chat')
        if target and len(images)==1:
            media=ids[0] if ids and ids[0] else BytesIO(images[0])
            await context.bot.edit_message_media(chat_id=message.chat_id,message_id=target.message_id,media=InputMediaPhoto(media))
            return
    # Retained for callers that only provide a Message (never sends to another chat).
    caption='🎨 '+prompt[:850]
    if len(images)==1:
        await message.reply_photo(BytesIO(images[0]),caption=caption)
    else:
        await message.reply_media_group([InputMediaPhoto(BytesIO(data),caption=caption if i==0 else None)
                                         for i,data in enumerate(images)])

async def cache_images(context,owner,images):
    """Inline rich content requires existing Telegram file IDs, not remote URLs."""
    import main
    target=config.IMAGE_CACHE_CHAT_ID or main.LOG_CHANNEL_ID
    if not target:
        raise ProviderError('Image delivery needs an upload chat. Ask the admin to set IMAGE_CACHE_CHAT_ID to a private channel where the bot can post.')
    ids=[]
    for data in images:
        try:
            sent=await context.bot.send_photo(chat_id=target,photo=BytesIO(data),disable_notification=True)
        except TelegramError as error:
            logger.warning('Image upload chat rejected sendPhoto (%s)',type(error).__name__)
            raise ProviderError('The image upload chat is unavailable. Ask the admin to set IMAGE_CACHE_CHAT_ID or fix the bot permissions in the log chat.') from None
        ids.append(sent.photo[-1].file_id)
        try:await context.bot.delete_message(chat_id=target,message_id=sent.message_id)
        except TelegramError:logger.warning('Could not delete image staging message')
    return ids

def image_rich(file_ids,prompt=''):
    # Structured blocks avoid tg:// markdown URI parsing differences.
    return {'blocks':[{'type':'photo','photo':{'type':'photo','media':fid}} for fid in file_ids]}
