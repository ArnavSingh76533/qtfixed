"""Native private-chat drafts and throttled message-edit fallback."""
import time
import asyncio
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions
from telegram.error import BadRequest, RetryAfter, TelegramError
import config
from rich_messages import api, edit_rich, rich_pages
from math_format import readable_math


class StreamPreview:
    def __init__(self, message, bot, owner, enabled=True, rich=True):
        self.message, self.bot, self.owner = message, bot, owner
        self.enabled = enabled
        self.rich = rich
        self.status = None
        self.last_update = 0
        self.next_update = 0
        self.last_text = ''
        self.keep_status = False
        self.pending_text=None
        self.preview_task=None
        self.draft = config.DRAFT_STREAMING and message.chat.type == 'private'
        self.draft_id = message.message_id or 1

    async def start(self):
        self.status = await self.message.reply_text('⚡ Working…', reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton('⏹ Stop', callback_data=f'chat:stop:{self.owner}',style='danger')]]))

    async def set_status(self,text):
        markup=InlineKeyboardMarkup([[InlineKeyboardButton('⏹ Stop',callback_data=f'chat:stop:{self.owner}',style='danger')]])
        try:
            if self.status:await asyncio.wait_for(self.status.edit_text(text[:3500],reply_markup=markup),5)
        except (TelegramError,asyncio.TimeoutError):
            pass

    async def update(self, text):
        if not self.enabled:return
        self.pending_text=text
        if not self.preview_task or self.preview_task.done():
            self.preview_task=asyncio.create_task(self._pump())
        await asyncio.sleep(0)

    async def _pump(self):
        try:
            while self.pending_text is not None:
                text=self.pending_text;self.pending_text=None
                await self._render(text)
                await asyncio.sleep(1.3)
        except asyncio.CancelledError:raise
        except TelegramError:pass
        except Exception as error:
            from telegram_delivery import log_error
            log_error('Preview update failed',error)

    async def finish_updates(self):
        self.pending_text=None
        if self.preview_task:
            self.preview_task.cancel()
            await asyncio.gather(self.preview_task,return_exceptions=True)

    async def _render(self, text):
        now = time.monotonic()
        if not self.enabled or now < self.next_update or now-self.last_update < 1.3:
            return
        # Preview the newest portion; the final answer always contains all text.
        preview = next(iter(rich_pages(text)),text[:1700]) if self.rich else readable_math(text if len(text)<=1700 else '…\n'+text[-1700:])
        if preview == self.last_text or not preview.strip():
            return
        self.last_update = now
        try:
            if self.draft:
                try:
                    if self.rich:
                        await api(self.bot,'sendRichMessageDraft',chat_id=self.message.chat_id,
                            draft_id=self.draft_id,rich_message={'markdown':preview},
                            message_thread_id=self.message.message_thread_id)
                    else:
                        await self.bot.send_message_draft(chat_id=self.message.chat_id,
                            draft_id=self.draft_id,text=preview,message_thread_id=self.message.message_thread_id)
                except BadRequest:
                    self.draft = False
            if not self.draft:
                markup=InlineKeyboardMarkup([[InlineKeyboardButton('⏹ Stop', callback_data=f'chat:stop:{self.owner}',style='danger')]])
                try:
                    if self.rich:
                        await edit_rich(self.bot,chat_id=self.message.chat_id,message_id=self.status.message_id,text=preview,markup=markup)
                    else:await self.status.edit_text(preview,reply_markup=markup)
                except BadRequest:
                    await self.status.edit_text(readable_math(text[-1700:]),reply_markup=markup)
            self.last_text = preview
        except RetryAfter as error:
            delay = error.retry_after
            self.next_update = now + (delay.total_seconds() if hasattr(delay,'total_seconds') else delay) + 1
        except TelegramError:
            # A preview failure must not discard the provider's final answer.
            pass

    async def close(self):
        await self.finish_updates()
        if self.status and not self.keep_status:
            try:
                await self.status.delete()
            except TelegramError:
                pass


class LatestPreview:
    """Coalesce optional inline previews without blocking model streaming."""
    def __init__(self,render):self.render=render;self.task=None;self.latest=None
    async def update(self,text):
        self.latest=text
        if not self.task or self.task.done():self.task=asyncio.create_task(self.run())
        await asyncio.sleep(0)
    async def run(self):
        try:
            while self.latest is not None:
                text=self.latest;self.latest=None
                try:await self.render(text)
                except TelegramError:pass
                await asyncio.sleep(1.5)
        except asyncio.CancelledError:raise
    async def close(self):
        self.latest=None
        if self.task:
            self.task.cancel();await asyncio.gather(self.task,return_exceptions=True)
