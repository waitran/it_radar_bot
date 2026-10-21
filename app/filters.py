"""Правила отбора: категории, стоп-слова, признаки закрытого набора/истёкшего срока."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from app.config import Rules
from app.models import Item

log = logging.getLogger("filters")

# Признаки явной открытости регистрации/набора (только при наличии этих фраз
# мы можем сказать владельцу, что набор открыт).
OPEN_MARKERS = [
    "открыт набор", "набор открыт", "идёт набор", "идет набор", "приём заявок",
    "принимаем заявки", "заявки принимаются", "регистрация открыта", "открытая регистрация",
    "заявки до", "подача заявок", "регистрация открыта", "открыта регистрация", "приём заявок", "дедлайн", "deadline", "успей", "успейте", "подача заявок", "остались места",
    "есть места", "register now", "apply now", "open for applications", "стартует",
    "старт приема", "ждём вас", "ждем вас", "присоединяйся", "присоединяйтесь",
    "хочешь участвовать", "хотите участвовать", "открываем набор", "запускаем набор",
]

# Признаки того, что событие/набор уже завершён.
CLOSED_PATTERNS = [
    r"подвед(?:ены|ли)\s+итог", r"заверши[лсяь]?[ао]?\b", r"состоял[сяась]",
    r"прошедш", r"прош[ею][лло]\s", r"итоги\s+(?:конкурса|хакатона|мероприятия|соревнования)",
    r"набор\s+закрыт", r"приём\s+заявок\s+закры", r"регистрация\s+закры",
    r"мероприятие\s+отмен", r"вакансия\s+отключена", r"\barchived\b", r"\bclosed\b",
]

DATE_RU = re.compile(r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\b")
MONTHS_RU = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6,
    "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "November": 11,
    "ноябр": 11, "декабр": 12,
}


def _norm(s: str) -> str:
    s = s.lower().replace("ё", "е")
    return re.sub(r"\s+", " ", s)


def parse_deadline(text: str, published_at: datetime | None) -> datetime | None:
    """Пытаемся извлечь дедлайн подачи («до 15 ноября», «до 30.11.2026»)."""
    t = _norm(text or "")
    m = re.search(r"(?:до|deadlin(?:e)?|последн(?:ий|яя)\s+(?:дата\s+)?)(?:\s|\u00a0)*(\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?|(?:\d{1,2}\s+(?:январ|феврал|март|апрел|мая|ма|июн|июл|август|сентябр|октябр|ноябр|декабр)[а-я]*))", t)
    if not m:
        m = re.search(r"до\s+(\d{1,2}\s+[а-я]+)", t)
    if not m:
        return None
    token = m.group(1).strip()
    day = month = year = None
    dm = DATE_RU.match(token)
    if dm:
        day, month = int(dm.group(1)), int(dm.group(2))
        y = dm.group(3)
        if y:
            year = int(y) if len(y) > 2 else 2000 + int(y)
    else:
        pm = re.match(r"(\d{1,2})\s+([a-zа-я]+)", token)
        if pm:
            day = int(pm.group(1))
            mon_root = pm.group(2)
            for root, num in MONTHS_RU.items():
                if mon_root.startswith(root[:3]):
                    month = num
                    break
    if not day or not month:
        return None
    if year is None:
        base = published_at or datetime.now(timezone.utc)
        year = base.year
        try:
            cand = datetime(year, month, day, tzinfo=timezone.utc)
        except ValueError:
            return None
        if cand < base:  # дедлайн в следующем году
            year += 1
    try:
        return datetime(year, month, day, 23, 59, tzinfo=timezone.utc)
    except ValueError:
        return None


def detect_status(text: str, deadline: datetime | None, rules: Rules) -> str:
    """open | closed | unknown. Unknown никогда не трактуем как открытый."""
    t = _norm(text or "")
    for pat in CLOSED_PATTERNS:
        if re.search(pat, t):
            return "closed"
    for w in rules.closed_words:
        if w and w in t:
            return "closed"
    if deadline is not None and deadline < datetime.now(timezone.utc):
        return "closed"
    has_open = any(k in t for k in OPEN_MARKERS)
    if has_open and (deadline is None or deadline >= datetime.now(timezone.utc)):
        return "open"
    return "unknown"


class Filters:
    def __init__(self, rules: Rules):
        self.rules = rules

    def match_categories(self, text: str) -> tuple[list[str], int]:
        """Возвращает (категории, score) по нормализованному тексту."""
        t = _norm(text)
        cats: list[str] = []
        score = 0
        for key, cat in self.rules.categories.items():
            hits = sum(1 for kw in cat["keywords"] if kw and kw in t)
            if hits:
                cats.append(key)
                score += hits
        return cats, score

    def is_excluded(self, text: str) -> str | None:
        t = _norm(text)
        for kw in self.rules.exclude_keywords:
            if kw and kw in t:
                return kw
        return None

    def check(self, item: Item) -> tuple[bool, str]:
        """Основная проверка правил. Возвращает (подходит?, причина отказа)."""
        full_text = f"{item.title}\n{item.summary or ''}\n{item.raw_text or ''}"

        # возраст публикации
        if item.published_at is not None:
            age_days = (datetime.now(timezone.utc) - item.published_at).days
            if age_days > self.rules.max_age_days:
                return False, f"старше {self.rules.max_age_days} дней"

        # явно закрытое объявление / истёкший срок
        item.deadline = item.deadline or parse_deadline(full_text, item.published_at)
        item.registration_status = detect_status(full_text, item.deadline, self.rules)
        if item.registration_status == "closed":
            return False, "объявление закрыто или срок подачи истёк"
        if item.is_expired():
            return False, "срок подачи заявок истёк"

        excl = self.is_excluded(full_text)
        if excl:
            return False, f"стоп-слово «{excl}» (новости/реклама)"

        cats, score = self.match_categories(full_text)
        if score < self.rules.min_score or not cats:
            return False, "не подходит ни под одну категорию отбора"
        item.categories = cats
        log.debug("matched %s categories=%s score=%d", item.item_key, cats, score)
        return True, ""
