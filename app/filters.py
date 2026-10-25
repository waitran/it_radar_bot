"""Deterministic selection of actionable entry-level IT opportunities."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.config import Rules
from app.date_rules import date_evidence, _aware
from app.models import Item

log = logging.getLogger("filters")

_IT = re.compile(
    r"\b(?:python|java|postgresql)(?=\d)|\b(?:it|ict|ит|ai|ml|llm|devops|sre|soc|ctf|qa|sql|python|java|javascript|"
    r"typescript|golang|go|rust|frontend|backend|fullstack|android|ios|linux|data|cyber|"
    r"cloud|software|developer|programming|tech|тех|ib|иб)\b|"
    r"информационн\w* (?:технолог|безопасност|систем)|программирован|"
    r"разработ(?:к|чик)|кибер|цифров|машинн\w* обучен|нейросет|нейронн|"
    r"искусственн\w* интеллект|тестирован|сетев\w* инженер|системн\w* (?:аналит|админист)|"
    r"бизнес[ -]аналит|робототех|компьютер|\b(?:ии|цод|presale|siem)\b",
    re.I,
)
_BEGINNER = re.compile(r"стажиров|стаж[её]р|\b(?:intern|internship|trainee|junior|entry[ -]level)\b|младш|начинающ|без опыта|нет опыта|опыт не требуется|\b0\s*[-–—]\s*1\b|(?:приглашаем|ждем)\s+(?:студент|выпускник)|опыт\w*.{0,20}(?:до\s*1\s*(?:года|лет)|0\s*[-–—]\s*1)", re.I)
_SENIOR = re.compile(r"\b(?:senior|middle|lead|principal|head of)\b|ведущ\w*|руководител\w*|старш\w*", re.I)
_RESTRICTED_AUDIENCE = re.compile(r"(?:только|исключительно)\s+для\s+(?:опытных\s+)?(?:руководител|директор|senior|middle|lead|ciso|cto)|не\s+(?:предназначен\w*|подходит)\s+для\s+(?:начинающ|студент)", re.I)
_INVITATION = re.compile(
    r"приглаша|присоедин|участвуй|прими участие|принять участие|приходи|запис(?:аться|ывай|ь)|"
    r"зарегистрируй|зарегистрироваться|регистрируй|регистрация\s+(?:уже\s+)?открыта|при[её]м заявок|подать (?:заявку|резюме)|"
    r"регистрация[^.!?\n]{0,50}по ссылке|пода(?:й|вай|ча) заяв|отклик|набор|жд[её]м|состоится|пройд[её]т|"
    r"стартует|начн[её]тся|успей|\b(?:register|registration|apply|join us|applications open)\b",
    re.I,
)
_RETROSPECTIVE = re.compile(
    r"(?:митап|хакатон|конференци\w*|встреч\w*|вебинар|соревнован\w*|лекци\w*)[^.!?\n]{0,65}?(?:уже\s+)?(?:прош[её]л|прошла|прошли|состоял|заверш)|"
    r"(?:прош[её]л|прошла|прошли|состоял\w*|завершил\w*)\s+(?:наш\w*\s+)?(?:митап|хакатон|конференц|встреч|вебинар|лекц)|"
    r"мы посетили|мы побывали|вспоминаем|вспоминатель|подводим итоги|итоги (?:хакатона|конкурса|митапа|конференции|олимпиады)|"
    r"как (?:прош[её]л|прошла|прошли)|(?:смотр|посмотр)\w* запись|запись (?:вебинара|лекции|митапа|конференции)|"
    r"\b(?:event recap|watch the recording|took place|was held)\b",
    re.I,
)
# Informational markers require a real next action. A future date mentioned in
# a news recap is not itself an invitation to take part.
_INFORMATIONAL = re.compile(
    r"в карточках|на карточках|что это (?:такое|и зачем)|"
    r"(?:подборка|дайджест|обзор|итоги|новости|новостей)[^.!?\n]{0,45}(?:недел|рабоч|новост)|"
    r"обсудил[аи]?|рассказал[аи]?|состоял[аои]сь|"
    r"завершил(?:ся|ась|ись)[^.!?\n]{0,60}этап",
    re.I,
)
_ACTION = re.compile(
    r"приглаша(?:ем|ю)|присоединяй|участвуй|прими(?:те)? участие|принять участие|приходи|"
    r"запис(?:аться|ывай|ь)|зарегистрируй|зарегистрироваться|регистрируй|"
    r"регистрация[^.!?\n]{0,50}по ссылке|подать (?:заявку|резюме)|пода(?:й|вай|ча) заяв|откликни|успей|"
    r"(?:открыт\w*|продолжается|начался)[^.!?\n]{0,25}(?:набор|при[её]м заявок|регистрац)|"
    r"(?:регистрация|набор|при[её]м заявок)\s+(?:уже\s+)?открыт|"
    r"\b(?:register|apply|join us|applications open)\b",
    re.I,
)
_EDUCATOR_AUDIENCE = re.compile(
    r"(?:митап|встреч\w*|конференц\w*)[^.!?\n]{0,70}для\s+(?:преподавател|учител)|"
    r"для тех,? кто (?:развивает современное высшее образование|препода[её]т)", re.I,
)

_CLOSED = re.compile(
    r"(?:регистрация|набор|при[её]м заявок|подача заявок|конкурс|хакатон|митап|мероприятие|конференция|вебинар|лекция|вакансия)\s*(?:уже\s+)?(?:закрыт\w*|заверш[её]н\w*|окончен\w*|приостановлен\w*)|"
    r"(?:закрыт\w*|заверш[её]н\w*|окончен\w*)\s+(?:регистрация|набор|при[её]м заявок)|"
    r"(?:заявки|отклики)\s+(?:больше\s+)?не принимаются|(?:все\s+)?места\s+(?:уже\s+)?(?:заняты|закончились)|"
    r"\b(?:registration|applications?|vacancy)\s+(?:is\s+|are\s+)?(?:closed|ended)|\bsold out\b",
    re.I,
)
_OPEN = re.compile(r"регистрация\s+(?:уже\s+)?открыта|открыт\w*\s+(?:регистрация|набор|при[её]м заявок)|при[её]м заявок\s+открыт|\bregistration\s+(?:is\s+)?open\b", re.I)
_PROMOTION = re.compile(r"розыгрыш|разыгрываем|промокод|скидк\w*\s+\d|купи(?:те)?\s|\bbuy now\b", re.I)


_NOT_OPEN = re.compile(r"(?:регистрац\w*|набор|прием заявок)[^.!?;\n]{0,45}(?:пока\s+|еще\s+)?не\s+(?:открыт|начат|начал)|регистрация\s+не\s+будет", re.I)
_NONTECH_ROLE = re.compile(r"подбор\w* персонала|отдел кадров|документооборот|товарооборот|мерч|\bbdm\b|продаж|продвижени|\bhr\b|бухгалтер|рекрутер", re.I)
_EVENT_TITLE = re.compile(r"митап|конференц|лекци|вебинар|встреч|воркшоп|конкурс|хакатон|олимпиад|\bctf\b", re.I)
_EXPERIENCE = re.compile(r"\bопыт(?:а)?\b(?:\s+работы)?[^.!?;\n]{0,35}?(?:от\s+|не менее\s+|минимум\s+)?(\d+)\s*(?:[-–—]\s*\d+\s*)?(?:лет|год|года)\b", re.I)


def _norm(value: str) -> str:
    return re.sub(r"[ \t\u00a0]+", " ", (value or "").lower().replace("ё", "е"))


def _keyword(keyword: str, text: str) -> bool:
    # Prefixes still support Russian inflection, but never match inside a word:
    # "акция" cannot exclude "транзакция" and "it" cannot match "security".
    pattern = r"(?<!\w)" + re.escape(_norm(keyword).strip())
    if re.fullmatch(r"[a-z]+", keyword, re.I):
        pattern += r"(?!\w)"
    return bool(keyword and re.search(pattern, text, re.I))


def parse_deadline(text: str, published_at: datetime | None, *, now=None, timezone_name="Europe/Moscow") -> datetime | None:
    now = _aware(now or datetime.now(timezone.utc), ZoneInfo(timezone_name))
    stub = Item("", "", "", "", "", "", None, published_at)
    deadlines, _ = date_evidence(stub, _norm(text), now, ZoneInfo(timezone_name))
    return deadlines[0] if len(deadlines) == 1 else None


def detect_status(text: str, deadline: datetime | None, rules: Rules, *, now=None) -> str:
    tz = ZoneInfo(rules.timezone)
    now = _aware(now or datetime.now(timezone.utc), tz)
    text = _norm(text)
    if _CLOSED.search(text) or any(_keyword(word, text) for word in rules.closed_words):
        return "closed"
    if deadline is not None and _aware(deadline, tz) < now:
        return "closed"
    if not _NOT_OPEN.search(text) and _OPEN.search(text) and deadline is not None:
        return "open"
    return "unknown"


class Filters:
    def __init__(self, rules: Rules):
        self.rules = rules

    def match_categories(self, text: str) -> tuple[list[str], int]:
        text = _norm(text)
        categories, score = [], 0
        for key, category in self.rules.categories.items():
            hits = sum(_keyword(word, text) for word in dict.fromkeys(category["keywords"]))
            if hits:
                categories.append(key)
                score += hits
        return categories, score

    def is_excluded(self, text: str) -> str | None:
        text = _norm(text)
        return next((word for word in self.rules.exclude_keywords if _keyword(word, text)), None)

    def check(self, item: Item, *, now=None) -> tuple[bool, str]:
        tz = ZoneInfo(self.rules.timezone)
        now = _aware(now or datetime.now(timezone.utc), tz)
        # Do not duplicate/truncate evidence by combining the summary and full text.
        text = _norm(item.title + "\n" + (item.raw_text or item.summary or ""))
        item.categories = []
        deadlines, events = date_evidence(item, text, now, tz)
        if item.deadline is not None:
            deadlines = [_aware(item.deadline, tz)]
        item.deadline = deadlines[0] if len(deadlines) == 1 else None
        item.registration_status = detect_status(text, item.deadline, self.rules, now=now)
        if item.explicitly_closed or item.registration_status == "closed":
            item.registration_status = "closed"
            return False, "объявление закрыто или срок подачи истёк"
        if deadlines and all(value < now for value in deadlines):
            item.registration_status = "closed"
            return False, "все распознанные сроки подачи истекли"
        if events and all(value < now for value in events):
            return False, "дата мероприятия уже прошла"
        if _NOT_OPEN.search(text):
            return False, "регистрация или набор ещё не открыты"
        if item.published_at is not None and now - _aware(item.published_at, tz) > timedelta(days=self.rules.max_age_days):
            return False, f"старше {self.rules.max_age_days} дней"
        excluded = self.is_excluded(text)
        if excluded:
            return False, f"стоп-слово «{excluded}» (новости/реклама)"
        categories, score = self.match_categories(text)
        vacancy = item.kind == "vacancy" or (not _EVENT_TITLE.search(item.title) and bool(
            re.search(r"\bваканс\w*", item.title) or re.search(r"\bищем\b|\bтребуется\b|открыта позиция|дайджест[_ ]ваканс", text) or _SENIOR.search(item.title)))
        beginner = bool(_BEGINNER.search(text + " " + item.experience)
                        or re.search(r"студент|выпускник", item.title + " " + item.experience, re.I))
        if vacancy:
            minimum = re.search(r"(?:от\s*)?(\d+)\s*(?:[-–—]\s*\d+\s*)?(?:лет|год|года|years?)", item.experience)
            required = _EXPERIENCE.search(text)
            experienced = bool(
                (minimum and int(minimum[1]) >= 2)
                or (required and int(required[1]) >= 2
                    and not re.search(r"опыт[^.!?;\n]{0,20}до\s+", required[0]))
            )
            if not beginner or experienced or _SENIOR.search(item.title + " " + item.experience):
                return False, "вакансия не начального уровня"
            if self.rules.require_it_context and _NONTECH_ROLE.search(item.title):
                return False, "вакансия не по IT-специальности"
            # Benefit paragraphs mentioning conferences do not make a job an event.
            categories = ["internships"] if "internships" in self.rules.categories else []
            score = max(score, 1)
        if not categories or score < self.rules.min_score:
            return False, "не подходит ни под одну включённую категорию"
        if _RESTRICTED_AUDIENCE.search(text):
            return False, "участие ограничено опытными специалистами"
        context = re.sub(r"\b(?:ит|it)[ -]*(?:компани\w*|индустри\w*|отрасл\w*|company)", "", text)
        if self.rules.require_it_context and not _IT.search(context):
            return False, "нет явного IT-контекста"
        if (_EDUCATOR_AUDIENCE.search(text) and not re.search(
                r"для\s+(?:студент|начинающ)|(?:преподавател\w*|учител\w*)\s+и\s+(?:студент|начинающ)", text)):
            return False, "мероприятие для преподавателей, а не начинающих IT-специалистов"
        if _INFORMATIONAL.search(text) and not (_ACTION.search(text) or vacancy and beginner):
            return False, "информационная публикация без приглашения к участию"
        future = any(value >= now for value in deadlines + events)
        if _RETROSPECTIVE.search(text) and not future:
            return False, "отчёт о прошедшем событии или запись"
        if _PROMOTION.search(text):
            return False, "рекламный розыгрыш или предложение покупки"
        if not (_INVITATION.search(text) or future or vacancy and beginner):
            return False, "нет приглашения, заявки или предстоящего события"
        item.categories = categories
        return True, ""
