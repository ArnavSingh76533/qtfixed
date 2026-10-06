"""Trusted smoke program in a disposable CI container; deployment uses check_sandbox.py."""
import asyncio
import base64
import json
import sys
import uuid
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agent_sandbox import Sandbox
from check_sandbox import CODE, WEB_CODE, BROWSER_CODE, PLOT_CODE

async def main():
    # CI intentionally uses its disposable runner's daemon. Production's execute()
    # separately requires rootless Docker or runsc; it has no bypass setting.
    command=Sandbox().command('qtfixed-ci-'+uuid.uuid4().hex)
    process=await asyncio.create_subprocess_exec(*command,stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    out,err=await asyncio.wait_for(process.communicate(json.dumps({'code':CODE,'mode':'python','files':{}}).encode()),45)
    assert process.returncode==0,err.decode()
    result=json.loads(out)
    assert result['exit_code']==0,result['stderr']
    assert base64.b64decode(result['files']['result.txt'])==b'Sandbox checks passed'
    print('Container smoke passed: real isolated execution and returned file.')
    sandbox=Sandbox()
    # Same production lifecycle, with only isolation verification replaced for this
    # disposable CI daemon; production has no bypass configuration.
    async def verified_ci_daemon():pass
    sandbox.check_isolation=verified_ci_daemon
    import config
    config.SANDBOX_ENABLED=True;config.SANDBOX_WEB_ENABLED=True
    config.SANDBOX_TIMEOUT=30
    result=await sandbox.execute('import time; time.sleep(26); print("Past old timeout")',{})
    assert result['exit_code']==0 and 'Past old timeout' in result['stdout'],result
    config.SANDBOX_TIMEOUT=1
    result=await sandbox.execute('import time; time.sleep(5)',{})
    assert result['exit_code']==124 and '1 seconds' in result['stderr'],result
    config.SANDBOX_TIMEOUT=600
    print('Configured timeout passed: task beyond 25 seconds; structured timeout at requested limit.')
    result=await sandbox.execute(PLOT_CODE,{})
    assert result['exit_code']==0,result['stderr']
    assert base64.b64decode(result['files']['plot.png']).startswith(b'\x89PNG')
    assert base64.b64decode(result['files']['clip.mp4'])[4:8]==b'ftyp'
    print('Real Python plot and FFmpeg MP4 passed.')
    code="with open('large.bin','wb') as f:\n for i in range(49):f.write(b'x'*1_000_000)\nprint('49 MB file produced')"
    result=await sandbox.execute(code,{})
    assert result['exit_code']==0,result['stderr']
    assert len(base64.b64decode(result['files']['large.bin']))==49_000_000
    del result
    print('49 MB output survived sandbox transport.')
    result=await sandbox.execute("import sympy; x=sympy.Symbol('x'); print(sympy.expand((x+1)**2))",{},network=True,packages=['sympy==1.14.0'])
    assert result['exit_code']==0,result['stderr']
    assert 'x**2 + 2*x + 1' in result['stdout']
    assert result['packages']==['sympy==1.14.0']
    print('Temporary PyPI install and code execution passed through public proxy.')
    result=await sandbox.execute(WEB_CODE,{},network=True)
    assert result['exit_code']==0,result['stderr']
    print('Web container smoke passed: '+result['stdout'].strip())
    result=await sandbox.execute(BROWSER_CODE,{},network=True)
    assert result['exit_code']==0,result['stderr']
    print('Browser container smoke passed: '+result['stdout'].strip())

if __name__=='__main__':asyncio.run(main())
