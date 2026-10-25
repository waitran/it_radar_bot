"""Conservative deadline/event date evidence; no automatic future-year rollover."""
import re
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
from app.models import Item

_MONTHS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "мая": 5, "май": 5,
    "июн": 6, "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}
_DATE = re.compile(
    r"(?<!\w)(?:(?P<iso_year>20\d{2})-(?P<iso_month>\d{2})-(?P<iso_day>\d{2})|"
    r"(?P<num_day>\d{1,2})[./](?P<num_month>\d{1,2})(?:[./](?P<num_year>20\d{2}|\d{2}))?|"
    r"(?P<day>\d{1,2})\s+(?P<month>январ\w*|феврал\w*|март\w*|апрел\w*|мая|май|июн\w*|июл\w*|август\w*|сентябр\w*|октябр\w*|ноябр\w*|декабр\w*)(?:\s+(?P<year>20\d{2}))?|"
    r"(?P<relative>сегодня|завтра|послезавтра)\b)(?!\d|[./]\d|[A-Za-z])",
    re.I,
)
_CLOCK = re.compile(r"^\s*(?:г(?:ода|\.)?\s*)?(?:в\s*)?(?P<hour>\d{1,2})[:.](?P<minute>\d{2})(?!\d)")
_DEADLINE_CONTEXT = re.compile(
    r"дедлайн|deadline|не позднее|(?:регистр\w*|зарегистр\w*|заяв\w*|отклик\w*|при[её]м\w*|наб(?:ор|ираем)|пода(?:й|вай|ть))[^\n.!?;]{0,90}(?:до|по)\s*$|"
    r"(?:при[её]м|подача)\s+заявок\s*[:—–-]?\s*$|(?:apply|applications?)[^\n.!?;]{0,40}by\s*$",
    re.I,
)
_EVENT_CONTEXT = re.compile(r"\bкогда\b|финал|митап|встреч|конференц|хакатон|лекц|соревнован|олимпиад|конкурс|вебинар|семинар|воркшоп|урок|сборы|приходи|присоедин|дата проведения|сроки проведения|пройд[её]т|состоится|начн[её]тся|\b(?:meetup|conference|event|hackathon|webinar|ctf)\b", re.I)

_SCHEDULE_LINE = re.compile(r"^\s*[📅🗓🗓️📆\s]*(?:(?:дата|когда|время|начало|сроки проведения)\s*[:—–-]\s*|[📅🗓📆]\ufe0f?\s*)$", re.I)


def _aware(value: datetime, tz: ZoneInfo) -> datetime:
    return value.replace(tzinfo=tz) if value.tzinfo is None else value.astimezone(tz)


def date_evidence(publication: Item, text: str, now: datetime, tz: ZoneInfo) -> tuple[list[datetime], list[datetime]]:
    """Only dates with a recognizable deadline/event context become evidence.

    A missing year belongs to the publication year, never the next future year.
    Date-only deadlines include the entire local date. Ambiguous multiple dates
    are kept separately; callers only reject if every relevant date has passed.
    """
    anchor = _aware(publication.published_at, tz) if publication.published_at else now
    deadlines: list[datetime] = []
    events: list[datetime] = []
    previous_kind, previous_end = None, 0
    for match in _DATE.finditer(text):
        # A software version is not an event date (e.g. "Python 3.12").
        if re.search(r"(?:python|java|postgres(?:ql)?|верси[яию]|релиз|\bv)\s*$", text[max(0, match.start()-30):match.start()], re.I):
            continue
        groups = match.groupdict()
        try:
            if groups["relative"]:
                if publication.published_at is None:
                    continue
                offset = {"сегодня": 0, "завтра": 1, "послезавтра": 2}[groups["relative"].lower()]
                day = (anchor + timedelta(days=offset)).date()
            else:
                year_string = groups["iso_year"] or groups["num_year"] or groups["year"]
                year = int(year_string) if year_string else anchor.year
                if year < 100:
                    year += 2000
                month_string = groups["iso_month"] or groups["num_month"]
                month = int(month_string) if month_string else next(number for prefix, number in _MONTHS.items() if groups["month"].lower().startswith(prefix))
                day_number = int(groups["iso_day"] or groups["num_day"] or groups["day"])
                day = datetime(year, month, day_number).date()
            clock = _CLOCK.match(text[match.end():])
            cutoff = time(int(clock["hour"]), int(clock["minute"])) if clock else time.max
            value = datetime.combine(day, cutoff, tzinfo=tz)
        except (ValueError, StopIteration):
            continue
        before = text[max(0, match.start() - 150):match.start()]
        # Restrict the semantic label to the nearest clause. A date must not
        # inherit "registration" from an unrelated preceding sentence.
        clause = re.split(r"[\n!?;]|\.(?!\d)", before)[-1]
        bridge = text[previous_end:match.start()]
        inherited = previous_kind if re.fullmatch(r"\s*(?:и|или|,|[-–—])\s*", bridge) else None
        kind = None
        if _DEADLINE_CONTEXT.search(clause) or inherited == "deadline":
            deadlines.append(value)
            kind = "deadline"
        elif (inherited == "event"
              or (_SCHEDULE_LINE.fullmatch(clause) and _EVENT_CONTEXT.search(text))
              or _EVENT_CONTEXT.search(clause) or _EVENT_CONTEXT.search(text[match.end():match.end() + 65].split("\n")[0])):
            events.append(value)
            kind = "event"
        previous_kind = kind
        previous_end = match.end() + (clock.end() if clock else 0)
    return list(dict.fromkeys(deadlines)), list(dict.fromkeys(events))

