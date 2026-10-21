from __future__ import annotations

from datetime import date

from tokyo_events.events_db import Event, EventStore, diff
from tokyo_events.state import Notified, State

from .conftest import NOW


def ev(**kw) -> Event:
    base = dict(id="e1", rank="orange", category="exhibition", artist="", title="Ван Гог",
                dates_ru="10 октября 2026 – 10 января 2027", date_precision="exact", start_date="2026-10-10",
                end_date="2027-01-10", price_ru="взрослые 2 400 ¥",
                venues=[{"name": "上野の森美術館 (Ueno Royal Museum)", "address": "", "area_ru": "", "on_map": True,
                         "dates_ru": ""}], first_sent_at=NOW.isoformat(), updated_at=NOW.isoformat())
    base.update(kw)
    return Event(**base)


def new(**kw) -> dict:
    base = dict(dates_ru="10 октября 2026 – 10 января 2027", date_precision="exact", start_date="2026-10-10",
                end_date="2027-01-10", price_ru="", is_free=False, status="scheduled", venues=[], title_ru="")
    base.update(kw)
    return base


def labels(changes):
    return [c.label for c in changes]


def test_nothing_new_when_article_just_omits_fields():
    assert diff(ev(), new()) == []                                    # цену и место не упомянули — не изменение
    assert diff(ev(), new(dates_ru="", date_precision="unknown")) == []


def test_same_values_written_differently_are_not_changes():
    assert diff(ev(), new(price_ru="一般2,400円", dates_ru="10.10.2026–10.01.2027",
                         venues=[{"name": "上野の森美術館"}])) == []


def test_dates_moved():
    c = diff(ev(), new(dates_ru="17 октября 2026 – 10 января 2027", start_date="2026-10-17"))
    assert labels(c) == ["Даты изменились"] and c[0].old == "10 октября 2026 – 10 января 2027"


def test_dates_announced_and_refined():
    assert labels(diff(ev(dates_ru="", date_precision="", start_date="", end_date=""), new())) == ["Объявлены даты"]
    approx = ev(dates_ru="весна 2027", date_precision="approximate", start_date="", end_date="")
    assert labels(diff(approx, new(dates_ru="12 марта 2027", start_date="2027-03-12", end_date="2027-03-12"))) == \
        ["Даты уточнились"]


def test_price_announced_changed_free():
    assert labels(diff(ev(price_ru=""), new(price_ru="2 400 ¥"))) == ["Объявлена цена"]
    assert labels(diff(ev(), new(price_ru="взрослые 2 600 ¥"))) == ["Цена изменилась"]
    assert labels(diff(ev(price_ru=""), new(is_free=True))) == ["Вход бесплатный"]


def test_venue_changed_or_added():
    other = [{"name": "東京都美術館 (Tokyo Metropolitan Art Museum)"}]
    c = diff(ev(), new(venues=other))
    assert labels(c) == ["Площадка изменилась"] and c[0].new == "東京都美術館 (Tokyo Metropolitan Art Museum)"
    tour = ev(venues=[{"name": "東京ドーム"}])
    c = diff(tour, new(venues=[{"name": "東京ドーム"}, {"name": "京セラドーム大阪"}]))
    assert labels(c) == ["Добавлены площадки"] and c[0].new == "京セラドーム大阪"
    assert labels(diff(ev(venues=[]), new(venues=other))) == ["Объявлена площадка"]


def test_postponed_cancelled():
    assert labels(diff(ev(), new(status="postponed"))) == ["Перенесено"]
    assert labels(diff(ev(), new(status="cancelled", dates_ru="", date_precision="unknown"))) == ["Отменено"]
    assert diff(ev(status="cancelled"), new(status="cancelled")) == []


def test_ticket_sale_announced_once():
    assert labels(diff(ev(tickets="later"), new(tickets="sale_announced", tickets_ru="с 1 ноября"))) == [
        "Объявлен старт продаж"]
    assert labels(diff(ev(tickets="unknown"), new(tickets="on_sale"))) == ["Билеты в продаже"]
    assert diff(ev(tickets="sale_announced"), new(tickets="on_sale")) == []  # о старте продаж уже сообщили
    assert diff(ev(tickets="later"), new(tickets="unknown")) == []
    assert diff(ev(tickets=""), new(tickets="on_sale")) == []  # старые события из базы: о билетах не знаем


def test_old_database_gets_ticket_columns(tmp_path):
    import sqlite3

    path = tmp_path / "events.db"
    old = sqlite3.connect(path)  # база без колонок tickets (как до 21.10.2026)
    old.execute("CREATE TABLE events (id TEXT PRIMARY KEY, rank TEXT NOT NULL, category TEXT NOT NULL DEFAULT "
                "'other', artist TEXT NOT NULL DEFAULT '', title TEXT NOT NULL, dates_ru TEXT NOT NULL DEFAULT '', "
                "date_precision TEXT NOT NULL DEFAULT '', start_date TEXT NOT NULL DEFAULT '', end_date TEXT NOT NULL "
                "DEFAULT '', price_ru TEXT NOT NULL DEFAULT '', is_free INTEGER NOT NULL DEFAULT 0, status TEXT NOT "
                "NULL DEFAULT 'scheduled', venues TEXT NOT NULL DEFAULT '[]', official_url TEXT NOT NULL DEFAULT '', "
                "venue_url TEXT NOT NULL DEFAULT '', first_sent_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
                "updates_sent INTEGER NOT NULL DEFAULT 0)")
    old.execute("INSERT INTO events (id, rank, title, first_sent_at, updated_at) VALUES ('a', 'green', 'A', 'x', 'x')")
    old.commit()
    old.close()
    store = EventStore(path)
    assert store.get("a").tickets == ""
    store.apply("a", new(tickets="sale_announced", tickets_ru="с 1 ноября"), [], NOW)
    store.add(ev(id="b", tickets="on_sale", tickets_ru="в продаже"))
    assert (store.get("a").tickets_ru, store.get("b").tickets) == ("с 1 ноября", "on_sale")


def test_store_add_apply_history_and_reload(tmp_path):
    path = tmp_path / "events.db"
    store = EventStore(path)
    store.add(ev(price_ru=""))
    changes = diff(store.get("e1"), new(price_ru="2 400 ¥"))
    store.apply("e1", new(price_ru="2 400 ¥", official_url="https://x.jp/"), changes, NOW)
    store.commit()
    store.close()
    again = EventStore(path)
    e = again.get("e1")
    assert e.price_ru == "2 400 ¥" and e.official_url == "https://x.jp/" and e.updates_sent == 1
    assert e.venues[0]["name"].startswith("上野の森美術館")  # пустые venues в обновлении не стирают площадку
    assert again.changes("e1") == [(NOW.isoformat(timespec="seconds"), "price", "", "2 400 ¥")]


def test_close_without_commit_discards(tmp_path):
    """Так работает dry-run: в базу ничего не записывается."""
    path = tmp_path / "events.db"
    store = EventStore(path)
    store.add(ev())
    store.close()
    assert EventStore(path).count() == 0


def test_prune_ended_events():
    store = EventStore(":memory:")
    store.add(ev(id="old", end_date="2026-08-01"))
    store.add(ev(id="current"))
    store.add(ev(id="undated-old", end_date="", updated_at="2026-01-01T00:00:00+09:00"))
    store.add_source("old", "k", "u", "t", "new", NOW)
    store.prune(date(2026, 10, 13))
    assert store.ids() == {"current"}
    assert store.db.execute("SELECT COUNT(*) FROM event_sources").fetchone()[0] == 0


def test_recent_order_and_where():
    store = EventStore(":memory:")
    store.add(ev(id="a", updated_at="2026-10-01T00:00:00+09:00"))
    store.add(ev(id="b", updated_at="2026-10-12T00:00:00+09:00", venues=[{"name": "A"}, {"name": "B"}]))
    recent = store.recent()
    assert [e.id for e in recent] == ["b", "a"] and recent[0].where == "A, B"


def test_migrate_old_notified_memory():
    from tokyo_events.cli import migrate_notified

    state = State(notified=[Notified(key="k1", rank="purple", title="Ado — тур", when="март 2027",
                                     where="東京ドーム, 京セラドーム大阪", artist="Ado", at=NOW.isoformat())])
    store = EventStore(":memory:")
    migrate_notified(state, store)
    e = store.get("k1")
    assert e.title == "Ado — тур" and e.dates_ru == "март 2027" and e.where == "東京ドーム, 京セラドーム大阪"
    assert state.notified == []
