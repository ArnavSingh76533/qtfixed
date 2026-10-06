"""One complete source file per long code block, available through an owner-bound link."""
import time
import uuid
from io import BytesIO
from rich_messages import with_code_copy

EXTENSIONS={'python':'py','py':'py','javascript':'js','js':'js','typescript':'ts',
            'bash':'sh','sh':'sh','html':'html','css':'css','json':'json','java':'java',
            'cpp':'cpp','c':'c','sql':'sql','markdown':'md','yaml':'yaml'}


def format_answer(context, owner, text):
    cache=context.application.bot_data.setdefault('code_downloads',{})
    now=time.monotonic()
    for token in list(cache):
        if now-cache[token]['created']>3600:cache.pop(token,None)
    def save(number,code,language):
        while len(cache)>=200:cache.pop(next(iter(cache)))
        token=uuid.uuid4().hex
        cache[token]={'owner':owner,'code':code,'created':now,
                      'filename':f'code_{number}.'+EXTENSIONS.get(language.lower(),'txt')}
        return f'https://t.me/{context.bot.username}?start=code_{token}'
    return with_code_copy(text,download=save)


async def start_with_code(update,context):
    args=context.args or []
    if not args or not args[0].startswith('code_'):
        from primo import start
        return await start(update,context)
    if update.effective_chat.type!='private':return
    item=context.application.bot_data.get('code_downloads',{}).get(args[0][5:])
    if not item or time.monotonic()-item['created']>3600:
        return await update.effective_message.reply_text('This code download expired. Regenerate the answer to get a new file.')
    if item['owner']!=update.effective_user.id:
        return await update.effective_message.reply_text('This download belongs to the person who requested the answer.')
    await update.effective_message.reply_document(BytesIO(item['code'].encode()),filename=item['filename'],caption='Complete code — one file, unchanged.')
