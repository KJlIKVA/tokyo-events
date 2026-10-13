"""Шаг 2 — подробности события. Без поиска в интернете силами ИИ:

1. код сам скачивает статью (ссылку Google Новостей раскрывает в настоящий адрес) и достаёт текст;
2. модель только читает этот текст и пишет уведомление: название, даты, цена, место, описание;
3. ссылки код находит сам: официальный сайт — по подписи «公式サイト» в статье, иначе сайт площадки
   из Wikidata; Google Maps — по названию и адресу.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ValidationError

from .article import Article, fetch_article, official_candidates
from .classify import LLMError, Transport, Verdict, check_budget, describe_item, load_prompt, strict_object
from .config import Config
from .sources import USER_AGENT, Item
from .state import Notified
from .venue import VenueSites

log = logging.getLogger(__name__)

MAX_VENUES = 8


class Venue(BaseModel):
    name: str
    address: str
    dates_ru: str
    area_ru: str = ""      # район и город текстом: «Уэно, Тайто, Токио»
    on_map: bool = True    # конкретное место, которое находится на карте (не линия поезда, не целый город)


class Details(BaseModel):
    # заполняет модель — по тексту статьи
    found: bool
    title_ru: str
    dates_ru: str
    date_precision: Literal["exact", "approximate", "year_only", "unknown"]
    end_date: str
    price_ru: str
    is_free: bool
    venues: list[Venue]
    summary_ru: str
    # заполняет код — модель к ссылкам не прикасается
    official_url: str = ""
    venue_url: str = ""
    source_url: str = ""

    @property
    def has_dates(self) -> bool:
        """Достаточно ли дат для уведомления: «2027 год» — слишком размыто, ждём хотя бы месяц."""
        return self.date_precision in ("exact", "approximate") and bool(self.dates_ru.strip())

    def ended(self, today: date) -> bool:
        try:
            return date.fromisoformat(self.end_date) < today
        except ValueError:
            return False


def schema() -> dict[str, Any]:
    """Только то, что модель берёт из текста. Ссылок в схеме нет."""
    s = {"type": "string"}
    venue = strict_object({"name": s, "address": s, "area_ru": s, "on_map": {"type": "boolean"}, "dates_ru": s})
    return strict_object({
        "found": {"type": "boolean"},
        "title_ru": s,
        "dates_ru": s,
        "date_precision": {"type": "string", "enum": ["exact", "approximate", "year_only", "unknown"]},
        "end_date": s,
        "price_ru": s,
        "is_free": {"type": "boolean"},
        "venues": {"type": "array", "items": venue},
        "summary_ru": s,
    })


def maps_url(venue: Venue) -> str:
    """Ссылка Google Maps по названию и адресу (официальный формат, ключ API не нужен)."""
    query = " ".join(p for p in (venue.name.strip(), venue.address.strip()) if p)
    return "https://www.google.com/maps/search/?" + urllib.parse.urlencode({"api": "1", "query": query})


def url_alive(url: str, client: httpx.Client) -> bool:
    """Страница должна открываться. 403/405 — сайт есть, просто не любит роботов."""
    if not url.startswith(("http://", "https://")):
        return False
    try:
        r = client.get(url, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
    except httpx.HTTPError:
        return False
    return r.status_code < 400 or r.status_code in (403, 405)


@dataclass
class Enricher:
    cfg: Config
    transport: Transport
    client: httpx.Client | None = None  # None — без сети (тесты): статьи не качаем, ссылки не ищем
    venues: VenueSites | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if self.client is not None:
            self.venues = VenueSites(self.client)

    def build_prompt(self, item: Item, v: Verdict, previous: Notified | None, now: datetime,
                     article: Article | None) -> str:
        artist_line = f"Артист: {v.artist}." if v.artist else ""
        update_line = (f"Это ОБНОВЛЕНИЕ уже отправленного события: «{previous.title}» "
                       f"(было: когда — {previous.when or '?'}, где — {previous.where or '?'}). "
                       "В summary_ru напиши, что изменилось.") if previous else ""
        if article and article.text:
            text = "\n".join(x for x in (article.title, article.description, article.text) if x)
        else:
            text = "(текст статьи недоступен — есть только заголовок)"
        return (load_prompt("enrich.txt").replace("{today}", f"{now:%Y-%m-%d}")
                .replace("{item}", describe_item("—", item, now))
                .replace("{category}", v.category)
                .replace("{artist_line}", artist_line)
                .replace("{update_line}", update_line)
                .replace("{article}", text))

    def enrich(self, item: Item, v: Verdict, previous: Notified | None, now: datetime,
               tokens_left: int) -> tuple[Details, int]:
        ec = self.cfg.llm.enrich
        article = fetch_article(item.link, self.client, ec.article_chars) if self.client is not None else None
        if self.client is not None and article is None:
            log.info("   статья не скачалась — у модели только заголовок")
        prompt = self.build_prompt(item, v, previous, now, article)
        check_budget(prompt, ec.max_output_tokens, tokens_left)
        text, tokens = self.transport(prompt, schema(), effort=ec.effort, max_output_tokens=ec.max_output_tokens)
        try:
            d = Details.model_validate(json.loads(text))
        except (json.JSONDecodeError, ValidationError) as e:
            raise LLMError(f"подробности: ответ модели не прошёл проверку: {str(e)[:300]}") from e
        d.venues = [x for x in d.venues if x.name.strip()][:MAX_VENUES]
        d.source_url = article.url if article else item.link
        self._find_links(d, item, article)
        return d, tokens

    def _find_links(self, d: Details, item: Item, article: Article | None) -> None:
        """Ссылки — только кодом: официальный сайт события, иначе сайт площадки."""
        if self.client is None:
            return
        if item.artist:
            # новость с официального сайта артиста — она сама и есть официальная страница
            d.official_url = item.link
            return
        for url in (official_candidates(article, item.title) if article else [])[:4]:
            if url_alive(url, self.client):
                d.official_url = url
                return
        if d.venues and self.venues is not None:
            site = self.venues.lookup(d.venues[0].name)
            if site and url_alive(site, self.client):
                d.venue_url = site
