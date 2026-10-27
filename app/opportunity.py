"""Text evidence for deterministic IT opportunity selection (no I/O).

Short ambiguous words need a technical collocation. Public actions are scoped
by clause; a future date or another team's plans are never public invitations.
"""
from __future__ import annotations

import re

STRONG_IT = re.compile(
    r'\b(?:python|java|postgresql)(?=\d)|'
    r'\b(?:python|java|javascript|typescript|golang|rust|devops|devsecops|sre|soc|ctf|'
    r'qa|sql|postgresql|kubernetes|docker|linux|frontend|backend|fullstack|pytorch|tensorflow|'
    r'android|ios|software|developer|programming|cybersecurity|siem|llm)\b|'
    r'информационн\w*\s+(?:технолог|безопасност|систем)|программирован|'
    r'разработ\w*\s+(?:программн\w*\s+обеспеч|по\b|приложен|сервис)|'
    r'программн\w*\s+обеспеч|кибербезопас|робототех|битв\w*\s+роботов|'
    r'машинн\w*\s+обучен|нейросет|нейронн\w*\s+сет|искусственн\w*\s+интеллект|'
    r'компьютерн\w*\s+(?:наук|сет|зрен|безопас)|сетев\w*\s+инженер|'
    r'системн\w*\s+(?:аналит|администр)|тестирован\w*\s+(?:по\b|программ|приложен)|'
    r'техническ\w*\s+(?:конференц|митап|семинар)|'
    r'\b(?:data\s+(?:science|engineering|scientist)|machine learning|cloud\s+(?:computing|infrastructure)|'
    r'(?:язык\w*|разработ\w*|программирован\w*)\s+(?:на\s+)?go|go\s+(?:разработ\w*|developer))\b|'
    r'\b(?:ai|ml)[ -]+(?:meetup|workshop|engineer|митап|курс|инженер)|'
    r'\b(?:it|ит)[ -]+(?:митап|вебинар|конференц|специалист|школ|стажиров|олимпиад)|'
    r'(?:хакатон\w*|конференц\w*|митап|олимпиад\w*)\s+(?:по\s+)?(?:ии|иб|ai|ml|it|ит)\b|'
    r'(?:день карьеры|карьерн\w*\s+встреч\w*|студент\w*)\s+(?:для\s+)?(?:it|ит)\b',
    re.I,
)
WEAK_IT = re.compile(r'\b(?:it|ит|go|tech|data|cloud|иб|ии|ai|ml|ib)\b|цифров\w*|технолог\w*', re.I)

# Topic association guards, not a publication blacklist: an independent IT
# invitation in another clause can still pass.
OTHER_ACTIVITY = re.compile(
    r'донор|донаци|служб\w* крови|благотвор|волонтер|спортив|волейбол|футбол|'
    r'фотограф|творческ|плакат|стих|настольн\w*\s+игр', re.I,
)
PUBLIC_ACTION = re.compile(
    r'приглаша(?:ем|ю)|присоединяй|участвуй|прими(?:те)?\s+участие|приходи|'
    r'запис(?:аться|ывай|ь|итесь)|зарегистрируй|зарегистрироваться|регистрируй|'
    r'регистрация[^.!?;\n]{0,50}(?:по ссылке|открыта)|'
    r'(?:регистрация|прием заявок|заявки)\s+до\s+(?=\d|сегодня|завтра)|подать\s+(?:заявку|резюме)|'
    r'пода(?:й|вай|ча)\w*\s+заяв|отклик(?:ни|айтесь|нуться)|'
    r'(?:открыт\w*|продолжается|начался|стартовал)\s+(?:набор|прием заявок|регистрац)|'
    r'(?:регистрация|набор|прием заявок)\s+(?:уже\s+)?открыт|'
    r'вход\s+свободн|(?:можно|можете)\s+(?:бесплатно\s+)?(?:посетить|прийти|принять участие)|'
    r'ждем\s+(?:вас|зрител|участник)|для\s+всех\s+желающих|'
    r'(?:заявки|резюме)\s+принимаются|(?:присылайте|отправляйте)\s+(?:заявки|резюме)|'
    r'\b(?:register|apply|join us|applications open|admission free)\b', re.I,
)
NARRATIVE = re.compile(
    r'\b(?:они|студент\w*|команд\w*|делегат\w*|участник\w*)\b[^.!?;\n]{0,65}'
    r'(?:смогли|успел\w*|решил\w*|планируют|будут)|'
    r'только\s+по\s+персональному\s+приглашению|'
    r'(?:они|наши\w*\s+(?:команд\w*|студент\w*))[^.!?;\n]{0,45}(?:смогут|могут)|'
    r'(?:рассказ\w*|объясн\w*|инструкци\w*)[^.!?;\n]{0,35}(?:как|чтобы)|'
    r'\bне\s*$|\bнельзя\s*$|\bне нужно\s*$|\bне надо\s*$', re.I,
)
NEWS = re.compile(
    r'команд\w*[^.!?;\n]{0,65}(?:выехал|представят|примут участие|выступят)|'
    r'(?:наш\w*\s+)?студент\w*[^.!?;\n]{0,50}(?:выиграл|занял|победил)|'
    r'университет[^.!?;\n]{0,45}(?:представят|представит|подписал)|'
    r'делегаци|участники уже определены', re.I,
)
RETROSPECTIVE = re.compile(
    r'(?:отчет|итоги)\s+о?\s*(?:прошедш\w*\s+)?(?:хакатон|конференц|митап|соревнован)|'
    r'(?:хакатон\w*|конференц\w*|митап|соревнован\w*|олимпиад\w*)[^.!?;\n]{0,65}'
    r'(?:прошел|прошла|прошли|завершил|состоял)|'
    r'смотрите запись|мы посетили|мы побывали|watch the recording|event recap', re.I,
)
EVENT = re.compile(r'митап|конференц|лекци|семинар|вебинар|воркшоп|форум|встреч|курс|школ|хакатон|соревнован|олимпиад|чемпионат|конкурс|\bctf\b', re.I)
NON_IT_TARGET = re.compile(
    r'(?:кубок|конкурс|чемпионат|турнир|марафон|фестивал|экскурси|выставк)\w*|'
    r'служб\w* крови|донаци|донорск\w* движен|личн\w* кабинет', re.I,
)
SPONSOR = re.compile(r'спонсор|при поддержке|партнер мероприятия', re.I)
HISTORICAL_ACTION = re.compile(
    r'(?:регистрация|набор|прием заявок)\s+(?:был\w*|ранее)|(?:можно|нужно)\s+было', re.I,
)
RECORDING_ACTION = re.compile(
    r'(?:просмотр|смотр|запис[ьи]).{0,25}(?:запис[ьи]|вебинар|лекци)|к просмотру', re.I,
)


def it_signals(text: str) -> list[str]:
    # A hostname or URL path is not evidence about an event's subject.
    visible = re.sub(r'https?://\S+|www\.\S+', '', text, flags=re.I)
    return list(dict.fromkeys(m[0] for m in STRONG_IT.finditer(visible)))


def action_evidence(text: str, *, require_it: bool = True) -> list[str]:
    """Keep only public actions associated with the technical opportunity.

    A registration continuation may inherit the preceding event heading, but
    an explicitly different activity cannot inherit its technical evidence.
    """
    clauses = [s.strip() for s in re.split(r'[\n!?;]|\.(?!\d)', text) if s.strip()]
    context_is_it = False
    actions = []
    for clause in clauses:
        technical = bool(it_signals(clause))
        other = bool(OTHER_ACTIVITY.search(clause) and
                     (EVENT.search(clause) or NON_IT_TARGET.search(clause)))
        other = other or bool(re.search(r'экскурси|выставк|личн\w* кабинет', clause))
        if SPONSOR.search(clause):
            technical = False
        # Explicitly naming a new non-technical event resets inherited context.
        if other:
            context_is_it = False
        if technical:
            context_is_it = True
        if re.search(r'только по персональному приглашению', clause):
            continue
        if HISTORICAL_ACTION.search(clause):
            continue
        if RETROSPECTIVE.search(clause) or RECORDING_ACTION.search(clause):
            continue
        for match in PUBLIC_ACTION.finditer(clause):
            if NARRATIVE.search(clause[:match.start()]):
                continue
            if require_it and other and not technical:
                continue
            if require_it and not (technical or context_is_it):
                continue
            actions.append(match[0])
    return list(dict.fromkeys(actions))


def event_type(text: str) -> str | None:
    """A display hint based on literal evidence, never a category expansion."""
    patterns = [
        (r'чемпионат[^.!?\n]{0,80}(?:робототех|битв\w*\s+роботов)', 'Чемпионат по робототехнике'),
        (r'хакатон|\bhackathon\b', 'Хакатон'), (r'\bctf\b|capture the flag', 'CTF'),
        (r'олимпиад', 'Олимпиада'), (r'чемпионат', 'Чемпионат'),
        (r'соревнован', 'Соревнование'), (r'конкурс', 'Конкурс'),
        (r'конференц|\bconference\b', 'Конференция'), (r'митап|\bmeetup\b', 'Митап'),
        (r'вебинар|\bwebinar\b', 'Вебинар'), (r'воркшоп|\bworkshop\b', 'Воркшоп'),
        (r'лекци', 'Лекция'), (r'стажиров', 'Стажировка'), (r'ваканси', 'Вакансия'),
        (r'курс|образовательн\w*\s+программ', 'Образовательная программа'),
    ]
    # Prefer the actual heading over incidental mentions in benefit paragraphs.
    for segment in (text.split('\n', 1)[0], text):
        for pattern, label in patterns:
            if re.search(pattern, segment, re.I):
                return label
    return None


def publication_type(text: str, categories: list[str], *, vacancy=False, actionable=False) -> str:
    if not actionable:
        if RETROSPECTIVE.search(text):
            return 'retrospective'
        return 'informational_news' if it_signals(text) else 'irrelevant'
    if vacancy:
        return 'internship' if re.search(r'стажер|стажиров|intern|trainee', text, re.I) else 'vacancy'
    if 'contests' in categories:
        return 'actionable_competition'
    if re.search(r'стажиров|стажер|internship', text, re.I):
        return 'internship'
    if re.search(r'курс|образовательн\w*\s+программ|школ\w*\s+программирован', text, re.I):
        return 'educational_opportunity'
    return 'actionable_event'
