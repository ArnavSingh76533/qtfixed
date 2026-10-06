"""Trusted smoke program in a disposable CI container; deployment uses check_sandbox.py."""
import asyncio
import base64
import json
import sys
import uuid
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agent_sandbox import Sandbox
from check_sandbox import CODE

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

if __name__=='__main__':asyncio.run(main())
