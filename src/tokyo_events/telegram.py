"""Уведомления в Telegram через Bot API."""

from __future__ import annotations

import html
import logging
import time
import urllib.parse
from typing import Any

import httpx

from .classify import Verdict
from .enrich import Details, maps_url
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


def _a(url: str, text: str) -> str:
    return f'<a href="{html.escape(url, quote=True)}">{html.escape(text)}</a>'


def format_message(v: Verdict, item: Item, d: Details, update: bool = False) -> str:
    """Событие: ранг и название, даты, цена, место со ссылкой на Google Maps, официальный сайт, описание."""
    e = html.escape
    title = e(d.title_ru or v.title_ru or item.title)
    lines = [f"{CIRCLES[v.rank]} {'🔄 Обновление: ' if update else ''}<b>{title}</b>"]
    lines.append(f"📅 {e(d.dates_ru)}")
    if d.is_free:
        lines.append("💴 Бесплатно" + (f" ({e(d.price_ru)})" if d.price_ru and "бесплат" not in d.price_ru.lower()
                                      else ""))
    else:
        lines.append(f"💴 {e(d.price_ru)}" if d.price_ru else "💴 Цена пока не объявлена")
    for venue in d.venues:
        place = _a(maps_url(venue), venue.name)
        lines.append(f"🗺 {place}" + (f" — {e(venue.dates_ru)}" if venue.dates_ru else ""))
    if d.official_url:
        lines.append(f"🌐 {_a(d.official_url, 'Официальный сайт')}")
    else:
        # официальный сайт не нашёлся — хоть что-то, откуда узнать подробности
        lines.append(f"🔗 {_a(item.link, f'Подробнее: {source_name(item)}')}")
    if d.summary_ru:
        lines += ["", e(d.summary_ru)]
    tags = " ".join(t for t in (CATEGORY_TAGS.get(v.category, ""),
                                f"#{v.artist.replace(' ', '_')}" if v.artist else "") if t)
    if tags:
        lines += ["", tags]
    return "\n".join(lines)


def parse_chat_ids(raw: str) -> list[str]:
    """TELEGRAM_CHAT_ID: один id или несколько через запятую (несколько аккаунтов)."""
    return [c.strip() for c in raw.split(",") if c.strip()]


class Telegram:
    """Каждое уведомление уходит во все чаты из chat_ids."""

    def __init__(self, token: str, chat_ids: list[str], client: httpx.Client | None = None) -> None:
        if not chat_ids:
            raise ValueError("нужен хотя бы один chat id")
        self._base = f"https://api.telegram.org/bot{token}"
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

    def _send_one(self, chat_id: str, text: str, silent: bool) -> int:
        """message_id отправленного сообщения."""
        result = self._call("sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                                            "disable_notification": silent,
                                            "link_preview_options": {"is_disabled": True}})
        return int(result["message_id"])

    def _call(self, method: str, payload: dict[str, Any]) -> Any:
        for attempt in range(3):
            try:
                r = self.client.post(f"{self._base}/{method}", json=payload)
                data = r.json()
            except (httpx.HTTPError, ValueError) as e:
                # текст исключения может содержать URL с токеном — пишем только тип
                raise TelegramError(f"Telegram не ответил ({type(e).__name__})") from None
            if data.get("ok"):
                return data["result"]
            retry_after = (data.get("parameters") or {}).get("retry_after")
            if data.get("error_code") == 429 and retry_after and attempt < 2:
                time.sleep(float(retry_after))
                continue
            raise TelegramError(f"Telegram: {data.get('error_code')} {data.get('description', '')}")

    def own_messages_hint(self) -> dict[str, set[int]]:
        """id сообщений, которые пользователи сами писали боту (из getUpdates) — их при очистке не трогаем."""
        incoming: dict[str, set[int]] = {}
        for upd in self._call("getUpdates", {"allowed_updates": ["message"]}) or []:
            msg = upd.get("message") or {}
            if "chat" in msg and "message_id" in msg:
                incoming.setdefault(str(msg["chat"]["id"]), set()).add(int(msg["message_id"]))
        return incoming

    def clear(self, chat_id: str, keep: set[int], depth: int = 500) -> int:
        """Удалить сообщения бота в личном чате. Telegram разрешает боту удалять свои сообщения
        не старше 48 часов; id в личном чате идут подряд, поэтому удаляем диапазон до свежего
        служебного сообщения (его тоже), пропуская сообщения пользователя из keep.
        Несуществующие и чужие id Telegram просто пропускает. Возвращает, сколько id отправлено на удаление."""
        last = self._send_one(chat_id, "🧹 Очищаю чат…", silent=True)
        ids = [i for i in range(max(1, last - depth), last + 1) if i not in keep]
        for start in range(0, len(ids), 100):  # deleteMessages — до 100 id за раз
            self._call("deleteMessages", {"chat_id": chat_id, "message_ids": ids[start: start + 100]})
        return len(ids)


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
