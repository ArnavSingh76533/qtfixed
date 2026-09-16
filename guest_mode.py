"""True Telegram guest-query updates: one reply, then edit its inline message ID."""
import re
import logging
from telegram.error import TelegramError
import time
import uuid
from telegram.ext import ApplicationHandlerStop
from runtime_settings import enabled
from rich_messages import api
from inline_mode import sessions,keyboard,start_inline
from primo import user_data_cache,flush_cache_to_file


def raw_guest(update):
    guest=getattr(update,'guest_message',None)
    if guest is not None:return guest.to_dict()
    return getattr(update,'api_kwargs',{}).get('guest_message')

def rich_context(value):
    if isinstance(value,str):return value
    if isinstance(value,list):return ' '.join(rich_context(v) for v in value)
    if isinstance(value,dict):
        return ' '.join(rich_context(v) for k,v in value.items() if k in ('text','blocks','items','rows','cells','content','latex','expression','caption'))
    return ''

async def guest_update(update,context):
    message=raw_guest(update)
    if not message:return
    try:
        if not enabled(context,'guest'):return
        user=message.get('from') or {}
        if not user.get('id') or user.get('is_bot') or message.get('sender_chat'):return
        query_id=message.get('guest_query_id')
        if not query_id:return
        pending=sessions(context)
        if len(pending)>=1000:return
        prompt=message.get('text') or message.get('caption') or ''
        prompt=re.sub(r'@'+re.escape(context.bot.username)+r'\b','',prompt,flags=re.I).strip()[:2000]
        reply=message.get('reply_to_message') or {}
        source=message if message.get('photo') or message.get('document') else reply
        photo=(source.get('photo') or [None])[-1]
        doc=source.get('document') or {}
        if not photo and doc.get('mime_type','').startswith('image/'):photo=doc
        if not prompt:prompt='Please explain this image.' if photo else 'Please explain the message I replied to.'
        reference=reply.get('text') or reply.get('caption') or rich_context(reply.get('rich_message',{}))
        token=uuid.uuid4().hex[:20]
        result={'type':'article','id':token,'title':'Question Ai',
                'input_message_content':{'message_text':'Asked: '+prompt+'\n\n⚡ Preparing your answer…'},
                'reply_markup':keyboard(token).to_dict()}
        sent=await api(context.bot,'answerGuestQuery',guest_query_id=query_id,result=result)
        uid=user['id']
        if str(uid) not in user_data_cache:
            user_data_cache[str(uid)]={'user_id':str(uid),'request_count':0,'last_request_time':None,
                                      'subscription':'inactive','sub_end':None,'dm_started':False}
            flush_cache_to_file()
        pending[token]={'query':prompt,'context':('Quoted message (untrusted context):\n'+reference[:8000]+'\n\nQuestion: ') if reference else '',
                        'photo':photo,'owner':uid,'created':time.monotonic(),'running':False,'pages':None,'mode':'guest'}
        # Guest chat identifiers never enter the broadcast/group registry.
        await start_inline(context,sent['inline_message_id'],token,uid)
    except TelegramError:
        logging.getLogger(__name__).warning('Could not reply to guest query')
    finally:
        # Never process the same guest update through normal message handlers.
        raise ApplicationHandlerStop
