"""Trusted code builders; generated programs execute only inside Sandbox."""
import json

BROWSER = '''import os, json, datetime
from pathlib import Path
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    options=dict(executable_path='/usr/bin/chromium',headless=True,timeout=15000,
        args=['--no-sandbox','--disable-dev-shm-usage','--renderer-process-limit=2',
              '--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader',
              '--proxy-bypass-list=<-loopback>'])
    if os.environ.get('HTTPS_PROXY'):options['proxy']={'server':os.environ['HTTPS_PROXY']}
    browser=p.chromium.launch(**options)
    page=browser.new_page(viewport={'width':1280,'height':900})
    errors=[]
    page.on('pageerror',lambda e:errors.append(str(e)[:500]))
'''

def browser_job(target,path,screenshot=None,actions=None,local=False,pdf=False):
    setup='TARGET='+repr(target)+'\nOUTPUT='+repr(path)+'\nACTIONS='+repr(actions or [])+'\nSHOT='+repr(screenshot)+'\n'
    code=BROWSER+'''    response=page.goto(TARGET,wait_until='domcontentloaded',timeout=25000)
    page.wait_for_timeout(1500)
    checks=[]
    for action in ACTIONS:
        kind=action['action'];selector=action.get('selector','')
        if kind=='click':page.locator(selector).click(timeout=5000)
        elif kind=='fill':page.locator(selector).fill(str(action['value']),timeout=5000)
        elif kind=='press':page.keyboard.press(str(action['value']))
        elif kind=='wait':page.wait_for_timeout(min(5000,max(0,int(action.get('value',500)))))
        elif kind=='expect_text':
            actual=page.locator(selector).inner_text(timeout=5000)
            assert str(action['value']) in actual, 'Expected '+str(action['value'])+'; actual '+actual[:300]
        elif kind=='expect_visible':assert page.locator(selector).is_visible(),selector+' not visible'
        else:raise ValueError('Unknown browser action')
        checks.append({'action':kind,'selector':selector,'passed':True})
    page.evaluate("async()=>{await Promise.race([Promise.all(Array.from(document.images).map(i=>i.complete?Promise.resolve():new Promise(r=>{i.onload=r;i.onerror=r}))),new Promise(r=>setTimeout(r,4000))]);if(document.fonts)await Promise.race([document.fonts.ready,new Promise(r=>setTimeout(r,4000))]);}")
    image_checks=page.locator('img').evaluate_all('(nodes)=>nodes.slice(0,50).map(n=>({src:n.currentSrc||n.src,loaded:n.complete&&n.naturalWidth>0}))')
    output=Path(OUTPUT);output.parent.mkdir(parents=True,exist_ok=True)
'''
    if pdf:
        code+='''    page.emulate_media(media='print')
    page.pdf(path=str(output),format='A4',print_background=True,margin={'top':'15mm','bottom':'15mm','left':'12mm','right':'12mm'})
    from pypdf import PdfReader
    reader=PdfReader(str(output))
    assert len(reader.pages)>0,'PDF has no pages'
    print(json.dumps({'saved':str(output),'pdf_pages':len(reader.pages),'text_sample':reader.pages[0].extract_text()[:1000],'errors':errors,'images':image_checks}))
'''
    else:
        code+='''    html=page.content()
    if len(html.encode())>2*1024*1024:raise ValueError('Rendered HTML exceeds 2 MB')
    output.write_text(html)
    if SHOT:
        image=Path(SHOT);image.parent.mkdir(parents=True,exist_ok=True);page.screenshot(path=str(image),full_page=True)
    print(json.dumps({'url':page.url,'status':response.status if response else None,'title':page.title(),
        'text':page.locator('body').inner_text(timeout=3000)[:6000],
        'links':page.locator('a[href]').evaluate_all('(nodes)=>nodes.slice(0,50).map(n=>n.href)'),
        'images':page.locator('img').evaluate_all('(nodes)=>nodes.filter(n=>n.currentSrc||n.src).slice(0,60).map(n=>({url:n.currentSrc||n.src,alt:n.alt,width:n.naturalWidth,height:n.naturalHeight}))'),
        'social_images':page.locator('meta[property="og:image"]').evaluate_all('(nodes)=>nodes.map(n=>n.content)'),
        'checks':checks,'page_errors':errors,'screenshot':SHOT,'fetched_at':datetime.datetime.now(datetime.timezone.utc).isoformat()}))
'''
    code+='    browser.close()\n'
    return setup+code

def video_job(url,path):
    return 'URL='+repr(url)+'\nOUTPUT='+repr(path)+'\n'+'''import os, json
from pathlib import Path
from yt_dlp import YoutubeDL
root=Path(OUTPUT);root.parent.mkdir(parents=True,exist_ok=True)
errors=[]
def progress(event):
    if event.get('downloaded_bytes',0)>=49_000_000:raise ValueError('Video exceeds download budget; use a smaller format')
class Quiet:
    def debug(self,msg):pass
    def warning(self,msg):pass
    def error(self,msg):errors.append(str(msg)[-600:])
for fmt in ['best[ext=mp4][height<=?720]/best[height<=?720]','best[ext=mp4][height<=?360]/worst']:
    try:
        options={'outtmpl':str(root),'format':fmt,'noplaylist':True,'playlist_items':'1',
            'max_filesize':49_000_000,'socket_timeout':12,'retries':2,'fragment_retries':2,
            'proxy':os.environ['HTTPS_PROXY'],'cachedir':False,'quiet':True,'logger':Quiet(),
            'progress_hooks':[progress],'js_runtimes':{'node':{}},'overwrites':True}
        with YoutubeDL(options) as y:
            info=y.extract_info(URL,download=True)
            actual=Path(y.prepare_filename(info))
        if not actual.exists() or not 0<actual.stat().st_size<50_000_000:raise ValueError('No complete video under 50 MB was downloaded')
        if actual!=root:actual.replace(root)
        print(json.dumps({'saved':str(root),'bytes':root.stat().st_size,'title':str(info.get('title',''))[:300],'source':URL,'format_id':info.get('format_id'),'attempt_errors':errors[-2:]}));break
    except Exception as error:
        errors.append(str(error)[-600:])
        for f in root.parent.glob(root.name+'*'):
            if f.is_file():f.unlink()
else:raise RuntimeError('Video download failed after two formats. Authentication/site blocks cannot be bypassed. '+'; '.join(errors[-2:]))
'''

def image_search_job(query,domain=''):
    from urllib.parse import urlencode
    targets=['https://www.google.com/search?'+urlencode({'q':query+(' site:'+domain if domain else ''),'udm':'2'}),
             'https://www.bing.com/images/search?'+urlencode({'q':query+(' site:'+domain if domain else '')})]
    return 'TARGETS='+repr(targets)+'\nDOMAIN='+repr(domain)+'\n'+BROWSER+'''    found=[];attempts=[]
    for target in TARGETS:
        try:
            response=page.goto(target,wait_until='domcontentloaded',timeout=25000);page.wait_for_timeout(1500)
            candidates=page.evaluate("""()=>{
                const out=[];
                for(const a of document.querySelectorAll('a')) {
                    try {const u=new URL(a.href);const image=u.searchParams.get('imgurl');if(image)out.push({url:image,source:u.searchParams.get('imgrefurl')||'',title:a.innerText});}catch(e){}
                    if(a.hasAttribute('m'))try{const x=JSON.parse(a.getAttribute('m'));if(x.murl)out.push({url:x.murl,source:x.purl||'',title:x.t||''});}catch(e){}
                }
                return out;
            }""")
            for item in candidates:
                if not item['url'].startswith('https://'):continue
                if DOMAIN:
                    from urllib.parse import urlsplit
                    host=(urlsplit(item['source']).hostname or '').lower()
                    if host!=DOMAIN and not host.endswith('.'+DOMAIN):continue
                if item['url'] not in [i['url'] for i in found]:found.append(item)
            attempts.append({'url':target,'status':response.status if response else None,'found':len(found)})
            if found:break
        except Exception as error:attempts.append({'url':target,'error':str(error)[:300]})
    print(json.dumps({'images':found[:12],'attempts':attempts,'note':'Only actual extracted original image URLs. If empty, browse the requested source directly; no fabricated results.'}))
    browser.close()
'''
