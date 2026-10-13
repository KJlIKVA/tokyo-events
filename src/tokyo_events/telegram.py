"""Уведомления в Telegram через Bot API."""

from __future__ import annotations

import html
import logging
import time
import urllib.parse

import httpx

from .classify import Verdict
from .sources import Item

log = logging.getLogger(__name__)

CIRCLES = {"green": "🟢", "orange": "🟠", "purple": "🟣"}
CATEGORY_TAGS = {"concert": "#концерт", "exhibition": "#выставка", "festival": "#фестиваль",
                 "opening": "#открытие", "big_event": "#грандиозное", "other": ""}


class TelegramError(RuntimeError):
    pass


def source_name(item: Item) -> str:
    if item.outlet:
        return item.outlet
    host = urllib.parse.urlparse(item.link).hostname or ""
    return "Google Новости" if host == "news.google.com" else host.removeprefix("www.")


def format_message(v: Verdict, item: Item) -> str:
    e = html.escape
    lines = [f"{CIRCLES[v.rank]} <b>{e(v.title_ru or item.title)}</b>"]
    if v.when_ru:
        lines.append(f"📅 {e(v.when_ru)}")
    if v.where_ru:
        lines.append(f"📍 {e(v.where_ru)}")
    if v.summary_ru:
        lines += ["", e(v.summary_ru)]
    tags = " ".join(t for t in (CATEGORY_TAGS.get(v.category, ""),
                                f"#{v.artist.replace(' ', '_')}" if v.artist else "") if t)
    link = f'<a href="{e(item.link, quote=True)}">{e(source_name(item))}</a>'
    lines += ["", f"{link}  {tags}" if tags else link]
    return "\n".join(lines)


def parse_chat_ids(raw: str) -> list[str]:
    """TELEGRAM_CHAT_ID: один id или несколько через запятую (несколько аккаунтов)."""
    return [c.strip() for c in raw.split(",") if c.strip()]


class Telegram:
    """Каждое уведомление уходит во все чаты из chat_ids."""

    def __init__(self, token: str, chat_ids: list[str], client: httpx.Client | None = None) -> None:
        if not chat_ids:
            raise ValueError("нужен хотя бы один chat id")
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"
        self.chat_ids = chat_ids
        self.client = client or httpx.Client(timeout=30)

    def send(self, text: str, silent: bool = False) -> list[str]:
        """Отправить во все чаты. Не дошло никуда — TelegramError (повторим в следующий запуск).
        Не дошло в часть чатов — список проблем: повторять нельзя, остальные получили бы дубль."""
        problems = []
        for chat_id in self.chat_ids:
            try:
                self._send_one(chat_id, text, silent)
            except TelegramError as e:
                problems.append(f"чат {_mask(chat_id)}: {e}")
        if len(problems) == len(self.chat_ids):
            raise TelegramError("; ".join(problems))
        return problems

    def _send_one(self, chat_id: str, text: str, silent: bool) -> None:
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                   "disable_notification": silent, "link_preview_options": {"is_disabled": True}}
        for attempt in range(3):
            try:
                r = self.client.post(self._url, json=payload)
                data = r.json()
            except (httpx.HTTPError, ValueError) as e:
                # текст исключения может содержать URL с токеном — пишем только тип
                raise TelegramError(f"Telegram не ответил ({type(e).__name__})") from None
            if data.get("ok"):
                return
            retry_after = (data.get("parameters") or {}).get("retry_after")
            if data.get("error_code") == 429 and retry_after and attempt < 2:
                time.sleep(float(retry_after))
                continue
            raise TelegramError(f"Telegram: {data.get('error_code')} {data.get('description', '')}")


def _mask(chat_id: str) -> str:
    # логи публичного репозитория видны всем — chat id целиком не пишем
    return "…" + chat_id[-3:]


class PrintTelegram:
    """Dry-run: печатает уведомления вместо отправки."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, bool]] = []

    def send(self, text: str, silent: bool = False) -> list[str]:
        self.sent.append((text, silent))
        print(("🔕 " if silent else "") + text + "\n" + "─" * 40)
        return []
