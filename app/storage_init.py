"""One-shot ownership preparation for existing persistent bot storage.

This container receives no token and no network. Only its /data and /backups
mounts are writable; the long-running bot runs as an unprivileged user.
"""
import os
from pathlib import Path
import stat


def prepare_storage(roots, uid=10001, gid=10001):
    if any(type(value) is not int or not 1 <= value <= 2147483647 for value in (uid,gid)):
        raise ValueError('Storage UID/GID must be positive non-root IDs')
    def visit(descriptor):
        info=os.fstat(descriptor)
        if stat.S_ISDIR(info.st_mode):
            os.fchown(descriptor,uid,gid);os.fchmod(descriptor,0o700)
            for name in os.listdir(descriptor):
                child=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=descriptor)
                try:visit(child)
                finally:os.close(child)
        elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            os.fchown(descriptor,uid,gid);os.fchmod(descriptor,0o600)
        else:
            raise ValueError('Storage contains a link or non-regular file')
    for root in roots:
        root=Path(root);root.mkdir(parents=True,exist_ok=True)
        descriptor=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:visit(descriptor)
        finally:os.close(descriptor)


def main():
    try:
        uid=int(os.environ.get('TARGET_UID','10001'));gid=int(os.environ.get('TARGET_GID','10001'))
        prepare_storage(('/data','/backups'),uid,gid)
    except (OSError,ValueError) as exc:
        print('Storage preparation failed: '+type(exc).__name__)
        raise SystemExit(1) from None
    print('Storage ownership prepared; database contents unchanged.')


if __name__=='__main__':main()
