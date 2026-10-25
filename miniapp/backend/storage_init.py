"""One-shot ownership preparation of Mini App volumes only; never mount bot data here."""
import os
from pathlib import Path
import stat


def prepare(roots, uid, gid):
    if any(type(value) is not int or value <= 0 for value in (uid, gid)):
        raise ValueError('Non-root storage UID/GID required')
    def visit(fd):
        info = os.fstat(fd)
        if stat.S_ISDIR(info.st_mode):
            os.fchown(fd, uid, gid); os.fchmod(fd, 0o700)
            for name in os.listdir(fd):
                child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                try: visit(child)
                finally: os.close(child)
        elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            os.fchown(fd, uid, gid); os.fchmod(fd, 0o600)
        else:
            raise ValueError('Links and special files are not permitted in Mini App storage')
    for root in roots:
        Path(root).mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try: visit(fd)
        finally: os.close(fd)


def main():
    try:
        prepare(('/miniapp-data', '/miniapp-backups', '/data', '/config'),
                int(os.environ.get('TARGET_UID', '10001')), int(os.environ.get('TARGET_GID', '10001')))
    except (OSError, ValueError):
        raise SystemExit('Mini App storage preparation failed') from None
    print('Mini App storage ownership prepared; catalog not mounted or changed.')


if __name__ == '__main__': main()
