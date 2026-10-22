"""Read-only public Telegram probe: python -m scripts.probe_tg [--channel NAME]."""
import argparse
import json
import logging

from app.collectors.telegram_channel import fetch_channel, TelegramSourceError
from app.config import Config
from app.logging_config import configure_logging


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--channel', help='One username from config.yaml')
    parser.add_argument('--since-id', type=int, help='Fetch public history newer than this ID')
    args = parser.parse_args()
    if args.since_id is not None and not args.channel:
        parser.error('--since-id requires --channel')
    configure_logging()
    try:
        cfg = Config.load()
    except Exception as exc:
        logging.getLogger("probe").error("Ошибка чтения конфигурации (%s).", type(exc).__name__)
        return 2
    sources = [s for s in cfg.channels if not args.channel or s.username.lower() == args.channel.lower()]
    if not sources:
        parser.error('Channel not found in config.yaml')
    failed = False
    for source in sources:
        try:
            items = fetch_channel(source.username, since_id=args.since_id, max_pages=source.max_pages)
            result = {'channel': source.username, 'status': 'ok', 'count': len(items),
                      'missing_dates': sum(i.published_at is None for i in items),
                      'without_text': sum(not i.raw_text for i in items),
                      'min_id': min((int(i.external_id) for i in items), default=None),
                      'max_id': max((int(i.external_id) for i in items), default=None)}
        except TelegramSourceError as exc:
            failed = True
            result = {'channel': source.username, 'status': 'error', 'reason': str(exc)}
        print(json.dumps(result, ensure_ascii=False), flush=True)
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
