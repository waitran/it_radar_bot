"""Reject tracked state/secrets; diagnostics contain paths, never matched values."""
import json
from pathlib import Path
import re
import subprocess
import sys

PATTERNS = (
    re.compile(rb'\b[0-9]{5,}:[A-Za-z0-9_-]{20,}'),
    re.compile(rb'\bgh[pousr]_[A-Za-z0-9]{30,}'),
    re.compile(rb'\bgithub_pat_[A-Za-z0-9_]{30,}'),
    re.compile(rb'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'),
    re.compile(rb'\bsk-(?:proj-)?[A-Za-z0-9_-]{35,}'),
    re.compile(rb'-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----'),
)


def violations(root, paths):
    problems=[]
    for relative in paths:
        path=Path(root)/relative;name=path.name
        reason=None
        if path.is_symlink():reason='tracked symlink'
        elif name.lower() == 'credentials.json' or name.lower().startswith('service-account') or any(
                name.lower().endswith(suffix) for suffix in ('.pem', '.key', '.p12', '.pfx', '.keystore', '.session', '.session-journal')):
            reason='tracked private credential file'
        elif (name.lower().startswith('.env') and name!='.env.example') or any(
                name.lower().endswith(suffix) for suffix in ('.db','.db-wal','.db-shm','.db-journal','.db.lock','.sqlite','.sqlite3','.sqlite3-wal','.sqlite3-shm','.bak','.part')):
            reason='tracked credentials or application state'
        elif path.is_file():
            with path.open('rb') as stream:
                tail=b''
                while chunk:=stream.read(65536):
                    content=tail+chunk
                    if any(pattern.search(content) for pattern in PATTERNS):
                        reason='potential credential';break
                    tail=content[-256:]
        if reason:problems.append(dict(path=str(relative),reason=reason))
    return problems


def main():
    paths=subprocess.check_output(['git','ls-files','-z']).decode().split('\0')
    paths=[path for path in paths if path]
    problems=violations(Path.cwd(),paths)
    if problems:
        print(json.dumps(problems,ensure_ascii=True,indent=2))
        raise SystemExit(1)
    print(f'Repository checks passed ({len(paths)} tracked files).')


if __name__=='__main__':main()

