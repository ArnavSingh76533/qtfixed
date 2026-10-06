"""Owner-bound tool registry. Tool arguments cannot select another user or host path."""
import ast
import asyncio
import base64
import datetime as dt
import json
import math
import operator
import re
import time
import uuid
from agent_scheduler import create_reminder
from agent_sandbox import validate_files
from skill_bundles import safe_path
from provider import ProviderError
from image_generation import generate_images, available
from answer_engine import usable_evidence
import config

def spec(name,description,properties,required=()):
    return {'type':'function','function':{'name':name,'description':description,'parameters':
        {'type':'object','properties':{k:{'type':v} for k,v in properties.items()},'required':list(required),'additionalProperties':False}}}

TOOLS=[
    spec('calculator','Calculate arithmetic exactly as given; no Python execution.',{'expression':'string'},['expression']),
    spec('current_time','Get current UTC time and configured user timezone.',{}),
    spec('list_files','List this task workspace, including selected skill resources.',{}),
    spec('list_resources','List the actual installed skills and agent instructions for this user, including whether each is enabled. Use this before answering questions about installed resources.',{}),
    spec('read_file','Read a UTF-8 workspace resource; offset is characters.',{'path':'string','offset':'integer'},['path']),
    spec('write_file','Create/update a UTF-8 workspace file; no host filesystem access.',{'path':'string','content':'string'},['path','content']),
    spec('export_file','Make a completed workspace file downloadable to the requesting user.',{'path':'string'},['path']),
    spec('python','Run Python in the isolated container. Set network=true for live public HTTP/HTTPS via its proxy (requests/urllib). No host files or credentials. numpy, matplotlib (Agg), Pillow, requests and bs4 are installed. Save plots with plt.savefig, not plt.show. Optional packages is a space-separated supported PyPI list installed only in this container; use network=true.',{'code':'string','network':'boolean','packages':'string'},['code']),
    spec('shell','Run shell only in the isolated container workspace. Set network=true for proxy-aware public HTTP/HTTPS. No host access. Optional packages installs supported PyPI dependencies in this temporary container; use network=true.',{'command':'string','network':'boolean','packages':'string'},['command']),
    spec('fetch_url','Fetch the actual live HTML/JSON/text at a public URL, save the raw body to path, and return readable text, links, status and timestamp. Read the saved file in sections for scripts/embedded data. No browser JavaScript execution.',{'url':'string','path':'string'},['url','path']),
    spec('browse_url','Render a public web page with sandboxed headless Chromium when fetch_url lacks JavaScript-generated content. Save rendered HTML to path and return visible text, links, HTTP status and timestamp. No logins or CAPTCHA bypass.',{'url':'string','path':'string'},['url','path']),
    spec('inspect_page','Test a workspace HTML file in Chromium. actions is a JSON array of click/fill/press/wait/expect_text/expect_visible with selector and value. Save screenshot_path; return checks and JS errors. Use network=true for CDNs such as Three.js.',{'path':'string','screenshot_path':'string','actions':'string','network':'boolean'},['path','screenshot_path']),
    spec('create_pdf','Create and verify a real PDF from workspace HTML, including local images. Use network=true for remote images. Queues the PDF for this chat.',{'html_path':'string','path':'string','network':'boolean'},['html_path','path']),
    spec('search_images','Find actual original image URLs using Google, then Bing if needed. Optional source_domain restricts results, e.g. pinterest.com. Download selected result and send_media or embed local downloaded image in PDF.',{'query':'string','source_domain':'string'},['query']),
    spec('download_video','Download a public video page with installed yt-dlp, Node JS solver and FFmpeg. Tries two formats under 50 MB, stores actual file; call send_media. No cookies/logins/DRM bypass.',{'url':'string','path':'string'},['url','path']),
    spec('web_search','Search current facts; synthesize returned evidence with sources.',{'query':'string'},['query']),
    spec('generate_image','Generate requested images and embed them in this chat after completion.',{'prompt':'string'},['prompt']),
    spec('download_media','Download a public image or direct MP4/WebM URL under 50 MB into the task workspace; respects global Web. Use send_media afterwards.',{'url':'string','path':'string'},['url','path']),
    spec('send_media','Queue an actual workspace image/video under 50 MB for delivery in this chat. Must exist; no invented paths. Graphs use Python savefig then send_media.',{'path':'string','caption':'string'},['path']),
    spec('set_work_plan','Refine your own temporary technical working instructions/subtasks as evidence changes. Subordinate to the original user request; cannot grant permissions or change user/global prompts.',{'brief':'string'},['brief']),
    spec('memory_list','Recall memories for this user in this conversation scope only.',{}),
    spec('memory_save','Only when the user explicitly asks to remember/save: save a fact in this scope.',{'name':'string','value':'string'},['name','value']),
    spec('memory_delete','Only when explicitly asked to forget: delete a named memory in this scope.',{'name':'string'},['name']),
    spec('schedule_reminder','Only when explicitly requested: schedule reminder text to the user bot DM. One ISO when OR a five-field cron; no commands are executed.',{'text':'string','when':'string','cron':'string','timezone':'string'},['text']),
    spec('list_reminders','List only this user reminders.',{}),
    spec('cancel_reminder','Cancel this user reminder by ID when explicitly asked.',{'id':'string'},['id']),
]

def calculate(expression):
    if not isinstance(expression,str) or len(expression)>300:raise ValueError('Expression too long.')
    tree=ast.parse(expression,mode='eval')
    if len(list(ast.walk(tree)))>80:raise ValueError('Expression too complex.')
    operations={ast.Add:operator.add,ast.Sub:operator.sub,ast.Mult:operator.mul,ast.Div:operator.truediv,ast.Mod:operator.mod}
    def visit(node):
        if isinstance(node,ast.Constant) and type(node.value) in (int,float):value=node.value
        elif isinstance(node,ast.UnaryOp) and isinstance(node.op,(ast.UAdd,ast.USub)):value=visit(node.operand)*(1 if isinstance(node.op,ast.UAdd) else -1)
        elif isinstance(node,ast.BinOp):
            left,right=visit(node.left),visit(node.right)
            if isinstance(node.op,ast.Pow):
                if abs(right)>12:raise ValueError('Exponent must be between -12 and 12.')
                value=left**right
            elif type(node.op) in operations:value=operations[type(node.op)](left,right)
            else:raise ValueError('Unsupported arithmetic operator.')
        else:raise ValueError('Only numbers and arithmetic operators are allowed.')
        if isinstance(value,complex) or abs(value)>1e100 or not math.isfinite(value):raise ValueError('Result outside supported range.')
        return value
    return visit(tree.body)

def cleanup_artifacts(application,now=None):
    now=time.monotonic() if now is None else now
    cache=application.bot_data.get('agent_artifacts',{})
    for key in list(cache):
        if now-cache[key]['created']>3600:cache.pop(key,None)

def artifact(context,owner,path,data):
    cache=context.application.bot_data.setdefault('agent_artifacts',{})
    now=time.monotonic()
    for key in list(cache):
        if now-cache[key]['created']>3600:cache.pop(key,None)
    # Bound process-wide artifact retention to 64 MB.
    while cache and (len(cache)>=100 or sum(len(v['data']) for v in cache.values())+len(data)>64*1024*1024):cache.pop(next(iter(cache)))
    token=uuid.uuid4().hex
    cache[token]={'owner':owner,'name':path.rsplit('/',1)[-1],'data':data,'created':now}
    return f'https://t.me/{context.bot.username}?start=file_{token}'

def memory_authorized(request, operation, target):
    """Direct operation + named target, never generic file-save permission."""
    text=re.sub(r'```[\s\S]*?```', '', request)
    text=re.split(r'\b(?:containing|with content|content is)\b',text,flags=re.I)[0]
    words=lambda value: set(re.findall(r'[\w]+',re.sub(r'([a-z])([A-Z])',r'\1 \2',value).replace('_',' ').casefold()))
    target_words=words(target)-{'my','the','memory','user'}
    if not target_words:return False
    pattern=(r'\b(?:remember|memorize|save\s+(?:in\s+)?(?:my\s+)?memory|store\s+(?:in\s+)?(?:my\s+)?memory)\b|याद\s+रख'
             if operation=='memory_save' else
             r'\b(?:forget|delete\s+(?:my\s+)?memory|remove\s+(?:my\s+)?memory|clear\s+(?:my\s+)?memory)\b|भूल')
    for match in re.finditer(pattern,text,re.I):
        before=re.split(r'[;\n]|\b(?:and then|then)\b',text[:match.start()],flags=re.I)[-1]
        # Mutation verbs must be direct commands, not words in a file name/payload.
        if not re.fullmatch(r'\s*(?:(?:agent|एजेंट)\s*[:,;-]?\s*)?(?:(?:please|can you|could you|will you|would you|I want you to)\s+)*',before,re.I):continue
        if re.search(r"(?:don't|do not|never|not|मत)\s+(?:please\s+)?$",before,re.I):continue
        # Quoted instructions or filenames are data, not a direct command.
        if before.count('"')%2 or before.count('`')%2:continue
        subject=re.split(r'[;\n]|\b(?:and then|then|containing)\b',text[match.end():],maxsplit=1,flags=re.I)[0]
        if re.search(r'\b(?:file|script|code)\b|\.[a-zA-Z0-9]{1,8}\b',subject,re.I):continue
        if target_words.issubset(words(subject)):return True
    return False

def recover_artifacts(context, owner, run_id):
    cache=context.application.bot_data.get('agent_artifacts',{})
    result=[]
    for row in context.application.bot_data['agent_store'].artifacts(owner,run_id):
        item=cache.get(row['token'])
        if (row['expires']>time.time() and item and item['owner']==owner
                and time.monotonic()-item['created']<=3600):
            result.append((row['path'],f'https://t.me/{context.bot.username}?start=file_{row["token"]}'))
    return result

class ToolRuntime:
    def __init__(self,context,owner,user,scope,request,settings):
        self.context,self.owner,self.user,self.scope=context,owner,user,scope
        self.request,self.settings=request,settings
        self.store=context.application.bot_data['agent_store']
        self.files={};self.exports={};self.exported_content={};self.images=[];self.media=[];self.documents=[];self.working_brief="";self.calls=0;self.run_id=None
    def export(self,path,raw):
        if self.exported_content.get(path)!=self.files[path]:
            self.exports[path]=artifact(self.context,self.owner,path,raw)
            self.exported_content[path]=self.files[path]
            if self.run_id:
                token=self.exports[path].split('file_',1)[1]
                self.store.add_artifact(self.owner,self.run_id,path,token)
        return {'download':self.exports[path]}
    def registry(self):
        disabled=set()
        if not self.settings['web']:disabled.update(('web_search','fetch_url','browse_url','download_media','search_images','download_video'))
        if not available():disabled.add('generate_image')
        if not config.SANDBOX_ENABLED:disabled.update(('python','shell','inspect_page','create_pdf','download_video','search_images'))
        if not config.SANDBOX_ENABLED or not config.SANDBOX_WEB_ENABLED:disabled.update(('browse_url','search_images','download_video'))
        return [s for s in TOOLS if s['function']['name'] not in disabled]
    def check_access(self):
        import primo
        primo.normalize_user(self.user)
        if self.user.get('subscription')!='active' or not self.store.prefs(self.owner)['enabled']:
            raise ProviderError('Agent mode is disabled or premium has expired. Enable it in your private /settings.')
    async def execute(self,name,args):
        self.check_access();self.calls+=1
        if self.calls>config.AGENT_MAX_TOOL_CALLS:raise ProviderError('Agent tool budget reached; no further actions were taken.')
        schemas={s['function']['name']:s['function']['parameters'] for s in self.registry()}
        if name not in schemas:raise ValueError('That tool is not enabled.')
        schema=schemas[name]
        if not isinstance(args,dict) or set(args)-set(schema['properties']) or not set(schema['required']).issubset(args):raise ValueError('Invalid tool arguments.')
        for key,value in args.items():
            expected={'string':str,'integer':int,'boolean':bool}[schema['properties'][key]['type']]
            if type(value)!=expected:raise ValueError('Invalid tool argument type.')
        if name=='set_work_plan':
            brief=args['brief'].strip()
            if not brief or len(brief)>4000:raise ValueError('Working brief must contain 1–4,000 characters.')
            self.working_brief=brief
            return {'working_brief':brief,'authorization':'Original user request and existing tool restrictions still apply.'}
        if name=='download_media':
            from agent_media import download,media_kind
            path=safe_path(args['path']);raw,url=await download(args['url'])
            kind=await asyncio.to_thread(media_kind,raw,path)
            proposed={**self.files,path:base64.b64encode(raw).decode()};validate_files(proposed);self.files=proposed
            return {'saved':path,'bytes':len(raw),'kind':kind,'source':url,'next':'Call send_media to deliver the actual file.'}
        if name=='send_media':
            from agent_media import media_kind
            path=safe_path(args['path'])
            if path not in self.files:raise ValueError('Media file does not exist. Run code or download_media first.')
            raw=base64.b64decode(self.files[path]);kind=await asyncio.to_thread(media_kind,raw,path)
            if not any(item['path']==path for item in self.media):
                if len(self.media)>=4:raise ValueError('At most four media attachments per task.')
                self.media.append({'path':path,'data':raw,'kind':kind,'caption':args.get('caption',path)[:900]})
            receipt=self.export(path,raw)
            return {'queued_media':path,'kind':kind,'bytes':len(raw),'download':receipt['download'],'delivery':'The bot will attach this actual file after the answer; queued is not yet delivered.'}
        if name=='calculator':return {'result':calculate(args['expression'])}
        if name=='current_time':return {'utc':dt.datetime.now(dt.timezone.utc).isoformat(),'timezone':self.store.prefs(self.owner)['timezone']}
        if name=='list_files':return {'files':list(self.files)}
        if name=='list_resources':return {'resources':[{'kind':b['kind'],'name':b['name'],'description':b['description'],'enabled':bool(b['enabled'])} for b in self.store.effective_bundles(self.owner)]}
        if name in ('read_file','write_file','export_file'):
            path=safe_path(args['path'])
            if name=='write_file':
                if len(args['content'])>100000:raise ValueError('Use smaller file sections.')
                proposed={**self.files,path:base64.b64encode(args['content'].encode()).decode()};validate_files(proposed)
                self.files=proposed;return {'saved':path}
            if path not in self.files:raise ValueError('File does not exist in this task.')
            raw=base64.b64decode(self.files[path])
            if name=='export_file':
                receipt=self.export(path,raw)
                if not any(i['path']==path for i in self.documents) and not any(i['path']==path for i in self.media):
                    self.documents.append({'path':path,'data':raw,'kind':'document','caption':path})
                return {**receipt,'queued_document':path}
            start=max(0,args.get('offset',0));text=raw.decode('utf-8')
            return {'content':text[start:start+14000],'total_characters':len(text),'next_offset':start+14000 if start+14000<len(text) else None}
        if name in ('browse_url','inspect_page','create_pdf','search_images','download_video'):
            from agent_jobs import browser_job,video_job,image_search_job
            from public_web import web_url
            network=name in ('browse_url','search_images','download_video') or args.get('network',False)
            if network and not self.settings['web']:raise ValueError('The owner disabled web access.')
            if name=='download_video':
                web_url(args['url']);path=safe_path(args['path'])
                if not path.lower().endswith(('.mp4','.webm')):raise ValueError('Use an MP4/WebM output filename.')
                code=video_job(args['url'],path)
            elif name=='search_images':
                query=args['query'].strip()[:1000];domain=args.get('source_domain','').lower().strip()
                if not query or (domain and not re.fullmatch(r'[a-z0-9]+(?:[.-][a-z0-9]+)*\.[a-z]{2,}',domain)):raise ValueError('Provide a query and optional public source domain.')
                code=image_search_job(query,domain);path=None
            elif name=='browse_url':
                web_url(args['url']);path=safe_path(args['path']);code=browser_job(args['url'],path)
            else:
                source=safe_path(args['html_path'] if name=='create_pdf' else args['path'])
                if source not in self.files:raise ValueError('Write the HTML file before browser testing or PDF creation.')
                path=safe_path(args['path']) if name=='create_pdf' else safe_path(args['screenshot_path'])
                actions=json.loads(args.get('actions','[]'))
                if not isinstance(actions,list) or len(actions)>30 or any(not isinstance(a,dict) for a in actions):raise ValueError('Browser actions must be a JSON array of up to 30 objects.')
                if name=='create_pdf' and not path.lower().endswith('.pdf'):raise ValueError('Use a .pdf output filename.')
                if name=='inspect_page' and not path.lower().endswith('.png'):raise ValueError('Screenshot must have a .png filename.')
                code=browser_job('file:///workspace/'+source,path if name=='create_pdf' else 'rendered-'+source,
                    screenshot=path if name=='inspect_page' else None,actions=actions,pdf=name=='create_pdf')
            result=await self.context.application.bot_data['sandbox'].execute(code,self.files,'python',network=network)
            self.files=result.pop('files')
            if result['exit_code']!=0:return {**result,'error':'Tool execution failed. Inspect stderr; repair or try another available method. No successful result was verified.'}
            try:receipt=json.loads(result['stdout'])
            except ValueError:raise ValueError('Tool result was unreadable. Inspect saved files and retest.') from None
            if name=='create_pdf':
                if path not in self.files:raise ValueError('The PDF file was not returned by the sandbox.')
                raw=base64.b64decode(self.files[path])
                if not raw.startswith(b'%PDF-'):raise ValueError('Output was not a PDF.')
                exported=self.export(path,raw)
                if not any(d['path']==path for d in self.documents):self.documents.append({'path':path,'data':raw,'kind':'document','caption':path})
                receipt.update(exported,queued_document=path)
            if name=='inspect_page' and receipt.get('page_errors'):
                receipt['error']='Browser reported JavaScript errors. Fix and retest before claiming the app works.'
            if name=='search_images' and not receipt.get('images'):
                receipt['error']='No original images found. Try browse_url on the requested source, inspect its images/embedded HTML, or ask for a specific public pin URL.'
            return {**receipt,'saved':path,'untrusted_content':True}
        if name=='fetch_url':
            from agent_web import fetch
            path=safe_path(args['path'])
            result=await fetch(args['url'])
            body=result.pop('body')
            proposed={**self.files,path:base64.b64encode(body.encode()).decode()};validate_files(proposed)
            self.files=proposed
            return {**result,'saved':path,'characters':len(body),'untrusted_content':True}
        if name in ('python','shell'):
            network=args.get('network',False)
            if network and not self.settings['web']:raise ValueError('The owner disabled web access. Offline execution is still available.')
            options={'network':network}
            if args.get('packages'):
                from sandbox_dependencies import requirements
                options['packages']=requirements(args['packages'])
            result=await self.context.application.bot_data['sandbox'].execute(args.get('code',args.get('command')),self.files,name,**options)
            self.files=result.pop('files')
            if result['exit_code']!=0:result['error']='Code test failed; inspect stderr, fix the code and retest. Do not claim success.'
            return {**result,'files':list(self.files)}
        if name=='web_search':
            query=args['query'][:4000];evidence=''
            try:evidence=await self.context.application.bot_data['web'].search(query)
            except ProviderError:pass
            if not usable_evidence(evidence):evidence=await self.context.application.bot_data['groq'].browser_search(query)
            if not usable_evidence(evidence):raise ValueError('Both web sources returned insufficient evidence.')
            return {'untrusted_evidence':evidence[:18000]}
        if name=='generate_image':
            if self.images:raise ValueError('Images already generated for this task.')
            self.images=await generate_images(self.context,self.owner,self.user,args['prompt'],self.request)
            return {'generated':len(self.images),'delivery':'Will be attached to the final answer by the bot.'}
        if name=='memory_list':return self.store.memories(self.owner,self.scope)
        if name in ('memory_save','memory_delete'):
            if not memory_authorized(self.request,name,args['name']):raise ValueError('Memory changes require a direct remember/forget request naming this specific memory. Saving a file does not grant memory permission.')
            if name=='memory_save':self.store.remember(self.owner,self.scope,args['name'],args['value'])
            else:self.store.forget(self.owner,self.scope,args['name'])
            return {'saved':True,'scope':self.scope}
        if name=='list_reminders':return self.store.schedules(self.owner)
        if name in ('schedule_reminder','cancel_reminder'):
            if not re.search(r'\b(remind|reminder|schedule|cron|cancel|daily|weekly|yaad)\b|याद',self.request,re.I):raise ValueError('The current user request must explicitly ask for reminder scheduling/cancellation.')
            if name=='cancel_reminder':return {'cancelled':bool(self.store.cancel_schedule(self.owner,args['id']))}
            if not self.user.get('dm_started',True):raise ValueError('Open the bot DM and /start before scheduling reminders.')
            return create_reminder(self.store,self.owner,args['text'],args.get('when'),args.get('cron'),args.get('timezone'))
        raise ValueError('Unsupported tool.')
