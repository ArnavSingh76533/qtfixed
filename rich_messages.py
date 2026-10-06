"""Bot API 10.3 bridge for the pinned PTB version; native rich LaTeX, no raster math."""
import re
from telegram.error import BadRequest

# Never rewrite LaTeX inside literal program code.
CODE = re.compile(r'(`{3,}|~{3,})[^\n]*\n[\s\S]*?(?:\1|\Z)|`[^`\n]+`')

def normalize_math(text):
    def convert(value):
        value = re.sub(r'\\\[([\s\S]*?)\\\]', lambda m: '\n\n$$\n'+m[1].strip()+'\n$$\n\n', value)
        return re.sub(r'\\\(([\s\S]*?)\\\)', lambda m: '$'+m[1].strip()+'$', value)
    parts=[]; pos=0
    for match in CODE.finditer(text):
        parts.extend([convert(text[pos:match.start()]),match[0]])
        pos=match.end()
    parts.append(convert(text[pos:]))
    return ''.join(parts)

def escape_query(text):
    return re.sub(r'([\\`*_{}\[\]()<>#+.!|$~=-])', r'\\\1', text)

def asked(query, answer):
    # Backward-compatible helper: the visible question header was removed.
    return answer

def fallback_markdown(text):
    """Keep complete-code download links when rich buttons aren't supported."""
    import html
    def row(match):
        links=re.findall(r'<tg-button type="url" url="([^"]*)">([^<]*)</tg-button>',match[0])
        return '\n'.join(f'[{html.unescape(label)}]({html.unescape(url)})' for url,label in links)
    parts=[];pos=0
    for match in CODE.finditer(text):
        parts.extend([re.sub(r'<tg-button-row>[\s\S]*?</tg-button-row>',row,text[pos:match.start()]),match[0]])
        pos=match.end()
    parts.append(re.sub(r'<tg-button-row>[\s\S]*?</tg-button-row>',row,text[pos:]))
    return ''.join(parts)

def rich_pages(text, limit=24000):
    """Keep normal fences/formulas intact. Bound bytes, paragraphs and block counts."""
    text=normalize_math(text)
    units=re.findall(r'```[\s\S]*?```|~~~[\s\S]*?~~~|\$\$[\s\S]*?\$\$|[^\n]+(?:\n|$)|\n',text)
    page='';lines=0
    for unit in units:
        if len(unit.encode())>limit:
            # Extremely large literal blocks: split into safe plain fenced chunks.
            for start in range(0,len(unit),limit//4):
                if page:yield page;page='';lines=0
                yield unit[start:start+limit//4]
            continue
        if page and (len((page+unit).encode())>limit or lines+unit.count('\n')>180):
            yield page;page='';lines=0
        page+=unit;lines+=unit.count('\n')
    if page.strip():yield page

async def api(bot, method, **data):
    # PTB's transport handles JSON, credentials, Telegram errors and connection pools.
    return await bot._post(method, data={k:v for k,v in data.items() if v is not None})

async def edit_rich(bot, *, text=None, rich=None, markup=None, **target):
    return await api(bot,'editMessageText',**target,
        rich_message=rich or {'markdown':normalize_math(text)},
        reply_markup=markup.to_dict() if markup else {'inline_keyboard':[]})

async def send_rich(message, text, markup=None):
    bot=message.get_bot()
    return await api(bot,'sendRichMessage',chat_id=message.chat_id,
        message_thread_id=message.message_thread_id,
        rich_message={'markdown':normalize_math(text)},
        reply_parameters={'message_id':message.message_id,'allow_sending_without_reply':True},
        reply_markup=markup.to_dict() if markup else None)


def with_code_copy(text, download=None):
    """One control per complete code block. Never split clipboard text."""
    import html
    from markdown_it import MarkdownIt
    tokens=MarkdownIt().parse(text)
    lines=text.splitlines(keepends=True)
    replacements={};number=0
    for token in tokens:
        if token.type not in ('fence','code_block') or not token.content.strip() or not token.map:continue
        number+=1
        code=token.content
        start,end=token.map
        button=''
        if len(code.rstrip('\n').encode('utf-16-le'))//2<=256:
            value=html.escape(code.rstrip('\n'),quote=True).replace('\n','&#10;').replace('\r','&#13;').replace('`','&#96;').replace('$','&#36;').replace(chr(92),'&#92;')
            button=f'<tg-button-row><tg-button type="copy_text" text="{value}">Copy code {number}</tg-button></tg-button-row>'
        elif download:
            url=download(number,code,(token.info or '').split()[0] if token.info else '')
            button=f'<tg-button-row><tg-button type="url" url="{html.escape(url,quote=True)}">Download complete code {number}</tg-button></tg-button-row>'
        original=''.join(lines[start:end])
        # Telegram has a finite message size. Large source stays whole in the file.
        if len(original.encode())>20000 and download:
            original='This code is too long for one Telegram message. Download the complete file below.\n'
        replacements[start]=(end,original+'\n'+button+'\n\n' if button else original)
    result=[];index=0
    while index<len(lines):
        if index in replacements:
            end,value=replacements[index];result.append(value);index=end
        else:result.append(lines[index]);index+=1
    return ''.join(result)
