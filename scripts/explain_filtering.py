"""Explain one publication offline: python -m scripts.explain_filtering < post.txt."""
import argparse
from datetime import datetime
import json
import sys

from app.config import Config
from app.filters import Filters
from app.models import Item


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='config.yaml')
    parser.add_argument('--now', help='ISO timestamp with timezone; defaults to current UTC')
    parser.add_argument('--published-at', help='Original publication ISO timestamp')
    parser.add_argument('--kind', choices=['publication', 'vacancy'], default='publication')
    args = parser.parse_args()
    text = sys.stdin.read().strip()
    now = datetime.fromisoformat(args.now) if args.now else None
    published = datetime.fromisoformat(args.published_at) if args.published_at else None
    item = Item('offline', 'Offline', 'input', '', text.split('\n', 1)[0], '', None,
                published, raw_text=text, kind=args.kind)
    print(json.dumps(Filters(Config.load(args.config).rules).explain(item, now=now),
                     ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
