"""Bounded requests, serialized per user to preserve history and quota correctness."""
import asyncio
import uuid
import logging
import config

logger = logging.getLogger(__name__)

def owner_of(key):
    return key[0] if isinstance(key, tuple) else key

def submit(context, owner, scope, work):
    data = context.application.bot_data
    active = data.setdefault('active_requests', {})
    if len(active) >= 200 or sum(owner_of(k) == owner for k in active) >= 8:
        return None
    locks = data.setdefault('request_locks', {})
    lock = locks.setdefault(owner, asyncio.Lock())
    slots = data.setdefault('request_slots', asyncio.Semaphore(config.MAX_CONCURRENT_REQUESTS))
    key = (owner, scope, uuid.uuid4().hex)
    async def run():
        try:
            async with lock:
                async with slots:
                    await work()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never emit request bodies or provider URLs/credentials in logs.
            logger.error('Queued request failed unexpectedly')
        finally:
            active.pop(key, None)
            if not any(owner_of(k) == owner for k in active): locks.pop(owner, None)
    task = asyncio.create_task(run())
    active[key] = task
    def finished(_):
        active.pop(key,None)
        if not any(owner_of(k)==owner for k in active):locks.pop(owner,None)
    task.add_done_callback(finished)
    return task

async def cancel(context, owner, scope=None):
    active = context.application.bot_data.get('active_requests', {})
    tasks = [v for k,v in active.items() if owner_of(k)==owner and
             (scope is None or not isinstance(k,tuple) or k[1]==scope)]
    for task in tasks: task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    for key,task in list(active.items()):
        if task in tasks: active.pop(key,None)
    return bool(tasks)
