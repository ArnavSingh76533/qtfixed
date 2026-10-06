"""Import a SKILL.md/agent instruction file or a bounded, traversal-safe ZIP."""
import base64
from io import BytesIO
from pathlib import PurePosixPath
import re
import stat
import zipfile
import yaml

MAX_BUNDLE=2*1024*1024

def safe_path(name):
    p=PurePosixPath(name)
    if not name or '\\' in name or any(ord(c)<32 for c in name) or p.is_absolute() or '..' in p.parts or ':' in name or len(name)>180:
        raise ValueError('Unsafe file path in bundle.')
    if any(part.startswith('.') for part in p.parts):raise ValueError('Hidden files are not accepted in bundles.')
    return str(p)

def parse_bundle(raw,filename,kind):
    if len(raw)>MAX_BUNDLE:raise ValueError('Skill/agent bundles must be under 2 MB.')
    files={}
    if zipfile.is_zipfile(BytesIO(raw)):
        with zipfile.ZipFile(BytesIO(raw)) as archive:
            members=archive.infolist()
            if len(members)>100:raise ValueError('Maximum 100 archive entries.')
            total=0
            for item in members:
                name=safe_path(item.filename.rstrip('/'))
                mode=item.external_attr>>16
                if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0,stat.S_IFREG,stat.S_IFDIR)):
                    raise ValueError('Links and special files are not accepted.')
                if item.is_dir():continue
                total+=item.file_size
                if total>MAX_BUNDLE or (item.file_size>512*1024 and PurePosixPath(name).name.lower() not in ('skill.md','agent.md','agents.md')):raise ValueError('Unpacked bundle exceeds size limits.')
                if name in files:raise ValueError('Duplicate archive filename.')
                files[name]=archive.read(item)
        expected='skill.md' if kind=='skill' else 'agent.md'
        candidates=[n for n in files if PurePosixPath(n).name.lower() in ({expected} if kind=='skill' else {'agent.md','agents.md'})]
        if len(candidates)!=1:raise ValueError('ZIP must contain exactly one '+expected+' entry point.')
        entry=candidates[0];root=PurePosixPath(entry).parent
        if str(root)!='.':
            # Only files inside the entry point's folder are installed.
            files={str(PurePosixPath(n).relative_to(root)):v for n,v in files.items() if PurePosixPath(n).is_relative_to(root)}
        entry=PurePosixPath(entry).name
    else:
        entry='SKILL.md' if kind=='skill' else 'AGENT.md';files={entry:raw}
    try:text=files[entry].decode('utf-8-sig')
    except UnicodeDecodeError:raise ValueError('Instructions must be UTF-8 text.') from None
    if '\x00' in text or not text.strip():raise ValueError('Instructions must be non-empty UTF-8 text.')
    metadata={};body=text
    if text.startswith('---\n') or text.startswith('---\r\n'):
        parts=re.split(r'^---\s*$',text,maxsplit=2,flags=re.M)
        if len(parts)!=3:raise ValueError('Invalid YAML frontmatter.')
        if len(parts[1])>8000:raise ValueError('Frontmatter is too large.')
        try:metadata=yaml.safe_load(parts[1]) or {}
        except yaml.YAMLError:raise ValueError('Invalid YAML frontmatter.') from None
        body=parts[2].strip()
        if not isinstance(metadata,dict):raise ValueError('Frontmatter must be a mapping.')
    if kind=='skill' and not all(isinstance(metadata.get(k),str) and metadata[k].strip() for k in ('name','description')):
        raise ValueError('SKILL.md needs YAML frontmatter with name and description. Use /skills example.')
    fallback=re.sub('[^a-z0-9-]+','-',PurePosixPath(filename).stem.lower()).strip('-')[:64] or 'my-agent'
    name=metadata.get('name',fallback);description=metadata.get('description','User-provided specialist instructions.')
    if not isinstance(name,str) or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*',name) or len(name)>64:raise ValueError('Use a lowercase, hyphenated name up to 64 characters.')
    if not isinstance(description,str) or not 1<=len(description)<=1024 or not body:raise ValueError('Provide a description and non-empty instructions.')
    return {'name':name,'description':description,'instructions':body,
            'files':{safe_path(n):base64.b64encode(v).decode() for n,v in files.items()}}
