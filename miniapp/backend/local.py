"""Explicit local-only launcher: temporary DB snapshot, loopback, normal HMAC auth."""
import argparse
from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile
import uvicorn
from .config import Settings
from .main import create_app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-db', required=True, type=Path)
    args = parser.parse_args()
    settings = Settings.from_env()
    with tempfile.TemporaryDirectory(prefix='miniapp-local-') as directory:
        snapshot = Path(directory)/'catalog.db'
        source = sqlite3.connect(args.source_db.absolute().as_uri()+'?mode=ro', uri=True, timeout=2)
        target = sqlite3.connect(snapshot)
        try:
            source.backup(target)
        finally:
            source.close()
            target.close()
        uvicorn.run(create_app(replace(settings, db_path=str(snapshot))),
                    host='127.0.0.1', port=8765, access_log=False)


if __name__ == '__main__':
    main()
