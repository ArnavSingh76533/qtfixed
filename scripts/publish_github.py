"""Create a PRIVATE qtfixed repo using your authenticated GitHub CLI.
Copies only an explicit source allowlist into a fresh temporary git repo.
No records, ZIPs, credentials, databases, or previous git history are uploaded.
"""
import pathlib
import shutil
import subprocess
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]


def run(*args, cwd=None, capture=False):
    return subprocess.run(args, cwd=cwd, check=True, text=True,
                          stdout=subprocess.PIPE if capture else None)


def main():
    if not shutil.which('gh') or not shutil.which('git'):
        raise SystemExit('Install Git and GitHub CLI, run gh auth login, then retry.')
    run('gh','auth','status')
    login=run('gh','api','user','--jq','.login',capture=True).stdout.strip()
    with tempfile.TemporaryDirectory(prefix='qtfixed-source-') as tmp:
        target=pathlib.Path(tmp)
        paths=list(ROOT.glob('*.py')) + [ROOT/'requirements.txt',ROOT/'README.md',ROOT/'.gitignore',ROOT/'.env.example']
        paths += list((ROOT/'tests').glob('*.py')) + list((ROOT/'scripts').glob('*.py'))
        paths += list((ROOT/'.github/workflows').glob('*.yml'))
        for path in paths:
            destination=target/path.relative_to(ROOT)
            destination.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(path,destination)
        run('git','init','-b','main',cwd=target)
        run('git','add','.',cwd=target)
        run('git','-c',f'user.name={login}','-c',f'user.email={login}@users.noreply.github.com',
            'commit','-m','Add Question Ai Groq bot with streaming and persistent campaigns',cwd=target)
        # Fails safely if that repository already exists; never force-pushes.
        run('gh','repo','create',f'{login}/qtfixed','--private','--source','.',
            '--remote','origin','--push','--description','Question Ai Telegram bot: Groq streaming, saved chats, broadcast campaigns',cwd=target)


if __name__=='__main__':
    main()
