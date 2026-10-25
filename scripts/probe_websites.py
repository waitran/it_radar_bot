"""Read-only website probe: python -m scripts.probe_websites [--source KEY]."""
import argparse
import json
import logging

from app.config import Config
from app.logging_config import configure_logging
from app.main import WEBSITE_COLLECTORS
from app.collectors.website_http import WebsiteSourceError, WebsiteSnapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', help='Website key from config.yaml')
    args = parser.parse_args()
    configure_logging()
    try:
        cfg = Config.load()
    except Exception as exc:
        logging.getLogger('probe').error('Ошибка конфигурации (%s).', type(exc).__name__)
        return 2
    sources = [s for s in cfg.websites if not args.source or s.key == args.source]
    if not sources:
        parser.error('Source not found in config.yaml')
    failed = False
    for source in sources:
        try:
            items = WEBSITE_COLLECTORS[source.type](source).fetch()
            detail_errors = items.failed_details if isinstance(items,WebsiteSnapshot) else {}
            failed = failed or bool(detail_errors)
            result = {'source': source.key, 'status': 'partial' if detail_errors else 'ok',
                      'detail_errors': detail_errors, 'count': len(items),
                      'with_application_url': sum(i.apply_url is not None for i in items),
                      'with_published_date': sum(i.published_at is not None for i in items),
                      'items': [{'id': i.external_id, 'title': i.title, 'text_length': len(i.raw_text)} for i in items]}
        except WebsiteSourceError as exc:
            failed = True
            result = {'source': source.key, 'status': 'unavailable', 'reason': str(exc)}
        except Exception as exc:
            failed = True
            result = {'source': source.key, 'status': 'error', 'reason': type(exc).__name__}
        print(json.dumps(result, ensure_ascii=False), flush=True)
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())

