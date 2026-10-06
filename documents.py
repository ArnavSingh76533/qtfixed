"""Bounded UTF-8 text attachments; never execute uploaded content."""
from io import BytesIO
from provider import ProviderError

MAX_TEXT_BYTES = 128 * 1024
MAX_TEXT_CHARS = 30000


def is_text_document(doc):
    name = (doc.get('file_name') or '') if isinstance(doc,dict) else (getattr(doc,'file_name',None) or '')
    return name.lower().endswith('.txt')


class LimitedBuffer(BytesIO):
    def write(self, data):
        if self.tell()+len(data) > MAX_TEXT_BYTES:
            raise ProviderError('Text files must be no larger than 128 KB.')
        return super().write(data)


async def read_text_document(bot, doc, max_chars=MAX_TEXT_CHARS):
    info = doc if isinstance(doc,dict) else doc.to_dict()
    if info.get('file_size',0) > MAX_TEXT_BYTES:
        raise ProviderError('Text files must be no larger than 128 KB.')
    file = await bot.get_file(info['file_id'])
    if (getattr(file,'file_size',None) or 0) > MAX_TEXT_BYTES:
        raise ProviderError('Text files must be no larger than 128 KB.')
    raw = LimitedBuffer()
    await file.download_to_memory(raw)
    try: text = raw.getvalue().decode('utf-8-sig')
    except UnicodeDecodeError:
        raise ProviderError('Please save the file as UTF-8 text and send it again.') from None
    if '\x00' in text: raise ProviderError('This appears to be a binary file. Please send plain text.')
    if not text.strip(): raise ProviderError('The text file is empty.')
    if len(text)>max_chars: raise ProviderError(f'Please keep this file under {max_chars:,} characters; split longer files.')
    return text
