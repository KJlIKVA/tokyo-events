"""Шаг 1 — оценка находок моделью по заголовкам: событие или нет, ранг, дубль или обновление."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from importlib import resources
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ValidationError

from .config import Config, LLMConfig, env
from .events_db import Event
from .sources import Item

log = logging.getLogger(__name__)

CATEGORIES = ["concert", "exhibition", "festival", "opening", "big_event", "other"]


class Verdict(BaseModel):
    id: str
    relevant: bool
    rank: Literal["green", "orange", "purple", "none"]
    category: Literal["concert", "exhibition", "festival", "opening", "big_event", "other"]
    artist: str
    duplicate_of: str
    update_of: str
    title_ru: str
    reason: str


class Batch(BaseModel):
    items: list[Verdict]


def strict_object(props: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "additionalProperties": False, "properties": props, "required": list(props)}


def schema(artist_names: list[str]) -> dict[str, Any]:
    s = {"type": "string"}
    verdict = strict_object({
        "id": s,
        "relevant": {"type": "boolean"},
        "rank": {"type": "string", "enum": ["green", "orange", "purple", "none"]},
        "category": {"type": "string", "enum": CATEGORIES},
        "artist": {"type": "string", "enum": ["", *artist_names]},
        "duplicate_of": s,
        "update_of": s,
        "title_ru": s,
        "reason": s,
    })
    return strict_object({"items": {"type": "array", "items": verdict}})


class LLMError(RuntimeError):
    pass


class BudgetExceeded(LLMError):
    pass


class Transport(Protocol):
    def __call__(self, prompt: str, schema: dict[str, Any], *, effort: str, max_output_tokens: int) -> tuple[str, int]:
        """(JSON-текст ответа, всего токенов). Бросает LLMError."""


class OpenAITransport:
    """OpenAI Responses API со Structured Outputs. Без инструментов: модель не ищет в интернете
    (решение пользователя — поиск слишком дорогой; ссылки находит код)."""

    def __init__(self, cfg: LLMConfig) -> None:
        from openai import OpenAI

        self.cfg = cfg
        self.client = OpenAI(api_key=env("OPENAI_API_KEY", required=True), timeout=cfg.request_timeout_s,
                             max_retries=2)

    def __call__(self, prompt: str, schema: dict[str, Any], *, effort: str, max_output_tokens: int) -> tuple[str, int]:
        import openai

        try:
            resp = self.client.responses.create(
                model=self.cfg.model,
                input=prompt,
                reasoning={"effort": effort},
                max_output_tokens=max_output_tokens,
                store=False,
                text={"format": {"type": "json_schema", "name": "tokyo_events", "schema": schema, "strict": True}},
            )
        except openai.OpenAIError as e:
            raise LLMError(f"{type(e).__name__}: {str(e)[:300]}") from e
        u = resp.usage
        inp = getattr(u, "input_tokens", 0) or 0
        cached = getattr(getattr(u, "input_tokens_details", None), "cached_tokens", 0) or 0
        out = getattr(u, "output_tokens", 0) or 0
        if self.cfg.price_per_1m:
            p = self.cfg.price_per_1m
            cost = ((inp - cached) * p.input + cached * (p.cached_input or p.input) + out * p.output) / 1e6
            log.info("модель: вход %d (кэш %d), выход %d токенов ≈ $%.4f", inp, cached, out, cost)
        if resp.status != "completed":
            raise LLMError(f"ответ не завершён: {resp.status} {getattr(resp, 'incomplete_details', None)}")
        return resp.output_text, inp + out


def check_budget(prompt: str, max_output_tokens: int, tokens_left: int) -> None:
    """Верхняя оценка вызова: вход ≈ байты/2 + максимум выхода. Не влезает — не вызываем."""
    reserve = len(prompt.encode("utf-8")) // 2 + max_output_tokens
    if reserve > tokens_left:
        raise BudgetExceeded(f"дневной лимит токенов: осталось {tokens_left:,}, вызов может занять до {reserve:,}")


def load_prompt(name: str) -> str:
    return resources.files("tokyo_events").joinpath(f"prompts/{name}").read_text(encoding="utf-8")


def describe_item(item_id: str, it: Item, now: datetime) -> str:
    lines = [f"id: {item_id}", f"заголовок: {it.title}"]
    if it.summary and it.summary != it.title:
        lines.append(f"текст: {it.summary}")
    if it.published:
        lines.append(f"опубликовано: {it.published.astimezone(now.tzinfo):%Y-%m-%d %H:%M}")
    if it.artist:
        lines.append(f"официальный источник: {it.artist}")
    return "\n".join(lines)


STATUS_MARK = {"cancelled": " | ОТМЕНЕНО", "postponed": " | ПЕРЕНЕСЕНО"}


def memory_lines(memory: list[Event]) -> str:
    def price(e: Event) -> str:
        return e.price_ru or ("бесплатно" if e.is_free else "?")

    return "\n".join(f"{e.id} | {e.rank} | {e.title} | {e.dates_ru or '?'} | {e.where or '?'} | {price(e)}"
                     f"{STATUS_MARK.get(e.status, '')}" for e in memory) or "(пока пусто)"


@dataclass
class Classifier:
    cfg: Config
    transport: Transport

    def build_prompt(self, items: list[tuple[str, Item]], memory: list[Event], now: datetime) -> str:
        artists = "\n".join(f"- {a.name}: {a.about}" for a in self.cfg.artists)
        # replace, а не format: в шаблоне есть фигурные скобки JSON
        return (load_prompt("classify.txt").replace("{today}", f"{now:%Y-%m-%d}")
                .replace("{area}", self.cfg.area)
                .replace("{artists}", artists)
                .replace("{memory}", memory_lines(memory))
                .replace("{items}", "\n\n".join(describe_item(i, it, now) for i, it in items)))

    def classify(self, items: list[tuple[str, Item]], memory: list[Event], now: datetime,
                 tokens_left: int) -> tuple[list[Verdict], int]:
        """(вердикты по порядку items, потрачено токенов)."""
        prompt = self.build_prompt(items, memory, now)
        check_budget(prompt, self.cfg.llm.max_output_tokens, tokens_left)
        text, tokens = self.transport(prompt, schema([a.name for a in self.cfg.artists]),
                                      effort=self.cfg.llm.effort, max_output_tokens=self.cfg.llm.max_output_tokens)
        try:
            batch = Batch.model_validate(json.loads(text))
        except (json.JSONDecodeError, ValidationError) as e:
            raise LLMError(f"ответ модели не прошёл проверку: {str(e)[:300]}") from e
        by_id = {v.id.strip(): v for v in batch.items}
        missing = [i for i, _ in items if i not in by_id]
        if missing:
            log.warning("модель не вернула вердикт для %s — разберём в следующий раз", missing)
        return [by_id[i] for i, _ in items if i in by_id], tokens
