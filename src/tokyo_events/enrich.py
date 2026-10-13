"""Шаг 2 — подробности события через поиск в интернете: даты, цена, место (для Google Maps), официальный сайт."""

from __future__ import annotations

import json
import logging
import urllib.parse
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ValidationError

from .classify import LLMError, Transport, Verdict, check_budget, describe_item, load_prompt, strict_object
from .config import Config
from .sources import USER_AGENT, Item
from .state import Notified

log = logging.getLogger(__name__)

MAX_VENUES = 8


class Venue(BaseModel):
    name: str
    address: str
    dates_ru: str


class Details(BaseModel):
    found: bool
    title_ru: str
    dates_ru: str
    date_precision: Literal["exact", "approximate", "year_only", "unknown"]
    end_date: str
    price_ru: str
    is_free: bool
    venues: list[Venue]
    official_url: str
    summary_ru: str

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
    s = {"type": "string"}
    venue = strict_object({"name": s, "address": s, "dates_ru": s})
    return strict_object({
        "found": {"type": "boolean"},
        "title_ru": s,
        "dates_ru": s,
        "date_precision": {"type": "string", "enum": ["exact", "approximate", "year_only", "unknown"]},
        "end_date": s,
        "price_ru": s,
        "is_free": {"type": "boolean"},
        "venues": {"type": "array", "items": venue},
        "official_url": s,
        "summary_ru": s,
    })


def maps_url(venue: Venue) -> str:
    """Ссылка Google Maps по названию и адресу (официальный формат, ключ API не нужен)."""
    query = " ".join(p for p in (venue.name.strip(), venue.address.strip()) if p)
    return "https://www.google.com/maps/search/?" + urllib.parse.urlencode({"api": "1", "query": query})


def url_alive(url: str, client: httpx.Client) -> bool:
    """Проверка, что модель не придумала адрес сайта: страница должна открываться.
    403/405 — сайт есть, просто не любит роботов."""
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
    client: httpx.Client | None = None  # для проверки официального сайта; None — не проверять

    def build_prompt(self, item: Item, v: Verdict, previous: Notified | None, now: datetime) -> str:
        artist_line = f"Артист: {v.artist}." if v.artist else ""
        update_line = (f"Это ОБНОВЛЕНИЕ уже отправленного события: «{previous.title}» "
                       f"(было: когда — {previous.when or '?'}, где — {previous.where or '?'}). "
                       "В summary_ru напиши, что изменилось.") if previous else ""
        return (load_prompt("enrich.txt").replace("{today}", f"{now:%Y-%m-%d}")
                .replace("{item}", describe_item("—", item, now) + f"\nссылка на новость: {item.link}")
                .replace("{category}", v.category)
                .replace("{artist_line}", artist_line)
                .replace("{update_line}", update_line))

    def enrich(self, item: Item, v: Verdict, previous: Notified | None, now: datetime,
               tokens_left: int) -> tuple[Details, int]:
        ec = self.cfg.llm.enrich
        prompt = self.build_prompt(item, v, previous, now)
        check_budget(prompt, ec.max_output_tokens, tokens_left, extra=ec.reserve_search_tokens)
        text, tokens = self.transport(prompt, schema(), effort=ec.effort, max_output_tokens=ec.max_output_tokens,
                                      web_search=True)
        try:
            d = Details.model_validate(json.loads(text))
        except (json.JSONDecodeError, ValidationError) as e:
            raise LLMError(f"подробности: ответ модели не прошёл проверку: {str(e)[:300]}") from e
        d.venues = [x for x in d.venues if x.name.strip()][:MAX_VENUES]
        d.official_url = d.official_url.strip()
        if d.official_url and self.client is not None and not url_alive(d.official_url, self.client):
            log.warning("официальный сайт %s не открывается — не показываем", d.official_url)
            d.official_url = ""
        return d, tokens
