"""Container-only runner. This file must never execute in the bot host process."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import time
sys.path.insert(0,"/")
from sandbox_dependencies import requirements

def run():
    root=Path('/workspace')
    if Path.cwd()!=root or os.getuid()!=65534:raise SystemExit('Container-only runner')
    data=json.load(sys.stdin)
    timeout=data.get('timeout',600)
    if type(timeout)!=int or not 1<=timeout<=600:raise ValueError('Invalid execution timeout')
    packages=requirements(data.get('packages',[]))
    for name,encoded in data['files'].items():
        path=root/name
        if not path.resolve().is_relative_to(root):raise ValueError('Invalid path')
        path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(base64.b64decode(encoded))
    program=root/'_task.py' if data['mode']=='python' else root/'_task.sh'
    bootstrap='import sys; sys.path.insert(0, "/workspace/.packages")\n' if data['mode']=='python' else ''
    program.write_text(bootstrap+data['code'])
    # Output goes to bounded tmpfs files, not unbounded host process pipes.
    with open('/tmp/stdout','w+') as out,open('/tmp/stderr','w+') as err:
        started=time.monotonic()
        installed=False
        try:
            if packages:
                install=subprocess.run(['python','-m','pip','install','--target=/workspace/.packages','--only-binary=:all:','--no-cache-dir','--no-compile','--disable-pip-version-check',*packages],stdout=out,stderr=err,timeout=min(180,timeout))
                if install.returncode:raise RuntimeError('Dependency installation failed; inspect stderr. No task code ran.')
                installed=True
                out.seek(0);out.truncate(0);err.seek(0);err.truncate(0)  # Keep task output separate from pip progress.
            remaining=max(.1,timeout-(time.monotonic()-started))
            process=subprocess.run(['python','-I',str(program)] if data['mode']=='python' else ['/bin/sh',str(program)],stdout=out,stderr=err,timeout=remaining)
            exit_code=process.returncode
        except RuntimeError as error:
            exit_code=1;err.write('\n'+str(error)+'\n')
        except subprocess.TimeoutExpired:
            exit_code=124
            err.write(f'\nExecution exceeded {timeout} seconds; the code process was stopped.\n')
        out.seek(0);err.seek(0)
        result={'packages':packages if installed else [],'exit_code':exit_code,'stdout':out.read(12000),'stderr':err.read(4000),'files':{}}
    total=0
    for path in root.rglob('*'):
        if path.is_symlink() or not path.is_file() or path==program:continue
        if not path.resolve().is_relative_to(root):continue
        if any(p.startswith('.') for p in path.relative_to(root).parts):continue
        size=path.stat().st_size;total+=size
        if total>64_000_000 or size>=50_000_000 or len(result['files'])>=60:raise ValueError('Too many output files')
        result['files'][str(path.relative_to(root))]=base64.b64encode(path.read_bytes()).decode()
    print(json.dumps(result))

if __name__=='__main__':run()
