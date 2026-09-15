"""User-supplied Felo workflow with bounded SSE reconnects and no replay duplication."""
import asyncio
import json
import secrets
import time
import uuid
from urllib.parse import quote
import httpx
from provider import ProviderError


async def sse_events(response):
    data = []
    async for line in response.aiter_lines():
        if not line:
            if data:
                yield '\n'.join(data)
                data = []
        elif line.startswith('data:'):
            data.append(line[5:].lstrip())
    if data:
        yield '\n'.join(data)


def decode_event(raw):
    if raw == '[DONE]':
        return '', True
    outer = json.loads(raw)
    done = outer.get('status') == 'completed' or outer.get('type') == 'completed'
    content = outer.get('content', {})
    if isinstance(content,str):
        content = json.loads(content)
    payload = content.get('data', {}) if isinstance(content,dict) else {}
    done = done or payload.get('type') in ('completed','done')
    body = payload.get('data') or {}
    text = body.get('text', '') if payload.get('type') == 'answer' and isinstance(body,dict) else ''
    return text if isinstance(text,str) else '', done


class FeloClient:
    def __init__(self, client=None):
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(20,connect=10))

    async def close(self):
        await self.client.aclose()

    async def search(self, query):
        async def run():
            request_id=secrets.token_urlsafe(16)
            payload={'query':query,'search_uuid':request_id,'lang':'','agent_lang':'en',
                'search_options':{'langcode':'en-GB'},'search_video':True,'query_from':'default',
                'category':'google','model':'','auto_routing':True,'mode':'concise',
                'device_id':uuid.uuid4().hex,'source_message_rid':'','documents':[],
                'document_action':'','slides_source':{'type':'ask_question','files':{}},
                'slide_template_uid':'','selected_resource_ids':[],'process_id':request_id,
                'stream_protocol':'message_center_v1','enable_task_state':True}
            headers={'user-agent':'Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36 Chrome/150.0.0.0 Mobile Safari/537.36',
                     'accept-language':'en-GB,en;q=0.5','origin':'https://felo.ai','content-type':'application/json'}
            response=await self.client.post('https://felo.ai/api-proxy/main/search/threads',headers=headers,json=payload)
            if response.status_code != 200:
                raise ProviderError(f'Web search is unavailable (HTTP {response.status_code}). Try again later; no current facts were verified.')
            data=response.json()
            stream_id=data.get('stream_key') or data.get('message_id')
            if not isinstance(stream_id,str) or not stream_id:
                raise ProviderError('Web search did not return a stream ID. Please retry.')
            url='https://felo.ai/api/message/v1/stream/'+quote(stream_id,safe='')+'?offset=0'
            latest=''
            for attempt in range(4):
                # offset=0 replays from the beginning: rebuild rather than append to prior attempts.
                current=''
                seen=set()
                try:
                    async with self.client.stream('GET',url,headers={**headers,'accept':'text/event-stream'},timeout=15) as result:
                        if result.status_code != 200:
                            raise ProviderError(f'Web search stream is unavailable (HTTP {result.status_code}).')
                        async for raw in sse_events(result):
                            try:
                                event=json.loads(raw) if raw != '[DONE]' else {}
                                event_id=event.get('event_id') or event.get('sequence_id')
                                if event_id is not None:
                                    if str(event_id) in seen:continue
                                    seen.add(str(event_id))
                                chunk,done=decode_event(raw)
                            except (ValueError,TypeError,AttributeError):
                                continue
                            current+=chunk
                            if len(current)>100000:
                                raise ProviderError('Web response exceeded the size limit. Ask a narrower question.')
                            if done and current.strip():
                                return current.strip()
                            if done:
                                raise ProviderError('Web search completed without an answer.')
                except httpx.TimeoutException:
                    pass
                latest=current if len(current)>len(latest) else latest
                if attempt<3:await asyncio.sleep(2)
            if latest.strip():
                return latest.strip()+'\n\n_Web search stream ended early; this result may be incomplete._'
            raise ProviderError('Web search returned no answer. Please retry.')
        try:
            return await asyncio.wait_for(run(),65)
        except (asyncio.TimeoutError,httpx.RequestError):
            raise ProviderError('Web search timed out or could not connect. Please retry.') from None
        except (ValueError,TypeError,AttributeError):
            raise ProviderError('Web search returned an unreadable response. Please retry.') from None
