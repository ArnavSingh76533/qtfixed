"""Native private-chat drafts and throttled message-edit fallback."""
import time
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions
from telegram.error import BadRequest, RetryAfter, TelegramError
import config


class StreamPreview:
    def __init__(self, message, bot, owner, enabled=True):
        self.message, self.bot, self.owner = message, bot, owner
        self.enabled = enabled
        self.status = None
        self.last_update = 0
        self.next_update = 0
        self.last_text = ''
        self.draft = config.DRAFT_STREAMING and message.chat.type == 'private'
        self.draft_id = message.message_id or 1

    async def start(self):
        self.status = await self.message.reply_text('Thinking…', reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton('⏹ Stop', callback_data=f'chat:stop:{self.owner}')]]))

    async def update(self, text):
        now = time.monotonic()
        if not self.enabled or now < self.next_update or now-self.last_update < 1.3:
            return
        # Preview the newest portion; the final answer always contains all text.
        preview = text if len(text) <= 1700 else '…\n' + text[-1700:]
        if preview == self.last_text or not preview.strip():
            return
        self.last_update = now
        try:
            if self.draft:
                try:
                    await self.bot.send_message_draft(chat_id=self.message.chat_id,
                        draft_id=self.draft_id, text=preview,
                        message_thread_id=self.message.message_thread_id)
                except BadRequest:
                    self.draft = False
            if not self.draft:
                await self.status.edit_text(preview, link_preview_options=LinkPreviewOptions(is_disabled=True),
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('⏹ Stop', callback_data=f'chat:stop:{self.owner}')]]))
            self.last_text = preview
        except RetryAfter as error:
            delay = error.retry_after
            self.next_update = now + (delay.total_seconds() if hasattr(delay,'total_seconds') else delay) + 1
        except TelegramError:
            # A preview failure must not discard the provider's final answer.
            pass

    async def close(self):
        if self.status:
            try:
                await self.status.delete()
            except TelegramError:
                pass
