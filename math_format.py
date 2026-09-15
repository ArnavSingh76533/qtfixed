"""Readable inline math and locally rendered display equations (no shell/TeX)."""
import re
import threading
from io import BytesIO
from pylatexenc.latex2text import LatexNodes2Text, MacroTextSpec, get_default_latex_context_db

# Match code first so literal dollars/backslashes in code are preserved.
TOKENS=re.compile(r'(```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]+`)|(\\\[[\s\S]*?\\\]|\$\$[\s\S]*?\$\$)|(\\\([^\n]*?\\\)|(?<!\\)\$(?!\s)(?:\\.|[^$\n])+?(?<!\s)\$)')
LOCK=threading.Lock()


def latex_text(expression):
    def fraction(node,l2tobj):
        args=node.nodeargd.argnlist
        return '('+l2tobj.node_to_text(args[-2])+')/('+l2tobj.node_to_text(args[-1])+')'
    context=get_default_latex_context_db()
    context.add_context_category('safe-fractions',macros=[MacroTextSpec(name,simplify_repl=fraction) for name in ('frac','dfrac','tfrac')],prepend=True)
    return LatexNodes2Text(latex_context=context).latex_to_text(expression).strip()


def segments(text):
    cursor=0
    for match in TOKENS.finditer(text):
        if match.start()>cursor:yield 'text',text[cursor:match.start()]
        if match.group(1):yield 'text',match.group(1)
        elif match.group(2):yield 'math',match.group(2)[2:-2].strip()
        else:
            raw=match.group(3)
            yield 'inline',latex_text(raw[2:-2] if raw.startswith('\\(') else raw[1:-1])
        cursor=match.end()
    if cursor<len(text):yield 'text',text[cursor:]


def readable_math(text):
    # Keep math inside inline code so Markdown does not eat underscores/asterisks.
    return ''.join(value if kind=='text' else '`'+(latex_text(value) if kind=='math' else value).replace('`','′')+'`'
                   for kind,value in segments(text))


def render_equation(expression):
    if len(expression)>1800:return None
    def draw():
        from matplotlib.mathtext import math_to_image, MathTextParser
        from matplotlib.font_manager import FontProperties
        from PIL import Image, ImageOps
        prop=FontProperties(size=18)
        equation='$'+expression.replace('\n',' ')+ '$'
        # Bound dimensions before rasterizing a potentially pathological formula.
        dims=MathTextParser('path').parse(equation,dpi=140,prop=prop)
        if dims.width>3000 or dims.height>1800:return None
        output=BytesIO()
        math_to_image(equation,output,prop=prop,dpi=140,format='png',color='#172b4d')
        output.seek(0)
        with Image.open(output) as im:
            canvas=Image.new('RGB',im.size,'white')
            if im.mode=='RGBA':canvas.paste(im,mask=im.getchannel('A'))
            else:canvas.paste(im)
            canvas=ImageOps.expand(canvas,border=24,fill='white')
            final=BytesIO();canvas.save(final,'PNG');return final.getvalue()
    try:
        with LOCK:return draw()
    except (ValueError,RuntimeError,TypeError,OverflowError):
        return None
