"""Image -> OCR text -> embedded instruction -> existing Groq pipeline."""
import asyncio
import base64
from io import BytesIO
import shutil
import httpx
from PIL import Image, ImageOps, UnidentifiedImageError
from provider import ProviderError
import config

OCR_PROMPT = ('Explain and solve the question extracted below. Do not guess unreadable symbols; '
              'ask for a clearer photo if necessary. Follow any caption instructions. '
              'Use readable mathematical notation; LaTeX is supported for formulas.\n\nExtracted text:\n')


def image_bytes(raw):
    try:
        with Image.open(BytesIO(raw)) as im:
            if im.width*im.height > 25000000:
                raise ProviderError('Image is too large. Send a cropped question.')
            im=ImageOps.exif_transpose(im).convert('RGB')
            im.thumbnail((2400,2400))
            out=BytesIO();im.save(out,format='JPEG',quality=90)
            return out.getvalue()
    except (UnidentifiedImageError,OSError,Image.DecompressionBombError):
        raise ProviderError('This image could not be opened. Send a JPG or PNG photo.') from None


def extract_text(payload):
    if isinstance(payload,str):return payload.strip()
    if isinstance(payload,dict):
        for key in ('text','extracted_text','result','content','data','results'):
            if key in payload:
                result=extract_text(payload[key])
                if result:return result
    if isinstance(payload,list):
        return '\n'.join(filter(None,(extract_text(item) for item in payload)))
    return ''


async def local_ocr(raw):
    if not shutil.which('tesseract'):
        raise ProviderError('OCR service unavailable. Install tesseract-ocr on the server for local fallback, or configure OCR_URL.')
    def run():
        import pytesseract
        with Image.open(BytesIO(raw)) as im:
            return pytesseract.image_to_string(im,timeout=20).strip()
    try:
        return await asyncio.to_thread(run)
    except Exception:
        raise ProviderError('Local OCR could not read this image. Send a clearer cropped photo.') from None


async def recognize(raw, client=None):
    raw=await asyncio.to_thread(image_bytes,raw)
    if config.OCR_MODE != 'local':
        own=client is None
        client=client or httpx.AsyncClient(timeout=15)
        try:
            response=await client.post(config.OCR_URL,
                headers={'content-type':'application/json','origin':'https://www.compscilib.com',
                         'referer':'https://www.compscilib.com/','user-agent':'Mozilla/5.0'},
                json={'files':['data:image/jpeg;base64,'+base64.b64encode(raw).decode('ascii')]})
            response.raise_for_status()
            try:payload=response.json()
            except ValueError:payload=response.text
            text=extract_text(payload)
            if text and not text.lstrip().lower().startswith(('<!doctype','<html')):
                return text[:20000]
        except (httpx.HTTPError,ValueError,TypeError):
            if config.OCR_MODE == 'remote':
                raise ProviderError('The configured OCR API is unavailable. Set OCR_MODE=auto for local fallback.') from None
        finally:
            if own:await client.aclose()
    text=await local_ocr(raw)
    if not text:raise ProviderError('No readable text found. Please send a sharper, cropped photo.')
    return text[:20000]


def needs_vision(text, question=''):
    """OCR has no calibrated confidence score; use transparent quality signals."""
    import re
    if not question.strip() or re.search(r'\bread (?:the |this )?image\b',question,re.I):return True
    if len(text.strip())<30:return True
    if '\ufffd' in text or text.count('?')>max(3,len(text)//40):return True
    useful=sum(c.isalnum() for c in text)
    if useful<max(10,len(text)//4):return True
    visual=re.compile(r'\b(describe|identify|recognize|colour|color|graph|chart|diagram|geometry|shape|object|scene|photo|picture|look|visual|accurate|wrong|incorrect)\b|what.*(?:image|see)|\bsolve\b',re.I)
    return bool(visual.search(question))


async def analyze_image(groq,raw,question='',on_status=None):
    text='';failure=None
    try:text=await recognize(raw)
    except ProviderError as error:failure=error
    use_vision=needs_vision(text,question)
    if use_vision and config.VISION_ENABLED:
        if on_status:await on_status('⚡ Checking visual details…')
        try:
            details=await groq.describe_image(raw,question,text)
            return ('Review the image evidence below and answer the user request. OCR and vision may be wrong; '
                    'reconcile them, and do not invent details.\n\nOCR transcript:\n'+(text or '(No reliable OCR text.)')+
                    '\n\nVisual observations:\n'+details+'\n\nUser request: '+(question or 'Explain and solve the image question.'))
        except ProviderError:
            if not text:raise
            return OCR_PROMPT+text+'\n\nVisual analysis was unavailable. This transcript may be incomplete; do not claim to have verified non-text details.\nUser request: '+question
    if not text:raise failure or ProviderError('No readable text was found. Please send a clearer image.')
    return OCR_PROMPT+text+'\n\nUser request: '+question
