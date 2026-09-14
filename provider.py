"""Groq SSE client. Only final-answer content is forwarded to Telegram."""
import asyncio
import json
import httpx
import config


class ProviderError(Exception):
    """Safe, user-facing error without credentials or provider response bodies."""


class GroqClient:
    def __init__(self, api_key, client=None):
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(60, connect=15),
            limits=httpx.Limits(max_connections=30, max_keepalive_connections=15))
        self.api_key = api_key

    async def close(self):
        await self.client.aclose()

    async def stream(self, messages, *, reasoning='medium'):
        payload = {'model': config.GROQ_MODEL, 'messages': messages, 'stream': True,
                   'max_completion_tokens': config.MAX_OUTPUT_TOKENS}
        if config.GROQ_MODEL.startswith('openai/gpt-oss-'):
            payload.update(include_reasoning=False, reasoning_effort=reasoning)
        headers = {'Authorization': f'Bearer {self.api_key}', 'Content-Type': 'application/json'}
        emitted = False
        for attempt in range(3):
            finished = False
            try:
                async with self.client.stream('POST', 'https://api.groq.com/openai/v1/chat/completions',
                                              headers=headers, json=payload) as response:
                    status = response.status_code
                    if status in (401, 403):
                        raise ProviderError('Groq rejected the API key or model permissions. Ask the admin to check the configuration.')
                    if status in (429, 500, 502, 503, 504):
                        if attempt == 2:
                            raise ProviderError('Groq is busy or the account rate limit was reached. Please try again later.')
                        try:
                            delay = min(15, max(1, float(response.headers.get('retry-after', 2 ** attempt))))
                        except ValueError:
                            delay = 2 ** attempt
                    elif status != 200:
                        raise ProviderError(f'Groq could not accept this request (HTTP {status}). Ask the admin to check model and token settings.')
                    else:
                        async for line in response.aiter_lines():
                            if not line.startswith('data:'):
                                continue
                            raw = line[5:].strip()
                            if not raw:
                                continue
                            if raw == '[DONE]':
                                finished = True
                                break
                            event = json.loads(raw)
                            if event.get('error'):
                                raise ProviderError('Groq interrupted the response. Please retry.')
                            choices = event.get('choices') or []
                            if not choices:
                                continue
                            choice = choices[0]
                            content = (choice.get('delta') or {}).get('content')
                            if isinstance(content, str) and content:
                                emitted = True
                                yield content
                            reason = choice.get('finish_reason')
                            if reason:
                                finished = True
                                if reason == 'length':
                                    yield '\n\n_Response reached the output limit. Ask me to continue._'
                        if not finished:
                            raise ProviderError('The connection ended before the answer completed. Please retry.')
                        if not emitted:
                            raise ProviderError('Groq returned an empty answer. Please retry.')
                        return
                await asyncio.sleep(delay)
            except httpx.RequestError:
                if emitted or attempt == 2:
                    raise ProviderError('Could not complete the connection to Groq. Please try again.') from None
                await asyncio.sleep(2 ** attempt)
            except (ValueError, TypeError, KeyError, AttributeError):
                raise ProviderError('Groq returned an unreadable response. Please retry.') from None
