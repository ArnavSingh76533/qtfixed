"""Deployment smoke test. Run with the same user/env as the bot; no AI credentials used."""
import asyncio
import base64
import sys
import argparse
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

WEB_CODE='''import os, socket, requests
assert os.getuid() == 65534
assert 'BOT_TOKEN' not in os.environ and 'GROQ_API_KEY' not in os.environ
assert os.environ.get('HTTPS_PROXY') == 'http://qtfixed-proxy:8080'
blocked = requests.get('http://169.254.169.254/', timeout=4)
assert blocked.status_code == 403, 'metadata was not blocked'
try:
    requests.get('https://127.0.0.1/', timeout=4)
except requests.exceptions.ProxyError:
    pass
else:
    raise AssertionError('loopback was not blocked')
try:
    socket.create_connection(('1.1.1.1',443),timeout=1)
except OSError:
    pass
else:
    raise AssertionError('direct egress bypassed the proxy')
response = requests.get('https://example.com/', timeout=12)
assert response.status_code == 200 and 'Example Domain' in response.text
print('public HTTPS works; metadata, loopback and direct egress blocked')
'''

BROWSER_CODE='''import os
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    browser=p.chromium.launch(executable_path='/usr/bin/chromium',headless=True,
        args=['--no-sandbox','--disable-dev-shm-usage','--proxy-bypass-list=<-loopback>'],
        proxy={'server':os.environ['HTTPS_PROXY']})
    page=browser.new_page()
    response=page.goto('https://example.com/',wait_until='domcontentloaded',timeout=15000)
    assert response.status==200 and 'Example Domain' in page.locator('body').inner_text()
    response=page.goto('http://169.254.169.254/',wait_until='domcontentloaded',timeout=5000)
    assert response.status==403
    browser.close()
print('headless Chromium public HTTPS works; metadata blocked')
'''

async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--web',action='store_true')
    args=parser.parse_args()
    sandbox=Sandbox()
    result=await sandbox.execute(CODE,{})
    assert result['exit_code']==0,result['stderr']
    assert base64.b64decode(result['files']['result.txt'])==b'Sandbox checks passed'
    print('PASS: non-root, read-only root, no network/credentials, file output works.')
    if args.web:
        result=await sandbox.execute(WEB_CODE,{},network=True)
        assert result['exit_code']==0,result['stderr']
        print('PASS: '+result['stdout'].strip())
        result=await sandbox.execute(BROWSER_CODE,{},network=True)
        assert result['exit_code']==0,result['stderr']
        print('PASS: '+result['stdout'].strip())

if __name__=='__main__':asyncio.run(main())
