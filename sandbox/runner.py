"""Container-only runner. This file must never execute in the bot host process."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys

def run():
    root=Path('/workspace')
    if Path.cwd()!=root or os.getuid()!=65534:raise SystemExit('Container-only runner')
    data=json.load(sys.stdin)
    for name,encoded in data['files'].items():
        path=root/name
        if not path.resolve().is_relative_to(root):raise ValueError('Invalid path')
        path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(base64.b64decode(encoded))
    program=root/'_task.py' if data['mode']=='python' else root/'_task.sh'
    program.write_text(data['code'])
    # Output goes to bounded tmpfs files, not unbounded host process pipes.
    with open('/tmp/stdout','w+') as out,open('/tmp/stderr','w+') as err:
        process=subprocess.run(['python','-I',str(program)] if data['mode']=='python' else ['/bin/sh',str(program)],stdout=out,stderr=err,timeout=25)
        out.seek(0);err.seek(0)
        result={'exit_code':process.returncode,'stdout':out.read(12000),'stderr':err.read(4000),'files':{}}
    total=0
    for path in root.rglob('*'):
        if path.is_symlink() or not path.is_file() or path==program:continue
        if not path.resolve().is_relative_to(root):continue
        if any(p.startswith('.') for p in path.relative_to(root).parts):continue
        size=path.stat().st_size;total+=size
        if total>8*1024*1024 or len(result['files'])>=60:raise ValueError('Too many output files')
        result['files'][str(path.relative_to(root))]=base64.b64encode(path.read_bytes()).decode()
    print(json.dumps(result))

if __name__=='__main__':run()
