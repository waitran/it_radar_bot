"""Offline labelled corpus evaluation. Never contacts sources or opens a database."""
import argparse
import importlib.util
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.config import Config
from app.filters import Filters
from app.models import Item

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 27, 12, tzinfo=ZoneInfo('Europe/Moscow'))


def evaluate(filter_class=Filters, config_path=ROOT/"config.yaml"):
    rows = json.loads((ROOT/'tests/fixtures/filtering/publications.json').read_text())
    selector = filter_class(Config.load(str(config_path)).rules)
    counts = dict(TP=0, FP=0, TN=0, FN=0)
    decisions = []
    for row in rows:
        item = Item('offline', 'Offline', row['id'], '', row['title'], '', None,
                    NOW, raw_text=row['text'])
        accepted, reason = selector.check(item, now=NOW)
        key = ('T' if accepted == row['expected'] else 'F') + ('P' if accepted else 'N')
        counts[key] += 1
        decisions.append(dict(id=row['id'], expected=row['expected'], accepted=accepted, reason=reason))
    precision = counts['TP'] / (counts['TP'] + counts['FP']) if counts['TP'] + counts['FP'] else 0
    recall = counts['TP'] / (counts['TP'] + counts['FN']) if counts['TP'] + counts['FN'] else 0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0
    return dict(counts, precision=precision, recall=recall, f1=f1, decisions=decisions)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--filter-file', type=Path, help='Optional archived baseline filters.py')
    parser.add_argument('--config', type=Path, default=ROOT/'config.yaml')
    args = parser.parse_args()
    cls = Filters
    if args.filter_file:
        spec = importlib.util.spec_from_file_location('baseline_filters', args.filter_file)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cls = module.Filters
    print(json.dumps(evaluate(cls, args.config), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
