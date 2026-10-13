from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tokyo_events.classify import Classifier, LLMError
from tokyo_events.config import Config, load_config
from tokyo_events.sources import Item
from tokyo_events.telegram import PrintTelegram, TelegramError

NOW = datetime(2026, 10, 13, 3, 0, tzinfo=UTC)  # 12:00 JST


@pytest.fixture
def cfg() -> Config:
    c = load_config("config.yaml")
    c.llm.batch_size = 5
    return c


def item(title: str, source: str = "gn_tokyo_festivals", hours_ago: float | None = 1, link: str | None = None,
         summary: str = "", artist: str | None = None) -> Item:
    return Item(source=source, title=title, link=link or f"https://example.com/{abs(hash(title))}",
                summary=summary, published=None if hours_ago is None else NOW - timedelta(hours=hours_ago),
                artist=artist)


class FakeFetcher:
    def __init__(self, by_source: dict[str, list[Item]] | None = None) -> None:
        self.by_source = by_source or {}
        self.fail: set[str] = set()

    def __call__(self, source: Any) -> list[Item]:
        if source.id in self.fail:
            raise ConnectionError("нет сети")
        return list(self.by_source.get(source.id, []))


def verdict(item_id: str, rank: str = "none", category: str = "other", artist: str = "", duplicate_of: str = "",
            title_ru: str = "") -> dict[str, Any]:
    return {"id": item_id, "relevant": rank != "none", "rank": rank, "category": category, "artist": artist,
            "duplicate_of": duplicate_of, "title_ru": title_ru, "when_ru": "", "where_ru": "",
            "summary_ru": "", "reason": "тест"}


class FakeTransport:
    """Отвечает по заголовкам: rules[заголовок] = dict полей вердикта (без id). Неизвестное — rank none."""

    def __init__(self, rules: dict[str, dict[str, Any]] | None = None, tokens: int = 1000) -> None:
        self.rules = rules or {}
        self.tokens = tokens
        self.prompts: list[str] = []
        self.fail = False

    def __call__(self, prompt: str, schema: dict[str, Any]) -> tuple[str, int]:
        self.prompts.append(prompt)
        if self.fail:
            raise LLMError("сбой")
        out = []
        for item_id, title in re.findall(r"^id: (n\d+)\nзаголовок: (.*)$", prompt, flags=re.M):
            out.append(verdict(item_id, **self.rules.get(title, {})))
        return json.dumps({"items": out}, ensure_ascii=False), self.tokens


class FailingTelegram(PrintTelegram):
    def send(self, text: str, silent: bool = False) -> list[str]:
        raise TelegramError("Telegram: 500 упал")


class PartlyFailingTelegram(PrintTelegram):
    def send(self, text: str, silent: bool = False) -> list[str]:
        super().send(text, silent)
        return ["чат …789: Telegram: 403 Forbidden: bot was blocked by the user"]


@pytest.fixture
def transport() -> FakeTransport:
    return FakeTransport()


@pytest.fixture
def classifier(cfg: Config, transport: FakeTransport) -> Classifier:
    return Classifier(cfg, transport)


@pytest.fixture
def sender() -> PrintTelegram:
    return PrintTelegram()
