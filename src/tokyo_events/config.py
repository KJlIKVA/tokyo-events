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
    """Шаг 2: по каждому отобранному событию модель ищет в интернете даты, цену, место и официальный сайт."""
    effort: Effort = "low"
    max_output_tokens: int = 12000
    search_context_size: Literal["low", "medium", "high"] = "medium"
    reserve_search_tokens: int = 40000  # сколько входных токенов закладывать в бюджет на результаты поиска
    max_per_run: int = 20
    price_per_search: float = 0.01      # USD за один поиск — только для отчёта о расходах в логе


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
    seen_days: int = 60
    max_new_undated: int = 10


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
