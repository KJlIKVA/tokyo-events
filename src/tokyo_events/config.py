"""Загрузка config.yaml и переменных окружения."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, model_validator

Rank = Literal["green", "orange", "purple"]
RANKS: tuple[Rank, ...] = ("green", "orange", "purple")

# решение пользователя (2026-10-13): только эта модель, другие запускать нельзя
ALLOWED_MODELS = ("gpt-5.6-terra",)


class Price(BaseModel):
    input: float
    cached_input: float | None = None
    output: float


Effort = Literal["minimal", "low", "medium", "high"]


class EnrichConfig(BaseModel):
    """Шаг 2: код скачивает статью, модель по её тексту пишет даты, цену, место и описание (без поиска)."""
    effort: Effort = "low"
    max_output_tokens: int = 6000
    article_chars: int = 5000  # сколько знаков статьи давать модели
    max_per_run: int = 20


class LLMConfig(BaseModel):
    model: str
    effort: Effort = "low"
    max_output_tokens: int = 16000
    batch_size: int = 20
    max_items_per_run: int = 120
    daily_token_limit: int = 1_000_000
    request_timeout_s: float = 180
    price_per_1m: Price | None = None
    enrich: EnrichConfig = EnrichConfig()

    @model_validator(mode="after")
    def _only_allowed_model(self) -> LLMConfig:
        if self.model not in ALLOWED_MODELS:
            raise ValueError(f"модель {self.model} не разрешена — только {', '.join(ALLOWED_MODELS)}")
        return self


class Filters(BaseModel):
    max_age_hours: int = 72
    first_run_hours: int = 12
    notify_ranks: list[Rank] = list(RANKS)
    silent_ranks: list[Rank] = ["green"]
    memory_days: int = 45
    memory_events: int = 150   # сколько последних событий из базы показывать модели для поиска дублей/обновлений
    far_days: int = 60         # начало позже — присылаем, только если билеты продаются или объявлен старт продаж
    seen_days: int = 60
    max_new_undated: int = 10


class Deals(BaseModel):
    """Скидки для поездок: присылать от min_pct; цвет по размеру скидки."""
    min_pct: int = 10
    orange_pct: int = 30
    purple_pct: int = 50


class HotelArea(BaseModel):
    name: str
    lat: float
    lon: float
    radius_km: float = 3.0   # API: 0.1–3.0
    onsen: bool = True       # только с горячими источниками
    meals: Literal["two_meals", "any"] | None = None  # None — как в hotels.meals (в городах обычно any)


class Stay(BaseModel):
    """Вариант заезда: день недели (или today — день в день) и сколько ночей."""
    checkin: Literal["today", "mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    nights: int = 1


class Hotels(BaseModel):
    """Цены отелей и рёканов через API 楽天トラベル: скидки к обычной цене. Пороги и цвета — как у deals."""
    enabled: bool = True
    run_at_hours: list[int] = [9, 15, 19]   # по Токио; в другие запуски цены не проверяются
    origin: str = ""              # сайт, указанный в приложении Rakuten (заголовок Origin)
    adults: int = 2
    meals: Literal["two_meals", "any"] = "two_meals"
    stays: list[Stay] = [Stay(checkin="fri", nights=1), Stay(checkin="sat", nights=1),
                         Stay(checkin="fri", nights=2), Stay(checkin="today", nights=1)]
    weeks_ahead: int = 4          # цены на столько недель вперёд — из них «обычная цена»
    alert_weeks: int = 2          # о скидках сообщать на ближайшие N недель («сегодня» — только сегодня)
    soon_days: int = 1            # заезд не позже чем через столько дней (1 — сегодня и завтра)…
    soon_min_pct: int = 60        # …только при такой скидке: иначе не успеть собраться
    min_compare: int = 2          # сколько других недель нужно, чтобы считать «обычную цену»
    history_days: int = 21
    min_saving_yen: int = 3000
    min_review: float = 3.5
    max_vs_min_charge: float = 1.3  # цена не дороже 1.3× самой низкой цены отеля (hotelMinCharge × гостей)
    max_alerts_per_run: int = 6
    max_per_area: int = 1         # с одного места за запуск — только лучшая скидка
    max_pages: int = 2            # страниц по 30 отелей на место и дату (в городах отелей сотни: берём дешёвые)
    areas: list[HotelArea] = []


class Artist(BaseModel):
    name: str
    aliases: list[str]
    about: str = ""


class Source(BaseModel):
    id: str
    kind: Literal["rss", "google_news", "page"]
    url: str | None = None
    q: str | None = None
    lang: Literal["ja", "en"] = "ja"
    link_pattern: str | None = None
    artists_only: bool = False
    artist: str | None = None

    @model_validator(mode="after")
    def _check(self) -> Source:
        if self.kind == "google_news" and not self.q:
            raise ValueError(f"источник {self.id}: для google_news нужен q")
        if self.kind in ("rss", "page") and not self.url:
            raise ValueError(f"источник {self.id}: нужен url")
        if self.kind == "page" and not self.link_pattern:
            raise ValueError(f"источник {self.id}: для page нужен link_pattern")
        return self


class Config(BaseModel):
    timezone: str = "Asia/Tokyo"
    llm: LLMConfig
    filters: Filters = Filters()
    deals: Deals = Deals()
    hotels: Hotels = Hotels()
    area: str
    artists: list[Artist]
    sources: list[Source]

    @model_validator(mode="after")
    def _check(self) -> Config:
        names = {a.name for a in self.artists}
        ids = [s.id for s in self.sources]
        if len(ids) != len(set(ids)):
            raise ValueError("id источников должны быть уникальны")
        for s in self.sources:
            if s.artist and s.artist not in names:
                raise ValueError(f"источник {s.id}: артиста {s.artist} нет в artists")
        return self


def load_config(path: str | Path = "config.yaml") -> Config:
    with open(path, encoding="utf-8") as f:
        return Config.model_validate(yaml.safe_load(f))


def load_dotenv(path: str | Path = ".env") -> None:
    """Простейший .env для локального запуска: KEY=VALUE, уже заданные переменные не трогаем."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        if value:
            os.environ.setdefault(key.strip(), value)


def env(name: str, required: bool = False) -> str | None:
    value = os.environ.get(name) or None
    if required and not value:
        raise RuntimeError(f"не задана переменная окружения {name}")
    return value
