"""Untrusted code executes only inside a fresh constrained container, never on host."""
import asyncio
import base64
import json
import re
import shutil
import uuid
from pathlib import Path
import config
from skill_bundles import safe_path

MAX_FILES=60
MAX_BYTES=8*1024*1024

def validate_files(files):
    if len(files)>MAX_FILES:raise ValueError('Workspace has too many files.')
    total=0
    for name,encoded in files.items():
        safe_path(name)
        try:raw=base64.b64decode(encoded,validate=True)
        except Exception:raise ValueError('Invalid workspace encoding.') from None
        total+=len(raw)
        if len(raw)>MAX_BYTES or total>MAX_BYTES:raise ValueError('Workspace exceeds 8 MB.')

class Sandbox:
    def __init__(self):
        self.slots=asyncio.Semaphore(config.SANDBOX_CONCURRENCY)
        self.verified=False
    async def check_isolation(self):
        if self.verified:return
        process=await asyncio.create_subprocess_exec('docker','info','--format','{{json .}}',stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
        try:out,_=await asyncio.wait_for(process.communicate(),10)
        except asyncio.TimeoutError:
            process.kill();await process.wait();raise ValueError('Docker health check timed out.') from None
        if process.returncode:raise ValueError('Docker is unavailable to the bot user. Configure rootless Docker first.')
        info=json.loads(out)
        if not any('rootless' in str(s) for s in info.get('SecurityOptions',[])) and config.SANDBOX_RUNTIME!='runsc':
            raise ValueError('Untrusted code requires rootless Docker or the runsc sandbox runtime. No code ran.')
        if not all(info.get(key) for key in ('MemoryLimit','PidsLimit','CpuCfsQuota')):
            raise ValueError('Docker resource limits are unavailable. Enable memory, PID and CPU cgroup limits before running untrusted code.')
        self.verified=True
    def command(self,name,network='none',proxy=None):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_./:@-]*',config.SANDBOX_IMAGE):raise ValueError('Invalid sandbox image setting.')
        command=['docker','run','--rm','--pull=never','--name',name,'--network='+network,
            '--read-only','--cap-drop=ALL','--security-opt=no-new-privileges',
            '--memory='+('768m' if proxy else '256m'),'--memory-swap='+('768m' if proxy else '256m'),'--cpus=1','--pids-limit='+('256' if proxy else '64'),
            '--ulimit=nofile='+('1024:1024' if proxy else '128:128'),'--ulimit=fsize=8388608:8388608',
            '--user=65534:65534','--log-driver=none',
            '--tmpfs=/tmp:rw,nosuid,nodev,size='+('128m' if proxy else '16m'),
            '--tmpfs=/workspace:rw,nosuid,nodev,size=32m,uid=65534,gid=65534,mode=700',
            '--workdir=/workspace','--env=HOME=/workspace','--env=PYTHONDONTWRITEBYTECODE=1','-i']
        if config.SANDBOX_RUNTIME:
            if not re.fullmatch(r'[A-Za-z0-9_-]+',config.SANDBOX_RUNTIME):raise ValueError('Invalid sandbox runtime.')
            command+=['--runtime',config.SANDBOX_RUNTIME]
        if proxy:
            for key in ('http_proxy','https_proxy','HTTP_PROXY','HTTPS_PROXY'):
                command+=['--env='+key+'='+proxy]
            command+=['--env=NO_PROXY=','--env=no_proxy=']
        return command+[config.SANDBOX_IMAGE,'python','-I','/runner.py']
    async def control(self,*arguments):
        process=await asyncio.create_subprocess_exec('docker',*arguments,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        try:
            out,err=await asyncio.wait_for(process.communicate(),15)
            if process.returncode:raise ValueError('Sandbox web setup failed. Rebuild the sandbox image and check Docker networking.')
            return out.decode().strip()
        finally:
            if process.returncode is None:process.kill();await process.wait()

    async def prepare_web(self,network,proxy_name):
        # No bridge gateway address on the isolated worker network, no published ports.
        await self.control('network','create','--internal','--opt','com.docker.network.bridge.inhibit_ipv4=true',network)
        command=self.command(proxy_name,network='bridge')
        command.remove('-i')
        command.insert(2,'-d')
        command[-1]='/web_proxy.py'
        # Only the trusted proxy sidecar can reach the internet; it resolves/pins public IPs.
        await self.control(*command[1:])
        await self.control('network','connect','--alias','qtfixed-proxy',network,proxy_name)
        readiness="import socket,time\nfor attempt in range(30):\n try:\n  s=socket.create_connection(('127.0.0.1',8080),.5);s.close();break\n except OSError:time.sleep(.1)\nelse:raise SystemExit(1)"
        await self.control('exec',proxy_name,'python','-I','-c',readiness)

    async def execute(self,code,files,mode='python',network=False):
        if not config.SANDBOX_ENABLED:raise ValueError('Code execution is disabled. The bot owner must install the documented container sandbox and enable SANDBOX_ENABLED. No code ran on the host.')
        if not shutil.which('docker'):raise ValueError('Docker sandbox is unavailable. No code ran.')
        if mode not in ('python','shell') or not isinstance(code,str) or len(code)>30000:raise ValueError('Invalid code or command (maximum 30,000 characters).')
        if network and not config.SANDBOX_WEB_ENABLED:raise ValueError('Sandbox public web testing is disabled by the owner. Use fetch_url and test saved HTML offline instead.')
        validate_files(files)
        async with self.slots:
            await self.check_isolation()
            name='qtfixed-'+uuid.uuid4().hex
            network_name=name+'-net';proxy_name=name+'-proxy'
            process=None
            try:
                if network:await self.prepare_web(network_name,proxy_name)
                command=self.command(name,network_name if network else 'none','http://qtfixed-proxy:8080' if network else None)
                process=await asyncio.create_subprocess_exec(*command,stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
                async def bounded(stream,limit):
                    chunks=[];size=0
                    while True:
                        chunk=await stream.read(65536)
                        if not chunk:return b''.join(chunks)
                        size+=len(chunk)
                        if size>limit:raise ValueError('Sandbox output limit exceeded.')
                        chunks.append(chunk)
                async def interact():
                    process.stdin.write(json.dumps({'code':code,'mode':mode,'files':files,'timeout':config.SANDBOX_TIMEOUT}).encode())
                    await process.stdin.drain();process.stdin.close()
                    out,err=await asyncio.gather(bounded(process.stdout,12*1024*1024),bounded(process.stderr,32000))
                    await process.wait();return out,err
                out,err=await asyncio.wait_for(interact(),config.SANDBOX_TIMEOUT+5)
                if process.returncode:raise ValueError('Sandbox runner failed (exit '+str(process.returncode)+'): '+err.decode(errors='replace')[-1200:])
                try:result=json.loads(out)
                except (ValueError,UnicodeDecodeError):raise ValueError('Sandbox returned invalid output.') from None
                if not isinstance(result,dict) or not isinstance(result.get('files'),dict):raise ValueError('Invalid sandbox result.')
                validate_files(result['files'])
                # Do not trust arbitrary fields from the untrusted container.
                return {'stdout':str(result.get('stdout',''))[:12000], 'stderr':str(result.get('stderr',''))[:4000],
                        'exit_code':int(result.get('exit_code',1)),'files':result['files']}
            except asyncio.TimeoutError:raise ValueError('Sandbox time limit reached. The container was stopped.') from None
            finally:
                # Exact random container name; no user-controlled host shell command.
                try:
                    cleanup=await asyncio.create_subprocess_exec('docker','rm','-f',name,stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
                    await asyncio.wait_for(cleanup.wait(),5)
                except (OSError,asyncio.TimeoutError):pass
                if process and process.returncode is None:
                    process.kill();await process.wait()
                if network:
                    for arguments in (('rm','-f',proxy_name),('network','rm',network_name)):
                        try:await self.control(*arguments)
                        except (OSError,ValueError,asyncio.TimeoutError):pass
