"""Уведомления в Telegram через Bot API."""

from __future__ import annotations

import html
import logging
import time
import urllib.parse
from typing import Any

import httpx

from .classify import Verdict
from .enrich import Details, Venue, maps_url
from .events_db import Change, Event
from .sources import Item

log = logging.getLogger(__name__)

CIRCLES = {"green": "🟢", "orange": "🟠", "purple": "🟣"}
CATEGORY_TAGS = {"concert": "#концерт", "exhibition": "#выставка", "festival": "#фестиваль",
                 "opening": "#открытие", "big_event": "#грандиозное", "deal": "#скидка", "other": ""}


class TelegramError(RuntimeError):
    pass


def source_name(item: Item) -> str:
    if item.outlet:
        return item.outlet
    host = urllib.parse.urlparse(item.link).hostname or ""
    return "Google Новости" if host == "news.google.com" else host.removeprefix("www.")


def _a(url: str, text: str) -> str:
    return f'<a href="{html.escape(url, quote=True)}">{html.escape(text)}</a>'


def _venue_line(x: Venue) -> str:
    """Конкретное место — 🗺 ссылкой на Google Maps; линия поезда, город, несколько мест — 📍 просто текстом."""
    e = html.escape
    dates = f" — {e(x.dates_ru)}" if x.dates_ru else ""
    if x.on_map:
        return f"🗺 {_a(maps_url(x), x.name)}{dates}"
    area = f", {e(x.area_ru)}" if x.area_ru and x.area_ru not in x.name else ""
    return f"📍 {e(x.name)}{area}{dates}"


def _price_line(d: Details) -> str:
    e = html.escape
    if d.is_free:
        extra = f" ({e(d.price_ru)})" if d.price_ru and "бесплат" not in d.price_ru.lower() else ""
        return "💴 Бесплатно" + extra
    return f"💴 {e(d.price_ru)}" if d.price_ru else "💴 Цена пока не объявлена"


def _body(v: Verdict, item: Item, d: Details) -> list[str]:
    """Даты, цена, место, ссылка, описание, теги — общая часть нового события и обновления."""
    e = html.escape
    blocks = [f"📅 {e(d.dates_ru)}" if d.dates_ru else "📅 Даты пока не объявлены", _price_line(d)]
    if d.venues:
        # площадки тура — одним блоком, по строке на каждую
        blocks.append("\n".join(_venue_line(x) for x in d.venues))
    if d.official_url:
        blocks.append(f"🌐 {_a(d.official_url, 'Официальный сайт')}")
    elif d.venue_url:
        blocks.append(f"🏛 {_a(d.venue_url, 'Сайт площадки')}")
    else:
        # ни официального сайта, ни сайта площадки — хоть что-то, откуда узнать подробности
        link = d.source_url or item.link
        name = source_name(item) if link == item.link else source_name(Item(source="", title="", link=link))
        blocks.append(f"🔗 {_a(link, f'Подробнее: {name}')}")
    if d.summary_ru:
        blocks.append(e(d.summary_ru))
    tags = " ".join(t for t in (CATEGORY_TAGS.get(v.category, ""),
                                f"#{v.artist.replace(' ', '_')}" if v.artist else "") if t)
    if tags:
        blocks.append(tags)
    return blocks


def format_message(v: Verdict, item: Item, d: Details) -> str:
    """Новое событие: ранг и название, даты, цена, место (Google Maps), официальный сайт, описание.
    Между частями — пустая строка (просьба пользователя: сплошняком читается плохо)."""
    title = html.escape(d.title_ru or v.title_ru or item.title)
    badge = f"💸 −{d.discount_pct}% · " if v.category == "deal" and 0 < d.discount_pct < 100 else \
        "💸 Бесплатно · " if v.category == "deal" and d.discount_pct >= 100 else ""
    return "\n\n".join([f"{CIRCLES[v.rank]} {badge}<b>{title}</b>", *_body(v, item, d)])


def format_update(event: Event, v: Verdict, item: Item, d: Details, changes: list[Change]) -> str:
    """🔴 Обновление уже присланного события: что изменилось + актуальная информация целиком.
    Чего нет в новой статье — берём из базы (например, цену, если в статье о переносе её не повторили)."""
    e = html.escape
    merged = d.model_copy(update={
        "dates_ru": d.dates_ru if d.has_dates else event.dates_ru,
        "price_ru": d.price_ru or event.price_ru,
        "is_free": d.is_free or event.is_free,
        "venues": d.venues or [Venue(**x) for x in event.venues],
        "official_url": d.official_url or event.official_url,
        "venue_url": d.venue_url or event.venue_url,
    })
    title = e(event.title or d.title_ru or item.title)
    what = "\n".join(f"• {e(c.label)}: {e(c.old)} → {e(c.new)}" if c.old else f"• {e(c.label)}: {e(c.new)}"
                     for c in changes)
    tagged = v.model_copy(update={"category": event.category or v.category, "artist": event.artist or v.artist})
    return "\n\n".join([f"🔴 <b>Обновление: {title}</b>", f"Что изменилось:\n{what}", *_body(tagged, item, merged)])


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
