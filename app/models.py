"""Модель данных: единая структура публикации для всех источников."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone


def normalize_title(title: str) -> str:
    """Нормализация названия объявления для кросс-источниковой дедупликации."""
    t = (title or "").lower().replace("ё", "е")
    t = re.sub(r"[^a-zа-я0-9]+", "", t)
    return t


@dataclass
class Item:
    source_key: str          # ключ источника из config.yaml
    source_name: str         # отображаемое имя (канал/компания)
    external_id: str         # id публикации в источнике (post id, uid вакансии, slug)
    url: str                 # ссылка на оригинал
    title: str               # заголовок / первая строка
    summary: str             # краткое содержание
    apply_url: str | None    # ссылка на регистрацию/заявку, если найдена
    published_at: datetime | None
    registration_status: str = "unknown"   # open | closed | unknown
    deadline: datetime | None = None
    raw_text: str = ""
    categories: list[str] = field(default_factory=list)  # заполняется фильтрами
    kind: str = "publication"  # vacancy is set by verified website collectors
    experience: str = ""
    explicitly_closed: bool = False

    @property
    def item_key(self) -> str:
        return f"{self.source_key}:{self.external_id}"

    @property
    def title_hash(self) -> str:
        nt = normalize_title(self.title)
        if not nt:
            return ""
        return hashlib.sha256(nt.encode("utf-8")).hexdigest()[:16]

    def is_expired(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        if self.deadline is not None:
            dl = self.deadline
            if dl.tzinfo is None:
                dl = dl.replace(tzinfo=timezone.utc)
            return dl < now
        return False
