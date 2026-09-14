"""Convert Markdown to native Telegram entities, with Unicode-safe chunking."""
import re
from markdown_it import MarkdownIt
from telegram import MessageEntity

PARSER = MarkdownIt('commonmark', {'html': False}).enable(['strikethrough', 'table'])


def units(text):
    return len(text.encode('utf-16-le')) // 2


def formatted_chunks(markdown, limit=3800):
    parts, spans = [], []
    offset = 0
    stack = []
    lists = []

    def append(text):
        nonlocal offset
        parts.append(text)
        offset += units(text)

    def newline():
        if parts and not parts[-1].endswith('\n'):
            append('\n')

    def open_span(kind, **kwargs):
        stack.append((kind, offset, kwargs))

    def close_span():
        kind, start, kwargs = stack.pop()
        if offset > start:
            spans.append((kind, start, offset, kwargs))

    def inline(tokens):
        for t in tokens:
            typ = t.type
            if typ == 'text':
                # Telegram spoilers are not a CommonMark construct.
                for i, piece in enumerate(re.split(r'(\|\|[^|]+\|\|)', t.content)):
                    if piece.startswith('||') and piece.endswith('||'):
                        open_span('spoiler'); append(piece[2:-2]); close_span()
                    else:
                        append(piece)
            elif typ in ('softbreak', 'hardbreak'):
                append('\n')
            elif typ in ('strong_open', 'em_open', 's_open'):
                open_span({'strong_open':'bold', 'em_open':'italic', 's_open':'strikethrough'}[typ])
            elif typ in ('strong_close', 'em_close', 's_close'):
                close_span()
            elif typ == 'code_inline':
                open_span('code'); append(t.content); close_span()
            elif typ == 'link_open':
                href = t.attrGet('href') or ''
                # Parser also rejects unsafe schemes; plain text still survives.
                open_span('text_link', url=href)
            elif typ == 'link_close':
                close_span()
            elif typ == 'image':
                append(t.content)
            else:
                append(t.content)

    for token in PARSER.parse(markdown):
        typ = token.type
        if typ == 'inline':
            inline(token.children or [])
        elif typ in ('fence', 'code_block'):
            newline()
            lang = (token.info.strip().split() or [''])[0]
            open_span('pre', language=lang if re.fullmatch(r'[\w+#.-]{0,40}', lang) else '')
            append(token.content.rstrip('\n')); close_span(); append('\n\n')
        elif typ == 'heading_open':
            newline(); open_span('bold')
        elif typ == 'heading_close':
            close_span(); append('\n\n')
        elif typ == 'paragraph_close':
            append('\n' if lists else '\n\n')
        elif typ in ('bullet_list_open', 'ordered_list_open'):
            lists.append(int(token.attrGet('start') or 1) if typ == 'ordered_list_open' else None)
        elif typ == 'list_item_open':
            newline()
            append('  ' * max(0, len(lists)-1))
            if lists and lists[-1] is not None:
                append(f'{lists[-1]}. '); lists[-1] += 1
            else:
                append('• ')
        elif typ in ('bullet_list_close', 'ordered_list_close'):
            lists.pop(); newline()
        elif typ == 'blockquote_open':
            newline(); open_span('blockquote')
        elif typ == 'blockquote_close':
            close_span(); newline()
        elif typ in ('th_close', 'td_close'):
            append(' | ')
        elif typ == 'tr_close':
            newline()
        elif typ == 'hr':
            append('────────\n')
    text = ''.join(parts).rstrip()
    # Prefer a paragraph/line boundary; never split a UTF-16 surrogate pair.
    begin = 0
    consumed = 0
    while begin < len(text):
        end, count = begin, 0
        while end < len(text) and count + units(text[end]) <= limit:
            count += units(text[end]); end += 1
        if end < len(text):
            boundary = text.rfind('\n', begin + (end-begin)//2, end)
            if boundary > begin:
                end = boundary + 1
        chunk = text[begin:end]
        length = units(chunk)
        entities = []
        for kind, start, stop, kwargs in spans:
            left, right = max(start, consumed), min(stop, consumed + length)
            if right > left:
                entities.append(MessageEntity(kind, left-consumed, right-left, **kwargs))
        entities.sort(key=lambda e: (e.offset, -e.length))
        # Telegram forbids any nesting with code/pre and nesting blockquotes.
        safe = []
        for e in entities:
            if e.type not in ('code','pre') and any(
                c.type in ('code','pre') and e.offset < c.offset+c.length and c.offset < e.offset+e.length
                for c in entities):
                continue
            if e.type == 'blockquote' and any(c.type == 'blockquote' and c.offset <= e.offset and c.offset+c.length >= e.offset+e.length for c in safe):
                continue
            safe.append(e)
        if chunk.strip():
            yield chunk, safe
        begin = end
        consumed += length
