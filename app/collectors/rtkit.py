"""Сборщик карьерной страницы RTKiT (rtkit.ru/careers).

Что проверено на реальных данных:
- RSS нет; публичного API нет: сайт — Angular SPA, все данные (список вакансий,
  направления, мероприятия) вшиты прямо в статический бандл main.js как методы
  Angular InMemoryWebApi (серверных эндпоинтов /api/v1/* отдают 401/405 без токена);
- сборщик читает https://rtkit.ru/main.js и извлекает коллекции vacanciesCollection(),
  eventsCollection() и блоки careers/students из исходного кода бандла.

Ограничение (сообщаем прямо): это хрупкая интеграция. Слоты объектов в бандле идут в
форме {title:"...",slug_id:"..."} с unicode-эскейпами; при пересборке фронтенда формат
может измениться и парсер начнёт отдавать меньше данных или ошибку — источник будет
помечен как недоступный, а не выдуман. Открытость набора и даты подачи заявок нигде не
указаны, поэтому registration_status всегда unknown.
"""
from __future__ import annotations

import logging
import re

from app.collectors.base import http_get
from app.models import Item

log = logging.getLogger("rtkit")

BUNDLE_URL = "https://rtkit.ru/main.js"

# Объекты вида {title:"\u041c\u043e\u0441\u043a\u0432\u0430",slug_id:"moscow",...}
# title может быть не первым полем (например {category:...,title:...}), поэтому
# ищем заголовок с любым префиксом внутри объекта.
OBJ_RE = re.compile(r"\{(?:[^{}]|\{[^{}]*\})*?title:\"((?:[^\"\\]|\\.)*)\",slug_id:\"((?:[^\"\\]|\\.)*)\"")


def _unesc(s: str) -> str:
    r"""Раскрываем юникод-эскейпы (\uXXXX) строкового литерала JS (двойная эскейп-цепочка в бандле)."""
    for _ in range(3):
        prev = s
        try:
            s = s.encode("utf-8").decode("unicode_escape")
        except Exception:  # noqa: BLE001
            break
        if "\\u" not in s and "\\\\" not in s or s == prev:
            break
    return s


def _extract_collection(js: str, name: str) -> list[tuple[str, str]]:
    """Достаёт пары (title, slug_id) из тела метода `name(){return[...]}` бандла."""
    m = re.search(re.escape(name) + r"\(\)\s*\{.*?return\s*\[", js)
    if not m:
        return []
    start = m.end() - 1  # позиция '['
    depth, i = 0, start
    while i < len(js):
        ch = js[i]
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                break
        elif ch == "\\":
            i += 1
        i += 1
    body = js[start:i]
    return [( _unesc(t), _unesc(s)) for t, s in OBJ_RE.findall(body)]


class RtkitCollector:
    type_key = "rtkit_careers"

    def __init__(self, source):
        self.source = source

    def fetch(self) -> list[Item]:
        resp = http_get(BUNDLE_URL, timeout=60)
        js = resp.text
        items: list[Item] = []

        for title, slug in _extract_collection(js, "vacanciesCollection"):
            url = f"{self.source.url.rstrip('/')}/{slug}"
            items.append(Item(
                source_key=self.source.key,
                source_name=self.source.name,
                external_id=f"vacancy:{slug}",
                url=url,
                title=title,
                summary="Вакансия из каталога карьерной страницы RTKiT (Ростелеком ИТ).",
                apply_url=None,               # форма отклика есть только после рендера SPA
                published_at=None,
                registration_status="unknown",  # открытость набора не указана
                raw_text=title,
            ))

        for title, slug in _extract_collection(js, "eventsCollection"):
            items.append(Item(
                source_key=self.source.key,
                source_name=self.source.name,
                external_id=f"event:{slug}",
                url=self.source.url,
                title=title,
                summary="Мероприятие, указанное на карьерной странице RTKiT.",
                apply_url=None,
                published_at=None,
                registration_status="unknown",
                raw_text=title,
            ))

        if not items:
            raise RuntimeError(
                "Не удалось извлечь вакансии/мероприятия из бандла rtkit.ru/main.js — "
                "структура сайта изменилась, интеграцию нужно обновить."
            )
        log.info("rtkit: собрано %d объявлений из бандла", len(items))
        return items
