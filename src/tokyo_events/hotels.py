"""Скидки на отели и рёканы по реальным ценам — API 楽天トラベル (VacantHotelSearch). Без ИИ.

Для каждого места из config.yaml (hotels.areas) берём цены на ближайшие выходные (заезд в субботу) и сравниваем:
- с ценами того же отеля на другие выходные (обычная цена);
- с прошлыми ценами того же отеля на ту же дату (подешевело).
Если дешевле на hotels/deals-порог (от 10%) и экономия заметная — уведомление с цветом по размеру скидки.
"""

from __future__ import annotations

import html
import json
import logging
import statistics
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import httpx

from .config import Config, HotelArea, env
from .events_db import EventStore

log = logging.getLogger(__name__)

API = "https://openapi.rakuten.co.jp/engine/api/Travel/VacantHotelSearch/20170426"
CIRCLES = {"green": "🟢", "orange": "🟠", "purple": "🟣"}
WEEKDAYS_RU = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
MONTHS_RU = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
             "ноября", "декабря"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS hotel_prices (      -- каждое наблюдение цены
    hotel_no    TEXT NOT NULL,
    stay_date   TEXT NOT NULL,                 -- дата заезда
    observed_at TEXT NOT NULL,
    price       INTEGER NOT NULL,              -- за номер на всех гостей, за ночь, ¥
    area        TEXT NOT NULL,
    plan_name   TEXT NOT NULL DEFAULT '',
    raw         TEXT NOT NULL DEFAULT ''       -- исходные rakutenCharge/total/chargeFlag — для проверки
);
CREATE INDEX IF NOT EXISTS hotel_prices_idx ON hotel_prices (hotel_no, stay_date);
CREATE TABLE IF NOT EXISTS hotel_alerts (      -- о чём уже сообщили
    hotel_no  TEXT NOT NULL,
    stay_date TEXT NOT NULL,
    price     INTEGER NOT NULL,
    pct       INTEGER NOT NULL,
    at        TEXT NOT NULL,
    PRIMARY KEY (hotel_no, stay_date)
);
"""


class RakutenError(RuntimeError):
    pass


@dataclass
class Offer:
    hotel_no: str
    name: str
    area: str
    stay_date: str
    price: int
    plan_name: str
    url: str
    address: str = ""
    review: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Deal:
    offer: Offer
    baseline: int
    pct: int
    basis: str      # weekends — обычная цена на другие выходные; history — раньше на эту дату
    rank: str
    cheaper_again: bool = False  # уже сообщали, а стало ещё дешевле


# --- API ---------------------------------------------------------------------------

class Rakuten:
    def __init__(self, app_id: str, access_key: str, client: httpx.Client,
                 sleep: Callable[[float], None] = time.sleep, pause_s: float = 1.1) -> None:
        self.app_id, self.access_key, self.client = app_id, access_key, client
        self.sleep, self.pause_s = sleep, pause_s

    def search(self, area: HotelArea, checkin: date, adults: int, nights: int, page: int = 1) -> list[dict]:
        """Сырые записи отелей. Нет свободных номеров — пустой список."""
        params = {
            "applicationId": self.app_id, "format": "json", "formatVersion": 1,
            "checkinDate": checkin.isoformat(), "checkoutDate": (checkin + timedelta(days=nights)).isoformat(),
            "adultNum": adults, "latitude": area.lat, "longitude": area.lon, "datumType": 1,
            "searchRadius": area.radius_km, "searchPattern": 1, "sort": "+roomCharge", "hits": 30, "page": page,
        }
        if area.onsen:
            params["squeezeCondition"] = "onsen"
        for attempt in range(3):
            self.sleep(self.pause_s)  # API просит не частить: не чаще раза в секунду
            try:
                r = self.client.get(API, params=params, headers={"accessKey": self.access_key})
            except httpx.HTTPError as e:
                raise RakutenError(f"нет ответа ({type(e).__name__})") from None
            if r.status_code == 404:  # not_found — свободных номеров нет
                return []
            if r.status_code == 429 and attempt < 2:
                self.sleep(10.0 * (attempt + 1))
                continue
            if r.status_code != 200:
                try:
                    desc = r.json().get("error_description") or r.json().get("error")
                except ValueError:
                    desc = ""
                raise RakutenError(f"{r.status_code} {desc}")
            return r.json().get("hotels", [])
        raise RakutenError("429: слишком много запросов")


def parse_offers(hotels: list[dict], area: str, stay_date: str, adults: int, two_meals: bool) -> list[Offer]:
    """Самый дешёвый подходящий план каждого отеля."""
    best: dict[str, Offer] = {}
    for entry in hotels:
        parts = entry.get("hotel", entry if isinstance(entry, list) else [])
        info: dict = {}
        rooms: list = []
        for part in parts:
            info = part.get("hotelBasicInfo", info)
            rooms = part.get("roomInfo", rooms)
        if not info or not rooms:
            continue
        plan: dict = {}
        for room in rooms:  # список чередует roomBasicInfo и dailyCharge
            if "roomBasicInfo" in room:
                plan = room["roomBasicInfo"]
                continue
            charge = room.get("dailyCharge")
            if not charge or not plan:
                continue
            if two_meals and not (plan.get("withDinnerFlag") == 1 and plan.get("withBreakfastFlag") == 1):
                continue
            price = room_price(charge, adults)
            if not price:
                continue
            no = str(info.get("hotelNo"))
            if no not in best or price < best[no].price:
                best[no] = Offer(
                    hotel_no=no, name=info.get("hotelName", ""), area=area, stay_date=stay_date, price=price,
                    plan_name=plan.get("planName", ""),
                    url=plan.get("reserveUrl") or info.get("planListUrl") or info.get("hotelInformationUrl", ""),
                    address=f"{info.get('address1', '')}{info.get('address2', '')}",
                    review=float(info["reviewAverage"]) if info.get("reviewAverage") else None,
                    raw={k: charge.get(k) for k in ("rakutenCharge", "total", "chargeFlag")})
    return list(best.values())


def room_price(charge: dict, adults: int) -> int:
    """Цена за номер на всех гостей за ночь. total — «итого за ночь»; если его нет, считаем из rakutenCharge
    (chargeFlag 0 — за человека, 1 — за номер)."""
    total = charge.get("total")
    if total:
        return int(total)
    per = charge.get("rakutenCharge")
    if not per:
        return 0
    return int(per) * (adults if charge.get("chargeFlag") == 0 else 1)


def saturdays(today: date, count: int) -> list[date]:
    first = today + timedelta(days=(5 - today.weekday()) % 7)
    return [first + timedelta(weeks=i) for i in range(count)]


# --- поиск скидок ---------------------------------------------------------------------------

def find_deals(db: Any, offers: list[Offer], cfg: Config, today: date) -> list[Deal]:
    """Скидки по ценам этого запуска (другие выходные) и по истории (та же дата раньше)."""
    h = cfg.hotels
    alert_dates = {d.isoformat() for d in saturdays(today, h.alert_weekends)}
    by_hotel: dict[str, dict[str, Offer]] = {}
    for o in offers:
        by_hotel.setdefault(o.hotel_no, {})[o.stay_date] = o
    since = (today - timedelta(days=h.history_days)).isoformat()
    deals: list[Deal] = []
    for dates in by_hotel.values():
        for stay, o in dates.items():
            if stay not in alert_dates or (o.review is not None and o.review < h.min_review):
                continue
            others = [x.price for d, x in dates.items() if d != stay]
            history = [r[0] for r in db.execute(
                "SELECT price FROM hotel_prices WHERE hotel_no = ? AND stay_date = ? AND observed_at >= ?",
                (o.hotel_no, stay, since))]
            candidates = []
            if len(others) >= h.min_compare:
                candidates.append((int(statistics.median(others)), "weekends"))
            if history:
                candidates.append((int(statistics.median(history)), "history"))
            for baseline, basis in sorted(candidates, reverse=True):
                pct = round((1 - o.price / baseline) * 100) if baseline else 0
                rank = _rank(pct, cfg)
                if rank and baseline - o.price >= h.min_saving_yen:
                    deals.append(Deal(o, baseline, pct, basis, rank))
                    break
    return deals


def _rank(pct: int, cfg: Config) -> str | None:
    t = cfg.deals
    return "purple" if pct >= t.purple_pct else "orange" if pct >= t.orange_pct else "green" if pct >= t.min_pct \
        else None


# --- уведомление ---------------------------------------------------------------------------

def yen(n: int) -> str:
    return f"{n:,}".replace(",", " ") + " ¥"


def stay_ru(stay: str, nights: int) -> str:
    d = date.fromisoformat(stay)
    out = d + timedelta(days=nights)
    if out.month == d.month:
        return f"{d.day}–{out.day} {MONTHS_RU[d.month - 1]} ({WEEKDAYS_RU[d.weekday()]}–{WEEKDAYS_RU[out.weekday()]})"
    return (f"{d.day} {MONTHS_RU[d.month - 1]} – {out.day} {MONTHS_RU[out.month - 1]} "
            f"({WEEKDAYS_RU[d.weekday()]}–{WEEKDAYS_RU[out.weekday()]})")


def format_deal(deal: Deal, cfg: Config) -> str:
    e = html.escape
    o, h = deal.offer, cfg.hotels
    head = "🔴 Ещё дешевле · " if deal.cheaper_again else f"{CIRCLES[deal.rank]} ♨️ −{deal.pct}% · "
    meals = "ужин и завтрак" if h.meals == "two_meals" else "любой план"
    basis = "обычно на выходные" if deal.basis == "weekends" else "раньше на эту дату"
    maps = "https://www.google.com/maps/search/?" + urllib.parse.urlencode(
        {"api": "1", "query": f"{o.name} {o.address}".strip()})
    blocks = [
        f"{head}<b>{e(o.name)}</b> · {e(o.area)}",
        f"📅 {stay_ru(o.stay_date, h.nights)} · {h.adults} взрослых · {meals}",
        f"💴 {yen(o.price)} вместо {yen(deal.baseline)} ({basis}) · −{deal.pct}%",
        f'🗺 <a href="{e(maps, quote=True)}">{e(o.name)}</a>',
        f'🌐 <a href="{e(o.url, quote=True)}">Забронировать на Rakuten Travel</a>',
    ]
    extra = " · ".join(x for x in (f"⭐ {o.review:.1f}" if o.review else "", f"план: «{e(o.plan_name)}»"
                                   if o.plan_name else "") if x)
    if extra:
        blocks.append(extra)
    blocks.append(f"#отели #{o.area.replace(' ', '_')}")
    return "\n\n".join(blocks)


# --- запуск ---------------------------------------------------------------------------

@dataclass
class HotelReport:
    checked: int = 0
    offers: int = 0
    sent: int = 0
    errors: list[str] = field(default_factory=list)


def check_hotels(cfg: Config, store: EventStore, sender: Any, now: datetime, client: httpx.Client,
                 force: bool = False, api: Rakuten | None = None) -> HotelReport:
    report = HotelReport()
    h = cfg.hotels
    if not h.enabled:
        return report
    app_id, key = env("RAKUTEN_APPLICATION_ID"), env("RAKUTEN_ACCESS_KEY")
    if api is None and not (app_id and key):
        log.info("отели: нет RAKUTEN_APPLICATION_ID / RAKUTEN_ACCESS_KEY — проверка цен выключена")
        return report
    if not force and now.hour not in h.run_at_hours:
        log.info("отели: цены проверяются в %s ч по Токио — сейчас пропуск", h.run_at_hours)
        return report
    api = api or Rakuten(app_id or "", key or "", client)
    db = store.db
    db.executescript(SCHEMA)
    today = now.date()
    offers: list[Offer] = []
    for area in h.areas:
        for sat in saturdays(today, h.weekends_ahead):
            try:
                raw = api.search(area, sat, h.adults, h.nights)
            except RakutenError as e:
                report.errors.append(f"отели {area.name} {sat}: {e}")
                log.warning("отели %s %s: %s", area.name, sat, e)
                continue
            report.checked += 1
            offers += parse_offers(raw, area.name, sat.isoformat(), h.adults, h.meals == "two_meals")
    report.offers = len(offers)
    deals = sorted(find_deals(db, offers, cfg, today), key=lambda d: -d.pct)
    at = now.isoformat(timespec="seconds")
    db.executemany("INSERT INTO hotel_prices VALUES (?, ?, ?, ?, ?, ?, ?)",
                   [(o.hotel_no, o.stay_date, at, o.price, o.area, o.plan_name, json.dumps(o.raw)) for o in offers])
    for deal in deals:
        if report.sent >= h.max_alerts_per_run:
            break
        prev = db.execute("SELECT price FROM hotel_alerts WHERE hotel_no = ? AND stay_date = ?",
                          (deal.offer.hotel_no, deal.offer.stay_date)).fetchone()
        if prev:
            if deal.offer.price > prev[0] * (1 - cfg.deals.min_pct / 100):
                continue  # уже сообщали, заметно дешевле не стало
            deal.cheaper_again = True
        try:
            problems = sender.send(format_deal(deal, cfg), silent=deal.rank in cfg.filters.silent_ranks
                                   and not deal.cheaper_again)
        except Exception as e:
            report.errors.append(f"отели: {e}")
            continue
        report.errors += [f"Telegram: {p}" for p in problems or []]
        db.execute("INSERT OR REPLACE INTO hotel_alerts VALUES (?, ?, ?, ?, ?)",
                   (deal.offer.hotel_no, deal.offer.stay_date, deal.offer.price, deal.pct, at))
        report.sent += 1
    # старые наблюдения не нужны
    db.execute("DELETE FROM hotel_prices WHERE stay_date < ?", (today.isoformat(),))
    db.execute("DELETE FROM hotel_alerts WHERE stay_date < ?", (today.isoformat(),))
    log.info("отели: запросов %d, предложений %d, скидок отправлено %d, ошибок %d",
             report.checked, report.offers, report.sent, len(report.errors))
    return report
