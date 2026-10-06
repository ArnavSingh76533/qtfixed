"""Bounded temporary PyPI requirements. URLs, paths and pip flags are not accepted."""
import re
ALLOWED={'numpy','matplotlib','pandas','scipy','sympy','seaborn','plotly','pillow','requests','beautifulsoup4','lxml','openpyxl','pypdf','python-docx','imageio','imageio-ffmpeg','yt-dlp'}
def requirements(value):
    if isinstance(value,str):value=value.split()
    if not isinstance(value,list) or len(value)>8:raise ValueError('Use at most eight supported PyPI requirements.')
    result=[]
    for item in value:
        if not isinstance(item,str):raise ValueError('Invalid Python requirement.')
        match=re.fullmatch(r'([A-Za-z][A-Za-z0-9_-]*)(?:==([0-9][A-Za-z0-9.]*))?',item)
        if not match or match[1].lower().replace('_','-') not in ALLOWED:raise ValueError('Unsupported package. Allowed: '+', '.join(sorted(ALLOWED)))
        if item not in result:result.append(item)
    return result
