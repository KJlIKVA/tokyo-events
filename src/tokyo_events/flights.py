"""Авиабилеты из Токио через Google Flights (SerpApi).

Раз в день — поиск Google Flights Explore («куда дешевле всего улететь в ближайшие полгода»): по каждому
направлению самая низкая цена туда и обратно. Бот запоминает эти цены; когда цена направления заметно ниже
обычной (медиана за прошлые дни), лучшие находки проверяет обычным поиском Google Flights на те же даты —
Google сам говорит, низкая ли это цена и какой диапазон для маршрута обычный, — и присылает уведомление.
"""

from __future__ import annotations

import html
import logging
import statistics
import urllib.parse
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from .config import Config, FlightSearch
from .hotels import CIRCLES, _rank, due, stay_ru, yen
from .serpapi import Budget, SerpApiError

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS flight_prices (     -- самая низкая цена направления в каждой проверке
    search      TEXT NOT NULL,                 -- имя поиска из config.yaml
    dest        TEXT NOT NULL,                 -- код аэропорта назначения
    name        TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    price       INTEGER NOT NULL,              -- туда и обратно за человека, ¥
    start_date  TEXT NOT NULL,
    end_date    TEXT NOT NULL,
    airline     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS flight_prices_idx ON flight_prices (search, dest);
CREATE TABLE IF NOT EXISTS flight_alerts (     -- о чём уже сообщили
    search TEXT NOT NULL,
    dest   TEXT NOT NULL,
    price  INTEGER NOT NULL,
    at     TEXT NOT NULL,
    PRIMARY KEY (search, dest)
);
CREATE TABLE IF NOT EXISTS flight_checks (     -- когда проверяли (и при пустом ответе)
    at TEXT NOT NULL
);
"""

DURATION_DAYS = {1: 2, 2: 7, 3: 14}  # если Google не дал дату возвращения


@dataclass
class Fare:
    search: str
    dest: str
    name: str
    country: str
    price: int
    start_date: str
    end_date: str
    airline: str = ""
    stops: int | None = None
    duration_min: int | None = None


@dataclass
class FlightDeal:
    fare: Fare
    baseline: int
    pct: int
    rank: str
    typical: tuple[int, int] | None = None   # обычный диапазон Google Flights на эти даты
    cheaper_again: bool = False


@dataclass
class FlightReport:
    checked: int = 0
    fares: int = 0
    sent: int = 0
    errors: list[str] = field(default_factory=list)


def parse_explore(data: dict[str, Any], search: FlightSearch) -> list[Fare]:
    fares = []
    for d in data.get("destinations") or []:
        code = ((d.get("destination_airport") or {}).get("code") or "").upper()
        price, start = d.get("flight_price"), d.get("start_date")
        if not code or not price or not start:
            continue
        end = d.get("end_date") or d.get("return_date") or \
            (date.fromisoformat(start) + timedelta(days=DURATION_DAYS[search.travel_duration])).isoformat()
        fares.append(Fare(search=search.name, dest=code, name=d.get("name") or code, country=d.get("country") or "",
                          price=int(price), start_date=start, end_date=end, airline=d.get("airline") or "",
                          stops=d.get("number_of_stops"), duration_min=d.get("flight_duration")))
    return fares


def find_flight_deals(db: Any, fares: list[Fare], cfg: Config, today: date) -> list[FlightDeal]:
    f = cfg.flights
    since = (today - timedelta(days=f.history_days)).isoformat()
    deals = []
    for fare in fares:
        history = [r[0] for r in db.execute(
            "SELECT price FROM flight_prices WHERE search = ? AND dest = ? AND observed_at >= ?",
            (fare.search, fare.dest, since))]
        if len(history) < f.min_history:
            continue
        baseline = int(statistics.median(history))
        pct = round((1 - fare.price / baseline) * 100) if baseline else 0
        rank = _rank(pct, cfg)
        if rank and baseline - fare.price >= f.min_saving_yen:
            deals.append(FlightDeal(fare, baseline, pct, rank))
    return sorted(deals, key=lambda d: -d.pct)


def insights(budget: Budget, cfg: Config, fare: Fare) -> dict[str, Any]:
    """Google Flights на те же даты: price_insights (price_level, typical_price_range)."""
    data = budget.search("google_flights", {
        "departure_id": cfg.flights.departure_id, "arrival_id": fare.dest, "type": 1,
        "outbound_date": fare.start_date, "return_date": fare.end_date,
        "currency": "JPY", "gl": "jp", "hl": "ru"})
    return data.get("price_insights") or {}


def flights_url(fare: Fare) -> str:
    q = f"Flights to {fare.dest} from TYO on {fare.start_date} through {fare.end_date}"
    return "https://www.google.com/travel/flights?" + urllib.parse.urlencode({"q": q, "curr": "JPY", "hl": "ru"})


def _duration(minutes: int | None) -> str:
    if not minutes:
        return ""
    h, m = divmod(int(minutes), 60)
    return f"{h} ч {m:02d} мин" if h else f"{m} мин"


def format_flight(deal: FlightDeal) -> str:
    e = html.escape
    fare = deal.fare
    nights = (date.fromisoformat(fare.end_date) - date.fromisoformat(fare.start_date)).days
    head = "🔴 Ещё дешевле · " if deal.cheaper_again else f"{CIRCLES[deal.rank]} ✈️ −{deal.pct}% · "
    where = e(fare.name) + (f", {e(fare.country)}" if fare.country and fare.country not in ("Япония", "Japan")
                            else "")
    blocks = [
        f"{head}<b>{where}</b> · из Токио",
        f"📅 {stay_ru(fare.start_date, nights)} · туда и обратно, за человека",
        f"💴 {yen(fare.price)} вместо {yen(deal.baseline)} (обычно за последние недели) · −{deal.pct}%",
    ]
    if deal.typical:
        blocks.append(f"📊 Google Flights: обычно {yen(deal.typical[0])} – {yen(deal.typical[1])}")
    stops = "" if fare.stops is None else "прямой" if fare.stops == 0 else f"пересадок: {fare.stops}"
    trip = " · ".join(x for x in (e(fare.airline), stops, _duration(fare.duration_min)) if x)
    if trip:
        blocks.append(f"✈️ {trip}")
    blocks.append(f'🌐 <a href="{e(flights_url(fare), quote=True)}">Открыть в Google Flights</a>')
    blocks.append(f"#авиабилеты #{fare.name.replace(' ', '_').replace('-', '_')}")
    return "\n\n".join(blocks)


def check_flights(cfg: Config, db: Any, sender: Any, now: datetime, budget: Budget | None,
                  force: bool = False) -> FlightReport:
    report = FlightReport()
    f = cfg.flights
    if not f.enabled or budget is None:
        return report
    db.executescript(SCHEMA)
    if not force and not due(db.execute("SELECT MAX(at) FROM flight_checks").fetchone()[0], now, f.run_at_hours):
        return report
    today = now.date()
    fares: list[Fare] = []
    for search in f.searches:
        if not budget.take():
            log.info("авиабилеты: дневная доля запросов SerpApi исчерпана")
            break
        params: dict[str, Any] = {"departure_id": f.departure_id, "type": 1, "month": 0,
                                  "travel_duration": search.travel_duration, "currency": "JPY", "gl": "jp", "hl": "ru"}
        if search.arrival_area_id:
            params["arrival_area_id"] = search.arrival_area_id
        if search.max_duration:
            params["max_duration"] = search.max_duration
        try:
            data = budget.search("google_travel_explore", params)
        except SerpApiError as e:
            report.errors.append(f"авиабилеты {search.name}: {e}")
            log.warning("авиабилеты %s: %s", search.name, e)
            continue
        report.checked += 1
        fares += parse_explore(data, search)
    report.fares = len(fares)
    deals = find_flight_deals(db, fares, cfg, today)
    at = now.isoformat(timespec="seconds")
    if report.checked:
        db.execute("INSERT INTO flight_checks VALUES (?)", (at,))
    db.executemany("INSERT INTO flight_prices VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                   [(x.search, x.dest, x.name, at, x.price, x.start_date, x.end_date, x.airline) for x in fares])
    realert_since = (now - timedelta(days=f.realert_days)).isoformat(timespec="seconds")
    verified = 0
    for deal in deals:
        if report.sent >= f.max_alerts_per_run:
            break
        fare = deal.fare
        prev = db.execute("SELECT price, at FROM flight_alerts WHERE search = ? AND dest = ?",
                          (fare.search, fare.dest)).fetchone()
        if prev and prev[1] >= realert_since:
            if fare.price > prev[0] * (1 - cfg.deals.min_pct / 100):
                continue  # недавно сообщали, заметно дешевле не стало
            deal.cheaper_again = True
        if verified < f.verify_per_run and budget.take():
            verified += 1
            try:
                pi = insights(budget, cfg, fare)
            except SerpApiError as e:
                log.warning("авиабилеты: проверка %s: %s", fare.dest, e)
                pi = {}
            if pi.get("price_level") == "high":
                log.info("авиабилеты: %s — по Google цена высокая, пропуск", fare.name)
                continue
            rng = pi.get("typical_price_range") or []
            if len(rng) == 2 and all(isinstance(x, int | float) for x in rng):
                deal.typical = (int(rng[0]), int(rng[1]))
        silent = deal.rank in cfg.filters.silent_ranks and not deal.cheaper_again
        try:
            problems = sender.send(format_flight(deal), silent=silent)
        except Exception as e:  # Telegram недоступен: попробуем в следующий раз
            report.errors.append(f"авиабилеты: {e}")
            continue
        report.errors += [f"Telegram: {p}" for p in problems or []]
        db.execute("INSERT OR REPLACE INTO flight_alerts VALUES (?, ?, ?, ?)", (fare.search, fare.dest, fare.price, at))
        report.sent += 1
    db.execute("DELETE FROM flight_prices WHERE observed_at < ?",
               ((today - timedelta(days=f.history_days * 2)).isoformat(),))
    db.execute("DELETE FROM flight_checks WHERE at < ?", ((today - timedelta(days=7)).isoformat(),))
    log.info("авиабилеты: запросов %d, направлений %d, отправлено %d", report.checked, report.fares, report.sent)
    return report
