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
    from agent_jobs import browser_job,video_job
    setup="""import requests
from pathlib import Path
url='https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.min.js'
r=requests.get(url,timeout=12);r.raise_for_status();Path('three.js').write_bytes(r.content)
html=\"\"\"<!doctype html><meta charset='utf-8'><title>Calculator test</title>
<script src='three.js'></script><canvas id='scene'></canvas>
<input id='a' value='4'><input id='b' value='5'><button id='sum'>Add</button><output id='result'></output>
<script>const renderer=new THREE.WebGLRenderer({canvas:document.querySelector('#scene')});renderer.setSize(500,300);
const scene=new THREE.Scene();scene.background=new THREE.Color('#19344e');
const camera=new THREE.PerspectiveCamera(60,5/3,.1,100);camera.position.z=3;
const cube=new THREE.Mesh(new THREE.BoxGeometry(),new THREE.MeshNormalMaterial());scene.add(cube);
function animate(){requestAnimationFrame(animate);cube.rotation.y+=.01;renderer.render(scene,camera)}animate();
document.querySelector('#sum').onclick=()=>document.querySelector('#result').textContent=String(Number(document.querySelector('#a').value)+Number(document.querySelector('#b').value));
</script>\"\"\"
Path('calculator.html').write_text(html)
"""
    result=await sandbox.execute(setup,{},network=True)
    assert result['exit_code']==0,result['stderr']
    files=result['files']
    actions=[{'action':'fill','selector':'#a','value':'7'},{'action':'fill','selector':'#b','value':'8'},
        {'action':'click','selector':'#sum'},{'action':'expect_text','selector':'#result','value':'15'}]
    result=await sandbox.execute(browser_job('file:///workspace/calculator.html','rendered.html',screenshot='calculator.png',actions=actions),files)
    assert result['exit_code']==0,result['stderr']
    receipt=json.loads(result['stdout'])
    assert not receipt['page_errors'],receipt
    assert len(receipt['checks'])==4,receipt
    assert base64.b64decode(result['files']['calculator.png']).startswith(b'\x89PNG')
    print('Real Three.js software WebGL calculator interactions and screenshot passed.')
    files=result['files']
    result=await sandbox.execute(browser_job('file:///workspace/calculator.html','calculator.pdf',pdf=True),files)
    assert result['exit_code']==0,result['stderr']
    assert json.loads(result['stdout'])['pdf_pages']>=1
    assert base64.b64decode(result['files']['calculator.pdf']).startswith(b'%PDF-')
    print('Actual HTML-to-PDF rendering and PDF inspection passed.')
    result=await sandbox.execute("import subprocess,yt_dlp,yt_dlp_ejs;print(subprocess.check_output(['node','--version']).decode().strip());print(yt_dlp.version.__version__)",{})
    assert result['exit_code']==0,result['stderr']
    assert 'v22.' in result['stdout'],result
    result=await sandbox.execute(video_job('https://interactive-examples.mdn.mozilla.net/media/cc0-videos/flower.mp4','download.mp4'),{},network=True)
    assert result['exit_code']==0,result['stderr']
    assert base64.b64decode(result['files']['download.mp4'])[4:8]==b'ftyp'
    print('Bundled Node/EJS and real public yt-dlp video download passed.')
    result=await sandbox.execute(WEB_CODE,{},network=True)
    assert result['exit_code']==0,result['stderr']
    print('Web container smoke passed: '+result['stdout'].strip())
    result=await sandbox.execute(BROWSER_CODE,{},network=True)
    assert result['exit_code']==0,result['stderr']
    print('Browser container smoke passed: '+result['stdout'].strip())

if __name__=='__main__':asyncio.run(main())
