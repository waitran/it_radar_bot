"""Personal relevance only: never changes the global quality rules."""
import re

CATEGORIES = {'internships': 'Стажировки', 'contests': 'Хакатоны и соревнования',
              'events': 'Митапы, лекции и конференции', 'career': 'Карьерные мероприятия'}
TOPICS = {'python': 'Python', 'backend': 'Backend', 'frontend': 'Frontend', 'ai': 'AI/ML',
          'data': 'Data Science', 'security': 'Cybersecurity', 'devops': 'DevOps', 'mobile': 'Mobile Development'}
PATTERNS = {'python': r'\bpython\b', 'backend': r'\bbackend\b|бэкенд|бекенд',
            'frontend': r'\bfrontend\b|фронтенд', 'ai': r'\bai\b|\bml\b|machine learning|машинн\w* обуч|искусственн\w* интеллект',
            'data': r'data science|data scientist|наук\w* о данных',
            'security': r'cybersecurity|кибербезопас|информационн\w* безопас|\bctf\b',
            'devops': r'\bdevops\b', 'mobile': r'\bandroid\b|\bios\b|мобильн\w* разработ'}
DEFAULTS = {'categories': list(CATEGORIES), 'topics': [], 'format': 'any', 'city': '', 'timezone': 'Europe/Moscow'}


def characteristics(item):
    text = item.title + '\n' + (item.raw_text or item.summary)
    topics = [key for key, pattern in PATTERNS.items() if re.search(pattern, text, re.I)]
    online = bool(re.search(r'\bонлайн\b|\bonline\b|дистанционн', text, re.I))
    offline = bool(re.search(r'\bофлайн\b|\boffline\b|очное участие', text, re.I))
    mode = 'hybrid' if online and offline else 'online' if online else 'offline' if offline else 'unknown'
    city = re.search(r'(?:город\s*[:—-]|место проведения\s*[:—-]|\bг\.)\s*([А-ЯЁA-Z][а-яёa-z]+(?:[ -][А-ЯЁA-Z][а-яёa-z]+)?)', text)
    return topics, mode, city[1] if city else ''


def matches(preferences, item):
    topics, mode, city = characteristics(item)
    return (bool(set(preferences['categories']) & set(item.categories))
            and (not preferences['topics'] or bool(set(preferences['topics']) & set(topics)))
            and (preferences['format'] == 'any' or mode in (preferences['format'], 'hybrid'))
            and (not preferences['city'] or preferences['city'].casefold() == city.casefold()))
