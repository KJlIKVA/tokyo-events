"""Отели с ценами Booking.com, Trip.com, Agoda и др. — через Google Hotels (SerpApi).

У Booking.com и Trip.com нет открытого API цен, а Google Hotels показывает по каждому отелю цены всех
сайтов бронирования и сам помечает выгодные: «на N% ниже обычного» — когда цена ниже медианы цены отеля
за год (Google ставит метку от 15%). Если метки в ответе нет, бот сравнивает цену с прошлыми ценами того же
отеля. Места и даты — по кругу, на сколько хватит дневной доли запросов SerpApi.
"""

from __future__ import annotations

import html
import logging
import re
import statistics
import urllib.parse
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from .config import Config, GoogleHotelArea
from .hotels import CIRCLES, _rank, due, stay_dates, stay_ru, yen
from .serpapi import Budget, SerpApiError

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS ghotel_prices (     -- цена отеля в Google Hotels (самая низкая из всех сайтов)
    token       TEXT NOT NULL,                 -- property_token Google
    stay_date   TEXT NOT NULL,
    nights      INTEGER NOT NULL,
    observed_at TEXT NOT NULL,
    price       INTEGER NOT NULL,              -- за номер за ночь с налогами, ¥
    area        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ghotel_prices_idx ON ghotel_prices (token, nights);
CREATE TABLE IF NOT EXISTS ghotel_alerts (
    token     TEXT NOT NULL,
    stay_date TEXT NOT NULL,
    nights    INTEGER NOT NULL,
    price     INTEGER NOT NULL,
    at        TEXT NOT NULL,
    PRIMARY KEY (token, stay_date, nights)
);
CREATE TABLE IF NOT EXISTS ghotel_checks (     -- когда проверяли (и при пустом ответе)
    at TEXT NOT NULL
);
"""

PCT = re.compile(r"(\d{1,2})\s*%")


@dataclass
class Property:
    token: str
    name: str
    area: str
    stay_date: str
    nights: int
    price: int
    prices: list[tuple[str, int]]       # (сайт, цена) по возрастанию
    rating: float | None = None
    google_pct: int = 0                 # «на N% ниже обычного» от Google
    lat: float | None = None
    lon: float | None = None
    link: str = ""


@dataclass
class HotelDeal:
    prop: Property
    baseline: int
    pct: int
    basis: str                          # google — метка Google; history — прошлые цены этого отеля
    rank: str
    cheaper_again: bool = False


@dataclass
class GoogleHotelsReport:
    checked: int = 0
    offers: int = 0
    sent: int = 0
    errors: list[str] = field(default_factory=list)


def _num(v: Any) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def parse_properties(data: dict[str, Any], area: str, stay_date: str, nights: int) -> list[Property]:
    out = []
    for p in data.get("properties") or []:
        token = p.get("property_token")
        price = _num((p.get("rate_per_night") or {}).get("extracted_lowest"))
        if not token or not price:
            continue
        prices = sorted(((x.get("source") or "", _num((x.get("rate_per_night") or {}).get("extracted_lowest")))
                         for x in p.get("prices") or []), key=lambda t: t[1])
        prices = [(s, v) for s, v in prices if s and v]
        deal_text = " ".join(str(p.get(k) or "") for k in ("deal", "deal_description"))
        m = PCT.search(deal_text)
        gps = p.get("gps_coordinates") or {}
        out.append(Property(
            token=token, name=p.get("name") or "", area=area, stay_date=stay_date, nights=nights, price=price,
            prices=prices, rating=float(p["overall_rating"]) if p.get("overall_rating") else None,
            google_pct=int(m.group(1)) if m else 0, lat=gps.get("latitude"), lon=gps.get("longitude"),
            link=p.get("link") or ""))
    return out


def find_hotel_deals(db: Any, props: list[Property], cfg: Config, today: date) -> list[HotelDeal]:
    g = cfg.google_hotels
    since = (today - timedelta(days=g.history_days)).isoformat()
    deals = []
    for p in props:
        if p.rating is not None and p.rating < g.min_review:
            continue
        candidates = []
        if p.google_pct:
            candidates.append((round(p.price / (1 - p.google_pct / 100)), "google"))
        weekday = date.fromisoformat(p.stay_date).weekday()
        history = [price for stay, price in db.execute(
            "SELECT stay_date, price FROM ghotel_prices WHERE token = ? AND nights = ? AND observed_at >= ?",
            (p.token, p.nights, since)) if date.fromisoformat(stay).weekday() == weekday]
        if len(history) >= g.min_compare:
            candidates.append((int(statistics.median(history)), "history"))
        for baseline, basis in sorted(candidates, reverse=True):
            pct = round((1 - p.price / baseline) * 100) if baseline else 0
            rank = _rank(pct, cfg)
            if rank and baseline - p.price >= g.min_saving_yen:
                deals.append(HotelDeal(p, baseline, pct, basis, rank))
                break
    order = {"purple": 0, "orange": 1, "green": 2}
    return sorted(deals, key=lambda d: (order[d.rank], -d.pct))


def plan_today(cfg: Config, today: date, n: int) -> list[tuple[GoogleHotelArea, date, int]]:
    """n пар (место, дата заезда) на сегодня — по кругу: каждый день следующие."""
    g = cfg.google_hotels
    pairs = [(a, d, s.nights) for s in g.stays for d in stay_dates(s, today, g.alert_weeks) for a in g.areas]
    if not pairs or n <= 0:
        return []
    start = (today.toordinal() * n) % len(pairs)
    return [pairs[(start + i) % len(pairs)] for i in range(min(n, len(pairs)))]


def hotel_url(p: Property) -> str:
    return "https://www.google.com/travel/search?" + urllib.parse.urlencode({"q": p.name, "hl": "ru", "curr": "JPY"})


def format_hotel(deal: HotelDeal, cfg: Config) -> str:
    e = html.escape
    p = deal.prop
    head = "🔴 Ещё дешевле · " if deal.cheaper_again else f"{CIRCLES[deal.rank]} 🏨 −{deal.pct}% · "
    basis = "по Google — ниже обычного для этого отеля" if deal.basis == "google" else "раньше в этот день недели"
    per = " за первую ночь" if p.nights > 1 else " за ночь"
    blocks = [
        f"{head}<b>{e(p.name)}</b> · {e(p.area)}",
        f"📅 {stay_ru(p.stay_date, p.nights)} · {cfg.google_hotels.adults} взрослых",
        f"💴 {yen(p.price)}{per} вместо ~{yen(deal.baseline)} ({basis}) · −{deal.pct}%",
    ]
    if p.prices:
        blocks.append("🏷 " + " · ".join(f"{e(s)} {yen(v)}" for s, v in p.prices[:4]))
    query = f"{p.lat},{p.lon}" if p.lat and p.lon else p.name
    maps = "https://www.google.com/maps/search/?" + urllib.parse.urlencode({"api": "1", "query": query})
    blocks.append(f'🗺 <a href="{e(maps, quote=True)}">{e(p.name)}</a>')
    blocks.append(f'🌐 <a href="{e(hotel_url(p), quote=True)}">Сравнить цены в Google Hotels</a>')
    if p.rating:
        blocks.append(f"⭐ {p.rating:.1f}")
    blocks.append(f"#отели #{p.area.replace(' ', '_').replace('-', '_')}")
    return "\n\n".join(blocks)


def check_google_hotels(cfg: Config, db: Any, sender: Any, now: datetime, budget: Budget | None,
                        force: bool = False) -> GoogleHotelsReport:
    report = GoogleHotelsReport()
    g = cfg.google_hotels
    if not g.enabled or budget is None or not g.areas:
        return report
    db.executescript(SCHEMA)
    if not force and not due(db.execute("SELECT MAX(at) FROM ghotel_checks").fetchone()[0], now, g.run_at_hours):
        return report
    today = now.date()
    n = min(g.max_per_run, budget.free - budget.spent)
    props: list[Property] = []
    for area, checkin, nights in plan_today(cfg, today, n):
        if not budget.take():
            break
        try:
            data = budget.search("google_hotels", {
                "q": area.q, "check_in_date": checkin.isoformat(),
                "check_out_date": (checkin + timedelta(days=nights)).isoformat(),
                "adults": g.adults, "currency": "JPY", "gl": "jp", "hl": "ja"})
        except SerpApiError as e:
            report.errors.append(f"Google Hotels {area.name} {checkin}: {e}")
            log.warning("Google Hotels %s %s: %s", area.name, checkin, e)
            continue
        report.checked += 1
        props += parse_properties(data, area.name, checkin.isoformat(), nights)
    at = now.isoformat(timespec="seconds")
    if report.checked:
        db.execute("INSERT INTO ghotel_checks VALUES (?)", (at,))
    report.offers = len(props)
    deals = find_hotel_deals(db, props, cfg, today)
    db.executemany("INSERT INTO ghotel_prices VALUES (?, ?, ?, ?, ?, ?)",
                   [(p.token, p.stay_date, p.nights, at, p.price, p.area) for p in props])
    per_area: dict[str, int] = {}
    for deal in deals:
        if report.sent >= g.max_alerts_per_run:
            break
        p = deal.prop
        if per_area.get(p.area):
            continue  # с одного места — только лучшая скидка за проверку
        prev = db.execute("SELECT price FROM ghotel_alerts WHERE token = ? AND stay_date = ? AND nights = ?",
                          (p.token, p.stay_date, p.nights)).fetchone()
        if prev:
            if p.price > prev[0] * (1 - cfg.deals.min_pct / 100):
                continue
            deal.cheaper_again = True
        silent = deal.rank in cfg.filters.silent_ranks and not deal.cheaper_again
        try:
            problems = sender.send(format_hotel(deal, cfg), silent=silent)
        except Exception as e:
            report.errors.append(f"Google Hotels: {e}")
            continue
        report.errors += [f"Telegram: {x}" for x in problems or []]
        per_area[p.area] = 1
        db.execute("INSERT OR REPLACE INTO ghotel_alerts VALUES (?, ?, ?, ?, ?)",
                   (p.token, p.stay_date, p.nights, p.price, at))
        report.sent += 1
    old = (today - timedelta(days=g.history_days)).isoformat()
    db.execute("DELETE FROM ghotel_prices WHERE observed_at < ?", (old,))
    db.execute("DELETE FROM ghotel_alerts WHERE stay_date < ?", (today.isoformat(),))
    db.execute("DELETE FROM ghotel_checks WHERE at < ?", ((today - timedelta(days=7)).isoformat(),))
    log.info("Google Hotels: запросов %d, отелей %d, отправлено %d", report.checked, report.offers, report.sent)
    return report
