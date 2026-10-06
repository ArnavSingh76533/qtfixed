"""Owner-bound tool registry. Tool arguments cannot select another user or host path."""
import ast
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
    spec('python','Run Python in the isolated container. Set network=true for live public HTTP/HTTPS via its proxy (requests/urllib). No host files or credentials. requests, bs4 and openpyxl are installed.',{'code':'string','network':'boolean'},['code']),
    spec('shell','Run shell only in the isolated container workspace. Set network=true for proxy-aware public HTTP/HTTPS. No host access.',{'command':'string','network':'boolean'},['command']),
    spec('fetch_url','Fetch the actual live HTML/JSON/text at a public URL, save the raw body to path, and return readable text, links, status and timestamp. Read the saved file in sections for scripts/embedded data. No browser JavaScript execution.',{'url':'string','path':'string'},['url','path']),
    spec('browse_url','Render a public web page with sandboxed headless Chromium when fetch_url lacks JavaScript-generated content. Save rendered HTML to path and return visible text, links, HTTP status and timestamp. No logins or CAPTCHA bypass.',{'url':'string','path':'string'},['url','path']),
    spec('web_search','Search current facts; synthesize returned evidence with sources.',{'query':'string'},['query']),
    spec('generate_image','Generate requested images and embed them in this chat after completion.',{'prompt':'string'},['prompt']),
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
        self.files={};self.exports={};self.exported_content={};self.images=[];self.calls=0;self.run_id=None
    def registry(self):
        disabled=set()
        if not self.settings['web']:disabled.update(('web_search','fetch_url','browse_url'))
        if not available():disabled.add('generate_image')
        if not config.SANDBOX_ENABLED:disabled.update(('python','shell'))
        if not config.SANDBOX_ENABLED or not config.SANDBOX_WEB_ENABLED:disabled.add('browse_url')
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
        if name=='calculator':return {'result':calculate(args['expression'])}
        if name=='current_time':return {'utc':dt.datetime.now(dt.timezone.utc).isoformat(),'timezone':self.store.prefs(self.owner)['timezone']}
        if name=='list_files':return {'files':list(self.files)}
        if name=='list_resources':return {'resources':[{'kind':b['kind'],'name':b['name'],'description':b['description'],'enabled':bool(b['enabled'])} for b in self.store.bundles(self.owner)]}
        if name in ('read_file','write_file','export_file'):
            path=safe_path(args['path'])
            if name=='write_file':
                if len(args['content'])>100000:raise ValueError('Use smaller file sections.')
                proposed={**self.files,path:base64.b64encode(args['content'].encode()).decode()};validate_files(proposed)
                self.files=proposed;return {'saved':path}
            if path not in self.files:raise ValueError('File does not exist in this task.')
            raw=base64.b64decode(self.files[path])
            if name=='export_file':
                if self.exported_content.get(path)!=self.files[path]:
                    self.exports[path]=artifact(self.context,self.owner,path,raw)
                    self.exported_content[path]=self.files[path]
                    if self.run_id:
                        token=self.exports[path].split('file_',1)[1]
                        self.store.add_artifact(self.owner,self.run_id,path,token)
                return {'download':self.exports[path]}
            start=max(0,args.get('offset',0));text=raw.decode('utf-8')
            return {'content':text[start:start+14000],'total_characters':len(text),'next_offset':start+14000 if start+14000<len(text) else None}
        if name=='browse_url':
            from public_web import web_url
            web_url(args['url']);path=safe_path(args['path'])
            code='''import json, os, datetime
from pathlib import Path
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    browser=p.chromium.launch(executable_path='/usr/bin/chromium',headless=True,timeout=10000,
        args=['--no-sandbox','--disable-dev-shm-usage','--disable-gpu','--renderer-process-limit=2','--proxy-bypass-list=<-loopback>'],
        proxy={'server':os.environ['HTTPS_PROXY']})
    page=browser.new_page()
    response=page.goto(URL,wait_until='domcontentloaded',timeout=15000)
    page.wait_for_timeout(1200)
    html=page.content()
    if len(html.encode())>2*1024*1024:raise ValueError('Rendered HTML exceeds 2 MB')
    output=Path(OUTPUT);output.parent.mkdir(parents=True,exist_ok=True);output.write_text(html)
    print(json.dumps({'url':page.url,'status':response.status if response else None,
        'title':page.title(),'text':page.locator('body').inner_text(timeout=2000)[:8000],
        'links':page.locator('a[href]').evaluate_all('(nodes)=>nodes.slice(0,40).map(n=>n.href)'),
        'fetched_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'note':'Rendered live page; challenge/403 responses are not successful task evidence.'}))
    browser.close()
'''
            code='URL='+repr(args['url'])+'\nOUTPUT='+repr(path)+'\n'+code
            result=await self.context.application.bot_data['sandbox'].execute(code,self.files,'python',network=True)
            self.files=result.pop('files')
            if result['exit_code']!=0:return {**result,'error':'Browser rendering failed. Inspect stderr or use fetch_url; do not claim success.'}
            try:receipt=json.loads(result['stdout'])
            except ValueError:raise ValueError('Browser result was unreadable. Use fetch_url or inspect the saved HTML.') from None
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
            result=await self.context.application.bot_data['sandbox'].execute(args.get('code',args.get('command')),self.files,name,network=network)
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
