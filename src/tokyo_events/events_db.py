"""База событий (SQLite, state/events.db): что уже отправлено, все подробности и история изменений.

Нужна, чтобы:
- не присылать одно событие повторно (сколько бы СМИ о нём ни написали);
- замечать обновления: перенос дат, объявленную цену, смену площадки, отмену — и присылать их 🔴.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from .venue import clean_name

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id             TEXT PRIMARY KEY,   -- ключ первой статьи о событии
    rank           TEXT NOT NULL,
    category       TEXT NOT NULL DEFAULT 'other',
    artist         TEXT NOT NULL DEFAULT '',
    title          TEXT NOT NULL,
    dates_ru       TEXT NOT NULL DEFAULT '',
    date_precision TEXT NOT NULL DEFAULT '',
    start_date     TEXT NOT NULL DEFAULT '',
    end_date       TEXT NOT NULL DEFAULT '',
    price_ru       TEXT NOT NULL DEFAULT '',
    is_free        INTEGER NOT NULL DEFAULT 0,
    status         TEXT NOT NULL DEFAULT 'scheduled',
    venues         TEXT NOT NULL DEFAULT '[]',   -- JSON: [{name, address, area_ru, on_map, dates_ru}]
    official_url   TEXT NOT NULL DEFAULT '',
    venue_url      TEXT NOT NULL DEFAULT '',
    tickets        TEXT NOT NULL DEFAULT '',     -- on_sale | sale_announced | later | not_needed | unknown
    tickets_ru     TEXT NOT NULL DEFAULT '',
    first_sent_at  TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    updates_sent   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS event_sources (       -- все статьи, отнесённые к событию
    event_id  TEXT NOT NULL REFERENCES events(id),
    item_key  TEXT NOT NULL,
    url       TEXT NOT NULL,
    title     TEXT NOT NULL,
    kind      TEXT NOT NULL,                       -- new | update | duplicate
    at        TEXT NOT NULL,
    PRIMARY KEY (event_id, item_key)
);
CREATE TABLE IF NOT EXISTS event_changes (       -- история обновлений
    event_id  TEXT NOT NULL REFERENCES events(id),
    at        TEXT NOT NULL,
    field     TEXT NOT NULL,
    old       TEXT NOT NULL,
    new       TEXT NOT NULL
);
"""

# колонки, добавленные после первой версии базы: у старой базы их нет — добавляем
ADDED_COLUMNS = {"tickets": "TEXT NOT NULL DEFAULT ''", "tickets_ru": "TEXT NOT NULL DEFAULT ''"}

STATUS_RU = {"scheduled": "по плану", "postponed": "перенесено", "cancelled": "отменено"}
TICKETS_OPEN = ("on_sale", "sale_announced")  # билеты продаются или объявлен старт продаж


@dataclass
class Event:
    id: str
    rank: str
    category: str
    artist: str
    title: str
    dates_ru: str = ""
    date_precision: str = ""
    start_date: str = ""
    end_date: str = ""
    price_ru: str = ""
    is_free: bool = False
    status: str = "scheduled"
    venues: list[dict] = field(default_factory=list)
    official_url: str = ""
    venue_url: str = ""
    tickets: str = ""
    tickets_ru: str = ""
    first_sent_at: str = ""
    updated_at: str = ""
    updates_sent: int = 0

    @property
    def where(self) -> str:
        return ", ".join(v.get("name", "") for v in self.venues[:3])


@dataclass
class Change:
    field: str     # dates | price | tickets | venues | status
    label: str     # по-русски, для уведомления
    old: str
    new: str


def _prices(text: str) -> set[int]:
    return {int(x.replace(",", "").replace(" ", "")) for x in re.findall(r"\d[\d, ]*\d|\d", text or "")
            if x.replace(",", "").replace(" ", "").isdigit()}


def _venue_names(venues: list[dict]) -> set[str]:
    return {re.sub(r"\s+", "", clean_name(v.get("name", ""))).lower() for v in venues if v.get("name", "").strip()}


def diff(event: Event, new: dict) -> list[Change]:
    """Что изменилось. Пустое новое значение — не изменение: в статье просто не упомянули."""
    changes: list[Change] = []
    # даты: «объявлены» (не было), «изменились» (другие дни), «уточнились» (было «весна», стало «12 марта»)
    new_dates = new.get("dates_ru", "").strip()
    new_precision = new.get("date_precision", "")
    if new_dates and new_precision in ("exact", "approximate"):
        new_iso = (new.get("start_date", ""), new.get("end_date", ""))
        old_iso = (event.start_date, event.end_date)
        moved = any(n and o and n != o for n, o in zip(new_iso, old_iso, strict=True))
        if not event.dates_ru:
            changes.append(Change("dates", "Объявлены даты", "", new_dates))
        elif moved:
            changes.append(Change("dates", "Даты изменились", event.dates_ru, new_dates))
        elif event.date_precision == "approximate" and new_precision == "exact":
            changes.append(Change("dates", "Даты уточнились", event.dates_ru, new_dates))
    # цена
    new_price = new.get("price_ru", "").strip()
    if new.get("is_free") and not event.is_free:
        changes.append(Change("price", "Вход бесплатный", event.price_ru, new_price or "бесплатно"))
    elif new_price and not event.price_ru:
        changes.append(Change("price", "Объявлена цена", "", new_price))
    elif new_price and _prices(new_price) and _prices(new_price) != _prices(event.price_ru):
        changes.append(Change("price", "Цена изменилась", event.price_ru, new_price))
    # билеты: объявили старт продаж или продажа уже идёт — один раз, дальше не сообщаем.
    # У событий из базы до 21.10.2026 о билетах ничего не записано ("") — по ним не сообщаем, чтобы не завалить
    new_tickets = new.get("tickets", "")
    if new_tickets in TICKETS_OPEN and event.tickets and event.tickets not in TICKETS_OPEN:
        changes.append(Change("tickets", "Билеты в продаже" if new_tickets == "on_sale" else "Объявлен старт продаж",
                              "", new.get("tickets_ru", "").strip() or "подробности на сайте"))
    # площадки
    old_names, new_names = _venue_names(event.venues), _venue_names(new.get("venues", []))
    if new_names and old_names and new_names != old_names:
        added = [v["name"] for v in new.get("venues", []) if _venue_names([v]) - old_names]
        if old_names <= new_names:
            changes.append(Change("venues", "Добавлены площадки", event.where, ", ".join(added)))
        else:
            changes.append(Change("venues", "Площадка изменилась", event.where,
                                  ", ".join(v["name"] for v in new.get("venues", [])[:3])))
    elif new_names and not old_names:
        changes.append(Change("venues", "Объявлена площадка", "", ", ".join(v["name"] for v in new["venues"][:3])))
    # перенос / отмена
    status = new.get("status", "scheduled")
    if status != event.status and status in ("postponed", "cancelled"):
        changes.append(Change("status", "Перенесено" if status == "postponed" else "Отменено",
                              STATUS_RU.get(event.status, event.status), STATUS_RU[status]))
    return changes


class EventStore:
    def __init__(self, path: str | Path) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        have = {r[1] for r in self.db.execute("PRAGMA table_info(events)")}
        for col, decl in ADDED_COLUMNS.items():
            if col not in have:
                self.db.execute(f"ALTER TABLE events ADD COLUMN {col} {decl}")

    # --- чтение ---
    def _event(self, row: sqlite3.Row) -> Event:
        d = dict(row)
        d["venues"] = json.loads(d["venues"] or "[]")
        d["is_free"] = bool(d["is_free"])
        return Event(**d)

    def get(self, event_id: str) -> Event | None:
        row = self.db.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return self._event(row) if row else None

    def ids(self) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT id FROM events")}

    def recent(self, limit: int = 150) -> list[Event]:
        """Для модели — «уже отправлено»: свежие сначала."""
        rows = self.db.execute("SELECT * FROM events ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        return [self._event(r) for r in rows]

    def count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    def changes(self, event_id: str) -> list[tuple[str, str, str, str]]:
        return [tuple(r) for r in self.db.execute(
            "SELECT at, field, old, new FROM event_changes WHERE event_id = ? ORDER BY at", (event_id,))]

    # --- запись ---
    def add(self, event: Event) -> None:
        self.db.execute(
            "INSERT INTO events (id, rank, category, artist, title, dates_ru, date_precision, start_date, end_date,"
            " price_ru, is_free, status, venues, official_url, venue_url, tickets, tickets_ru, first_sent_at,"
            " updated_at, updates_sent) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (event.id, event.rank, event.category, event.artist, event.title, event.dates_ru, event.date_precision,
             event.start_date,
             event.end_date, event.price_ru, int(event.is_free), event.status,
             json.dumps(event.venues, ensure_ascii=False), event.official_url, event.venue_url,
             event.tickets, event.tickets_ru, event.first_sent_at, event.updated_at or event.first_sent_at,
             event.updates_sent))

    def add_source(self, event_id: str, item_key: str, url: str, title: str, kind: str, now: datetime) -> None:
        self.db.execute("INSERT OR IGNORE INTO event_sources VALUES (?, ?, ?, ?, ?, ?)",
                        (event_id, item_key, url, title, kind, now.isoformat(timespec="seconds")))

    def apply(self, event_id: str, new: dict, changes: list[Change], now: datetime) -> None:
        """Записать обновление: новые непустые значения поверх старых + история изменений."""
        e = self.get(event_id)
        assert e is not None
        at = now.isoformat(timespec="seconds")
        fields = {"title": new.get("title_ru") or e.title}
        if new.get("dates_ru") and new.get("date_precision") in ("exact", "approximate"):
            fields.update(dates_ru=new["dates_ru"], date_precision=new["date_precision"],
                          start_date=new.get("start_date") or e.start_date, end_date=new.get("end_date") or e.end_date)
        if new.get("price_ru"):
            fields["price_ru"] = new["price_ru"]
        if new.get("is_free"):
            fields["is_free"] = 1
        if new.get("venues"):
            fields["venues"] = json.dumps(new["venues"], ensure_ascii=False)
        if new.get("status") in ("postponed", "cancelled"):
            fields["status"] = new["status"]
        if new.get("tickets") in (*TICKETS_OPEN, "later", "not_needed"):
            fields["tickets"] = new["tickets"]
        if new.get("tickets_ru"):
            fields["tickets_ru"] = new["tickets_ru"]
        for key in ("official_url", "venue_url"):
            if new.get(key):
                fields[key] = new[key]
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(f"UPDATE events SET {sets}, updated_at = ?, updates_sent = updates_sent + ? WHERE id = ?",
                        (*fields.values(), at, 1 if changes else 0, event_id))
        self.db.executemany("INSERT INTO event_changes VALUES (?, ?, ?, ?, ?)",
                            [(event_id, at, c.field, c.old, c.new) for c in changes])

    def prune(self, today: date, keep_days_after_end: int = 30, keep_days_undated: int = 180) -> None:
        """Прошедшие события больше не нужны для поиска повторов."""
        end_border = (today - timedelta(days=keep_days_after_end)).isoformat()
        undated_border = (today - timedelta(days=keep_days_undated)).isoformat()
        old = [r[0] for r in self.db.execute(
            "SELECT id FROM events WHERE (end_date != '' AND end_date < ?) OR (end_date = '' AND updated_at < ?)",
            (end_border, undated_border))]
        for table, col in (("event_changes", "event_id"), ("event_sources", "event_id"), ("events", "id")):
            self.db.executemany(f"DELETE FROM {table} WHERE {col} = ?", [(i,) for i in old])

    def commit(self) -> None:
        self.db.commit()

    def compact(self) -> None:
        """Удалённые строки SQLite место не возвращает: без VACUUM файл только растёт, а GitHub не примет
        память бота больше 100 МБ."""
        self.db.execute("VACUUM")

    def close(self) -> None:
        self.db.close()  # без commit — изменения отбрасываются (так работает dry-run)
