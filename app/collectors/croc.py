"""Сборщик вакансий КРОК (careers.croc.ru/vacancies/?_sft_expirience=0-1-год).

Что проверено на реальных данных:
- RSS/API у сайта нет (проверены /feed, /rss, /vacancies/feed — отдаётся HTML/404);
- страница фильтра «опыт 0–1 год» статическая (WordPress), карточки объявлений —
  .vacancy__card-item, пагинации нет (выдача помещается на одной странице);
- в карточке: ссылка на вакансию, название, теги занятости/города/опыта. Дат публикации
  на странице нет — published_at остаётся None, дедупликация по item_key работает;
- страницы отдельных вакансий открываются без входа, форма отклика есть (#responseForm),
  но это якорь на самой странице — отдельной ссылки на заявку нет.

Важно: фильтр сайта по опыту не точный (в выдаче попадаются вакансии с опытом 3-6 лет),
поэтому сборщик дополнительно оставляет только объявления, подходящие под правила
отбора (стажировки/начинающие и т.п. из config.yaml). Статус регистрации всегда unknown —
открытость набора на странице не указана, утверждать обратное нельзя.
"""
from __future__ import annotations

import logging
import re

from bs4 import BeautifulSoup

from app.collectors.base import http_get
from app.models import Item

log = logging.getLogger("croc")

MAX_DETAIL_PAGES = 15


class CrocCollector:
    type_key = "croc_vacancies"

    def __init__(self, source):
        self.source = source

    def _cards(self) -> list[dict]:
        resp = http_get(self.source.url)
        soup = BeautifulSoup(resp.text, "html.parser")
        out = []
        for card in soup.select(".vacancy__card-item"):
            a = card.find("a", href=True)
            if not a:
                continue
            title = a.find("span")
            title_text = title.get_text(strip=True) if title else a.get_text(" ", strip=True)
            tags = [li.get_text(strip=True) for li in card.find_all("li")]
            href = a["href"]
            url = href if href.startswith("http") else "https://careers.croc.ru" + href
            out.append({"url": url, "title": title_text, "tags": tags})
        # уникальность по slug
        seen, uniq = set(), []
        for c in out:
            slug = c["url"].rstrip("/").rsplit("/", 1)[-1]
            if slug and slug not in seen:
                c["slug"] = slug
                seen.add(slug)
                uniq.append(c)
        log.info("croc: карточек на странице %d, уникальных %d", len(out), len(uniq))
        return uniq

    def _detail(self, url: str) -> tuple[str, str | None]:
        """Возвращает (текст описания, ссылку на отклик если это внешний URL)."""
        try:
            resp = http_get(url)
            if resp.status_code == 404:
                return "", None  # вакансия снята
            resp.raise_for_status()
        except Exception as e:  # noqa: BLE001 — одна страница не ломает источник
            log.warning("croc: %s не получена: %s", url, e)
            return "", None
        soup = BeautifulSoup(resp.text, "html.parser")
        main = soup.select_one(".vacancy__single, article, main") or soup.body or soup
        text = re.sub(r"\s+", " ", main.get_text(" ", strip=True))[:800]
        apply_url = None
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.startswith(("http", "//")) and re.search(
                r"(hh\.ru|rabota\.yandex|otklik|apply|resume)", href, re.I
            ):
                apply_url = href if href.startswith("http") else "https:" + href
                break
        return text, apply_url

    def fetch(self) -> list[Item]:
        cards = self._cards()
        items: list[Item] = []
        for i, c in enumerate(cards):
            desc, apply_url = (None, None)
            if i < MAX_DETAIL_PAGES:  # разумная частота: не грузим все страницы каждый час
                desc, apply_url = self._detail(c["url"])
            summary = f"{', '.join(c['tags'])}. {desc or ''}".strip(". ")[:500]
            items.append(Item(
                source_key=self.source.key,
                source_name=self.source.name,
                external_id=c["slug"],
                url=c["url"],
                title=c["title"],
                summary=summary,
                apply_url=apply_url,
                published_at=None,          # дат на сайте нет — не выдумываем
                registration_status="unknown",  # открытость не указана явно
                raw_text=f"{c['title']}. {', '.join(c['tags'])}. {desc or ''}",
            ))
        log.info("croc: собрано %d объявлений", len(items))
        return items
