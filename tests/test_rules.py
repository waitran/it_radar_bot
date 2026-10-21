"""Тесты правил отбора и дедупликации (без сети).

Запуск:  python -m tests.test_rules   или   pytest tests/
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone

os.environ.setdefault("CONFIG_PATH", os.path.join(os.path.dirname(__file__), "..", "config.yaml"))

from app.config import Config
from app.db import Database
from app.filters import Filters
from app.models import Item


def _item(title: str, summary: str = "", published: datetime | None = None, key: str = "x") -> Item:
    return Item(
        source_key="test", source_name="Test", external_id=key,
        url=f"https://example.com/{key}", title=title, summary=summary,
        apply_url=None, published_at=published or datetime.now(timezone.utc),
        raw_text=summary,
    )


def test_internship_matches():
    cfg = Config.load()
    f = Filters(cfg.rules)
    ok, reason = f.check(_item("Открыт набор на стажировку Python-разработчик", "для студентов без опыта"))
    assert ok, reason
    print("ok: стажировка распознана")


def test_ad_excluded():
    cfg = Config.load()
    f = Filters(cfg.rules)
    ok, reason = f.check(_item("Скидка 50% на наш новый продукт, купите сейчас!", "промокод IT2026"))
    assert not ok and "стоп-слово" in reason, reason
    print("ok: реклама отброшена")


def test_closed_event_excluded():
    cfg = Config.load()
    f = Filters(cfg.rules)
    ok, reason = f.check(_item("Хакатон завершён: подвели итоги конкурса", "победители получили призы"))
    assert not ok and ("закрыто" in reason or "истёк" in reason), reason
    print("ok: закрытое объявление отброшено")


def test_deadline_in_past_excluded():
    cfg = Config.load()
    f = Filters(cfg.rules)
    past = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%d.%m.%Y")
    ok, reason = f.check(_item("Приглашаем на митап по кибербезопасности", f"заявки до {past}"))
    assert not ok, reason
    print("ok: истёкший срок подачи отброшен")


def test_unknown_status_not_claimed_open():
    cfg = Config.load()
    f = Filters(cfg.rules)
    it = _item("Лекция по машинному обучению в университете", "преподаватель расскажет про нейросети")
    ok, _ = f.check(it)
    assert ok
    assert it.registration_status != "open", "нельзя утверждать, что регистрация открыта, без явных признаков"
    print("ok: неизвестный статус не трактуется как «открыто»")


def test_dedup_and_baseline(tmp_path: str | None = None):
    cfg = Config.load()
    db_file = tempfile.mktemp(suffix=".db")
    db = Database(db_file)
    f = Filters(cfg.rules)
    from app.main import Bot

    bot = Bot.__new__(Bot)  # без TelegramNotifier — сеть не трогаем
    bot.cfg, bot.db, bot.filters = cfg, db, f

    it = _item("Стартовал приём заявок на хакатон по информационной безопасности", key="h1")
    assert bot.process_item(it, baseline=True) is False      # baseline — не к отправке
    assert db.exists("test:h1")
    assert bot.process_item(it, baseline=False) is False     # повтор — дубль
    it2 = _item("Стартовал приём заявок на хакатон по информационной безопасности",
                key="h2", published=datetime(2000, 1, 1, tzinfo=timezone.utc))  # тот же заголовок из др. источника
    it2.source_key = "other"
    assert bot.process_item(it2, baseline=False) is False    # кросс-дедуп по title_hash
    it3 = _item("Карьерная встреча для студентов: день открытых дверей", key="c1")
    assert bot.process_item(it3, baseline=False) is True     # новое подходящее — в отправку
    assert len(db.pending_unnotified()) == 1
    os.remove(db_file)
    print("ok: дедупликация, baseline и pending_unnotified работают")


if __name__ == "__main__":
    test_internship_matches()
    test_ad_excluded()
    test_closed_event_excluded()
    test_deadline_in_past_excluded()
    test_unknown_status_not_claimed_open()
    test_dedup_and_baseline()
    print("\nВсе тесты пройдены.")
