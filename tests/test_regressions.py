import asyncio
import datetime as dt
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# Never load or modify the owner's records during tests.
TEMP = tempfile.TemporaryDirectory()
os.environ['DATA_DIR'] = TEMP.name
import primo
import main
import broadcast
from storage import read_json, write_json
from telegram.error import BadRequest


def update(user='123', chat=123):
    message = NS(reply_text=AsyncMock(), reply_document=AsyncMock(), sender_chat=None,
                 text='Hello', chat=NS(type='private'), photo=[], caption=None)
    person = NS(id=int(user))
    message.from_user = person
    return NS(message=message, effective_user=person,
              effective_chat=NS(id=chat, type='private'))


def context(args=None):
    return NS(args=args or [], bot=NS(send_message=AsyncMock(), send_document=AsyncMock()), bot_data={})


def user(**extra):
    return dict(user_id='123', request_count=0, subscription='inactive', sub_end=None,
                last_request_time=None, **extra)


class RegressionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        primo.user_data_cache.clear()
        
        main.aggregated_logs.clear()
        for path in Path(TEMP.name).glob('*.json'):
            path.unlink()

    async def test_generation_admin_only(self):
        u = update()
        with patch.object(primo, 'save_promo_code') as save:
            await primo.generate_promo(u, context())
            save.assert_not_called()

    async def test_claim_missing_argument(self):
        u = update()
        await primo.claim_promo(u, context())
        self.assertIn('Usage', u.message.reply_text.call_args.args[0])

    async def test_claim_before_start(self):
        u = update()
        await primo.claim_promo(u, context(['GPT-X-GPT']))
        self.assertIn('/start', u.message.reply_text.call_args.args[0])

    async def test_claim_persists_and_cannot_reuse(self):
        primo.user_data_cache['123'] = user()
        expiry = (dt.date.today() + dt.timedelta(days=2)).isoformat()
        primo.save_promo_codes([dict(code='GPT-X-GPT', expiry=expiry, used=False)])
        await primo.claim_promo(update(), context(['GPT-X-GPT']))
        self.assertEqual(primo.load_user_data()['123']['subscription'], 'active')
        self.assertTrue(primo.load_promo_codes()[0]['used'])
        primo.user_data_cache['456'] = dict(user(), user_id='456')
        await primo.claim_promo(update('456'), context(['GPT-X-GPT']))
        self.assertEqual(primo.user_data_cache['456']['subscription'], 'inactive')

    def test_expired_premium(self):
        u = user(); u.update(subscription='active', sub_end='2000-01-01')
        primo.normalize_user(u)
        self.assertEqual(u['subscription'], 'inactive')

    def test_expiry_includes_last_day(self):
        now = dt.datetime(2026, 9, 14, 23, tzinfo=dt.timezone.utc)
        u = user();u.update(subscription='active', sub_end='2026-09-14')
        primo.normalize_user(u, now)
        self.assertEqual(u['subscription'], 'active')

    def test_daily_reset(self):
        u = user();u.update(request_count=20, last_request_time='2000-01-01T00:00:00')
        primo.normalize_user(u)
        self.assertEqual(u['request_count'], 0)
        self.assertIsNone(u['quota_started_at'])

    def test_quota_window_does_not_slide(self):
        u = user();primo.user_data_cache['123'] = u
        primo.charge_request(u);start = u['quota_started_at']
        primo.charge_request(u)
        self.assertEqual(u['quota_started_at'], start)
        self.assertEqual(primo.load_user_data()['123']['request_count'], 2)









    def test_image_integration_removed(self):
        source = Path(main.__file__).read_text()
        self.assertFalse(hasattr(main, 'handle_image'))
        self.assertNotIn('filters.PHOTO', source)
        self.assertNotIn('compscilib', source)

    def test_atomic_storage_and_recovery(self):
        path = Path(TEMP.name)/'sample.json'
        write_json(path, {'ok':1})
        self.assertEqual(read_json(path, dict), {'ok':1})
        path.with_name(path.name+'.20260101_000000.backup').write_text('{"saved":2}')
        path.write_text('{broken')
        self.assertEqual(read_json(path, dict, recover=True), {'saved':2})
        with self.assertRaises(ValueError):
            read_json(path, dict)
        self.assertEqual(path.read_text(), '{broken')

    async def test_registration_survives_log_failure(self):
        u=update();c=context();c.bot.send_message.side_effect=BadRequest('no chat')
        await primo.log_user_data(u,c)
        self.assertIn('123',primo.load_user_data())
        u.message.reply_text.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
