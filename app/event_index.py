"""Deterministic metadata. Multiple dates remain unknown, never guessed."""
import json
import re
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo
from app.date_rules import date_evidence, _DATE
from app.preferences import characteristics


def extract(item, timezone_name='Europe/Moscow'):
    tz = ZoneInfo(timezone_name)
    text = item.title+'\n'+(item.raw_text or item.summary)
    if not item.published_at:
        # No publication-year anchor: erase relative/yearless matches while
        # retaining clause labels for explicitly dated evidence.
        def explicit(match):
            groups = match.groupdict()
            return match[0] if groups['iso_year'] or groups['num_year'] or groups['year'] else ' '*len(match[0])
        text = _DATE.sub(explicit,text)
    deadlines, events = date_evidence(item, text, datetime.now(tz), tz)
    if re.search(r'(?<!\d)\d{1,2}\s*(?:[-–—]|и|или|по)\s*\d{1,2}\s+(?:январ|феврал|март|апрел|мая|июн|июл|август|сентябр|октябр|ноябр|декабр)',text,re.I):
        deadlines, events = [], []
    deadline = item.source_deadline or (deadlines[0] if len(deadlines) == 1 else None)
    # Legacy inferred item.deadline has no precision provenance; do not invent it.
    event = events[0] if len(events) == 1 else None
    topics, mode, city = characteristics(item)
    def fields(value):
        if not value: return None, None, None
        value = value.replace(tzinfo=tz) if value.tzinfo is None else value
        date_only = value.timetz().replace(tzinfo=None) == time.max
        return value.timestamp(), value.date().isoformat(), 'date' if date_only else 'time'
    start, day, precision = fields(event)
    end, deadline_day, deadline_precision = fields(deadline)
    return dict(event_at=start, event_day=day, event_precision=precision,
                deadline_at=end, deadline_day=deadline_day, deadline_precision=deadline_precision,
                topics=json.dumps(topics), format=mode, city=city,
                registration_status='closed' if item.explicitly_closed else item.registration_status)
