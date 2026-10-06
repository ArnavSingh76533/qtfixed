"""Deployment smoke test. Run with the same user/env as the bot; no AI credentials used."""
import asyncio
import base64
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agent_sandbox import Sandbox

CODE='''import os, socket
from pathlib import Path
assert os.getuid() == 65534
assert 'BOT_TOKEN' not in os.environ and 'GROQ_API_KEY' not in os.environ
assert not Path('/workspace/.env').exists()
try:
    Path('/host-write-test').write_text('bad')
except OSError:
    pass
else:
    raise AssertionError('root filesystem was writable')
try:
    socket.create_connection(('1.1.1.1',443),timeout=1)
except OSError:
    pass
else:
    raise AssertionError('network was reachable')
Path('result.txt').write_text('Sandbox checks passed')
print('isolated execution complete')
'''

async def main():
    result=await Sandbox().execute(CODE,{})
    assert result['exit_code']==0,result['stderr']
    assert base64.b64decode(result['files']['result.txt'])==b'Sandbox checks passed'
    print('PASS: non-root, read-only root, no network/credentials, file output works.')

if __name__=='__main__':asyncio.run(main())
