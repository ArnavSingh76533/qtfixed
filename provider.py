"""Groq SSE client. Only final-answer content is forwarded to Telegram."""
import asyncio
import json
import httpx
import config


class ProviderError(Exception):
    """Safe, user-facing error without credentials or provider response bodies."""


class WebSearchRequested(Exception):
    def __init__(self, query):
        self.query = query


class GroqClient:
    def __init__(self, api_key, client=None):
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(60, connect=15),
            limits=httpx.Limits(max_connections=30, max_keepalive_connections=15))
        self.api_key = api_key

    async def close(self):
        await self.client.aclose()

    async def stream(self, messages, *, reasoning='medium', allow_web=False, allow_image=False):
        payload = {'model': config.GROQ_MODEL, 'messages': messages, 'stream': True,
                   'max_completion_tokens': config.MAX_OUTPUT_TOKENS}
        if config.GROQ_MODEL.startswith('openai/gpt-oss-'):
            payload.update(include_reasoning=False, reasoning_effort=reasoning)
        tools=[]
        if allow_web:
            tools.append({'type':'function','function':{'name':'web_search',
                'description':'Answer questions requiring current facts, news, prices, schedules or web search.',
                'parameters':{'type':'object','properties':{'query':{'type':'string'}},'required':['query']}}})
        if allow_image:
            tools.append({'type':'function','function':{'name':'generate_image',
                'description':'Generate a picture when the user asks you to create, draw or imagine an image. Preserve all requested visual details.',
                'parameters':{'type':'object','properties':{'prompt':{'type':'string'}},'required':['prompt']}}})
        if tools:
            payload.update(tools=tools,tool_choice='auto',parallel_tool_calls=False)
        tool_name = ''
        tool_arguments = ''
        headers = {'Authorization' : f'Bearer {self.api_key}', 'Content-Type': 'application/json'}
        emitted = False
        for attempt in range(3):
            tool_name = ''
            tool_arguments = ''
            finished = False
            try:
                async with self.client.stream('POST', 'https://api.groq.com/openai/v1/chat/completions',
                                              headers=headers, json=payload) as response:
                    status = response.status_code
                    if status in (401, 403):
                        raise ProviderError('The AI service rejected its credentials. Ask the admin to check the configuration.')
                    if status in (429, 500, 502, 503, 504):
                        if attempt == 2:
                            raise ProviderError('The AI service is busy or the account rate limit was reached. Please try again later.')
                        try:
                            delay = min(15, max(1, float(response.headers.get('retry-after', 2 ** attempt))))
                        except ValueError:
                            delay = 2 ** attempt
                    elif status != 200:
                        raise ProviderError(f'The AI service could not accept this request (HTTP {status}). Ask the admin to check model and token settings.')
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
                                raise ProviderError('The AI service interrupted the response. Please retry.')
                            choices = event.get('choices') or []
                            if not choices:
                                continue
                            choice = choices[0]
                            delta = choice.get('delta') or {}
                            for call in delta.get('tool_calls') or []:
                                if call.get('index',0) != 0:
                                    continue
                                function = call.get('function') or {}
                                tool_name += function.get('name') or ''
                                tool_arguments += function.get('arguments') or ''
                                if len(tool_arguments)>10000:
                                    raise ProviderError('The web query was too large.')
                            content = delta.get('content')
                            if isinstance(content, str) and content:
                                emitted = True
                                yield content
                            reason = choice.get('finish_reason')
                            if reason:
                                finished = True
                                if reason == 'length':
                                    yield '\n\n_Response reached the output limit. Ask me to continue._'
                        if tool_name:
                            args = json.loads(tool_arguments)
                            if tool_name == 'generate_image' and allow_image:
                                from image_generation import ImageRequested
                                prompt = args.get('prompt') if isinstance(args,dict) else None
                                if not isinstance(prompt,str) or not prompt.strip() or len(prompt)>4000:
                                    raise ProviderError('Please use /image followed by your image description.')
                                raise ImageRequested(prompt.strip())
                            query = args.get('query') if isinstance(args,dict) else None
                            if tool_name != 'web_search' or not isinstance(query,str) or not query.strip() or len(query)>4000:
                                raise ProviderError('Could not prepare a valid web search. Try /web followed by your question.')
                            raise WebSearchRequested(query.strip())
                        if not finished:
                            raise ProviderError('The connection ended before the answer completed. Please retry.')
                        if not emitted:
                            raise ProviderError('The assistant returned an empty answer. Please retry.')
                        return
                await asyncio.sleep(delay)
            except httpx.RequestError:
                if emitted or attempt == 2:
                    raise ProviderError('Could not complete the connection to the AI service. Please try again.') from None
                await asyncio.sleep(2 ** attempt)
            except (ValueError, TypeError, KeyError, AttributeError):
                raise ProviderError('The AI service returned an unreadable response. Please retry.') from None
