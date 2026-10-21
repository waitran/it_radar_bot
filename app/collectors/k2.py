"""Сборщик вакансий K2 Cybersecurity (careers.cybersecurity.k2.tech/vacancy).

Что проверено на реальных данных:
- RSS/API у сайта нет;
- каталог вакансий рендерится JS-блоком Tilda (store.tildaapi.com), прямой запрос к его
  JSON-эндпоинту отклоняется ("Wrong storepartuid") — интеграция по API невозможна;
- рабочая схема: sitemap.xml сайта + статические страницы вакансий (отдаются без JS).

Сборщик читает sitemap, берёт страницы-вакансии (исключая корень/служебные /pageNNN),
загружает каждую страницу и извлекает заголовок (<title>), текст описания и ссылку на
форму отклика. lastmod из sitemap используется как дата публикации.
"""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup

from app.collectors.base import http_get
from app.models import Item

log = logging.getLogger("k2")

NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
MAX_VACANCY_PAGES = 60


class K2Collector:
    type_key = "k2_vacancy"

    def __init__(self, source):
        self.source = source

    def _sitemap_allowed(self) -> bool:
        """Уважаем robots.txt сайта."""
        host = self.source.url.split("//", 1)[1].split("/", 1)[0]
        try:
            rp = RobotFileParser()
            rp.set_url(f"https://{host}/robots.txt")
            rp.read()
            return rp.can_fetch("*", f"https://{host}/sitemap.xml")
        except Exception as e:  # noqa: BLE001
            log.warning("K2: robots.txt недоступен (%s) — продолжаем с осторожностью", e)
            return True

    def _vacancy_urls(self) -> list[tuple[str, str | None]]:
        host = self.source.url.split("//", 1)[1].split("/")[0]
        sm_url = f"https://{host}/sitemap.xml"
        resp = http_get(sm_url)
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
        out: list[tuple[str, str | None]] = []
        for u in root.iter(NS + "url"):
            loc = (u.findtext(NS + "loc") or "").strip()
            lastmod = (u.findtext(NS + "lastmod") or "").strip() or None
            if not loc or f"//{host}" not in loc:
                continue
            path = loc.split(host, 1)[1].strip("/")
            # исключаем корень, саму страницу каталога и служебные страницы Tilda
            if not path or path == "vacancy" or re.fullmatch(r"page\d+\.html", path):
                continue
            if path.endswith((".jpg", ".png", ".css", ".js")):
                continue
            out.append((loc, lastmod))
        return out[:MAX_VACANCY_PAGES]

    @staticmethod
    def _parse_dt(s: str | None) -> datetime | None:
        if not s:
            return None
        try:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            return None

    def fetch(self) -> list[Item]:
        if not self._sitemap_allowed():
            raise RuntimeError("robots.txt запрещает чтение sitemap — источник недоступен для автосбора")
        pairs = self._vacancy_urls()
        items: list[Item] = []
        for url, lastmod in pairs:
            try:
                resp = http_get(url)
                if resp.status_code == 404:
                    continue  # объявление снято — не присылаем
                resp.raise_for_status()
            except Exception as e:  # noqa: BLE001 — одна упавшая страница не ломает источник
                log.warning("K2: %s не получена: %s", url, e)
                continue
            soup = BeautifulSoup(resp.text, "html.parser")
            title = (soup.title.get_text(strip=True) if soup.title else "") or url.rsplit("/", 1)[-1]
            main = soup.select_one("#allrecords") or soup.body or soup
            text = re.sub(r"\s+", " ", main.get_text(" ", strip=True))
            apply_url = None
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if re.search(r"(hh\.ru|rabota\.yandex|otklik|zayavka|apply)", href, re.I):
                    apply_url = href
                    break
            desc = text[:500]
            slug = url.rstrip("/").rsplit("/", 1)[-1]
            items.append(Item(
                source_key=self.source.key,
                source_name=self.source.name,
                external_id=slug,
                url=url,
                title=title,
                summary=desc,
                apply_url=apply_url,
                published_at=self._parse_dt(lastmod),
                raw_text=f"{title}. {desc}",
            ))
        log.info("K2: собрано %d страниц вакансий", len(items))
        return items
