"""Atomic JSON persistence. Corrupt data fails closed instead of being erased."""
import json
import os
import tempfile
from pathlib import Path
from filelock import FileLock


def read_json(filename, expected_type, recover=False):
    path = Path(filename)
    if not path.exists():
        return expected_type()
    candidates = [path]
    if recover:
        candidates += sorted(path.parent.glob(path.name + '.*.backup'), reverse=True)
    for candidate in candidates:
        try:
            with candidate.open(encoding='utf-8') as stream:
                data = json.load(stream)
            if isinstance(data, expected_type):
                return data
        except (ValueError, OSError):
            continue
    raise ValueError(f"No valid {expected_type.__name__} data in {path.name}; restore a backup")


def write_json(filename, data):
    path = Path(filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(path) + '.lock', timeout=30):
        temp = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                    dir=path.parent, prefix=path.name + '.', suffix='.tmp', delete=False) as stream:
                temp = stream.name
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, path)
        finally:
            if temp and os.path.exists(temp):
                os.unlink(temp)
