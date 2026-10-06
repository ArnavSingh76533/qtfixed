"""Bounded live HTML/JSON/text retrieval; response bodies are untrusted evidence."""
import asyncio
import datetime as dt
import http.client
from html.parser import HTMLParser
from urllib.parse import urljoin
from public_web import connect_public, web_url

MAX_BODY = 2 * 1024 * 1024

class PageText(HTMLParser):
    def __init__(self):
        super().__init__(); self.parts=[]; self.hidden=0; self.links=[]
    def handle_starttag(self, tag, attrs):
        if tag in ('script','style'):self.hidden+=1
        if tag=='a':
            href=dict(attrs).get('href')
            if href and len(self.links)<80:self.links.append(href)
    def handle_endtag(self, tag):
        if tag in ('script','style'):self.hidden=max(0,self.hidden-1)
    def handle_data(self, data):
        if not self.hidden and data.strip():self.parts.append(data.strip())


def fetch_page(url):
    current=url
    for redirect in range(6):
        parsed,port=web_url(current)
        cls=http.client.HTTPSConnection if parsed.scheme=='https' else http.client.HTTPConnection
        connection=cls(parsed.hostname,port,timeout=12)
        connection._create_connection=lambda address, timeout, source_address=None:connect_public(parsed.hostname,port,timeout)
        try:
            path=parsed.path or '/'
            if parsed.query:path+='?'+parsed.query
            connection.request('GET',path,headers={'User-Agent':'QuestionAi/1.0 (public page reader)', 'Accept':'text/html,application/json,text/plain;q=0.9', 'Accept-Encoding':'identity','Connection':'close'})
            response=connection.getresponse()
            if response.status in (301,302,303,307,308):
                location=response.getheader('Location')
                if not location:raise ValueError('Website sent a redirect without a destination.')
                current=urljoin(current,location);continue
            body=response.read(MAX_BODY+1)
            if len(body)>MAX_BODY:raise ValueError('Page exceeds the 2 MB fetch limit.')
            encoding=response.getheader('Content-Encoding','identity').lower()
            if encoding not in ('','identity'):raise ValueError('Website returned unsupported compressed content.')
            content_type=response.getheader('Content-Type','text/plain')
            if not any(kind in content_type.lower() for kind in ('text/','json','xml','javascript')):
                raise ValueError('This tool reads HTML, JSON, XML and text, not binary downloads.')
            charset='utf-8'
            for field in content_type.split(';')[1:]:
                if field.strip().lower().startswith('charset='):charset=field.split('=',1)[1].strip().strip('"')
            try:body_text=body.decode(charset,errors='replace')
            except LookupError:body_text=body.decode('utf-8',errors='replace')
            page=PageText()
            if 'html' in content_type.lower():page.feed(body_text)
            text='\n'.join(page.parts) if page.parts else body_text
            links=[]
            for href in page.links:
                absolute=urljoin(current,href)
                if absolute.startswith(('http://','https://')) and absolute not in links:links.append(absolute)
            return {'url':current,'status':response.status,'content_type':content_type,'fetched_at':dt.datetime.now(dt.timezone.utc).isoformat(),
                    'body':body_text,'text':text[:18000],'links':links[:40],
                    'note':'Live response. Scripts are not executed. A 403/429/challenge page is not score evidence.'}
        finally:connection.close()
    raise ValueError('Website redirected too many times.')

async def fetch(url):
    try:return await asyncio.wait_for(asyncio.to_thread(fetch_page,url),30)
    except http.client.HTTPException:raise ValueError('Website returned an invalid HTTP response.') from None
