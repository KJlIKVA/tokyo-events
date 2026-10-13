from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from tokyo_events.config import HotelArea
from tokyo_events.events_db import EventStore
from tokyo_events.hotels import (
    Rakuten,
    check_hotels,
    find_deals,
    format_deal,
    parse_offers,
    room_price,
    saturdays,
    stay_ru,
)
from tokyo_events.telegram import PrintTelegram

TODAY = date(2026, 10, 13)  # вторник
NOW = datetime(2026, 10, 13, 9, 5, tzinfo=ZoneInfo("Asia/Tokyo"))


def hotel(no, name, plans, review=4.4):
    """Запись отеля в формате VacantHotelSearch: plans = [(price_total, dinner, breakfast, plan_name)]."""
    rooms = []
    for price, dinner, breakfast, plan in plans:
        rooms.append({"roomBasicInfo": {"planName": plan, "withDinnerFlag": dinner, "withBreakfastFlag": breakfast,
                                        "reserveUrl": f"https://travel.rakuten.co.jp/{no}/{plan}"}})
        rooms.append({"dailyCharge": {"stayDate": "x", "rakutenCharge": price // 2, "total": price, "chargeFlag": 0}})
    return {"hotel": [{"hotelBasicInfo": {"hotelNo": no, "hotelName": name, "reviewAverage": review,
                                          "address1": "神奈川県", "address2": "足柄下郡箱根町湯本1",
                                          "planListUrl": f"https://hotel.travel.rakuten.co.jp/hotelinfo/plan/{no}"}},
                      {"roomInfo": rooms}]}


def test_saturdays_and_stay_text():
    assert saturdays(TODAY, 3) == [date(2026, 10, 17), date(2026, 10, 24), date(2026, 10, 31)]
    assert saturdays(date(2026, 10, 17), 1) == [date(2026, 10, 17)]  # в субботу — эта же суббота
    assert stay_ru("2026-10-17", 1) == "17–18 октября (сб–вс)"
    assert stay_ru("2026-10-31", 1) == "31 октября – 1 ноября (сб–вс)"


def test_parse_offers_picks_cheapest_two_meal_plan():
    raw = [hotel(1, "Рёкан А", [(18000, 0, 0, "素泊まり"), (30000, 1, 1, "2食付き"), (26000, 1, 1, "早割2食")])]
    offers = parse_offers(raw, "Хаконэ", "2026-10-17", adults=2, two_meals=True)
    assert [(o.name, o.price, o.plan_name) for o in offers] == [("Рёкан А", 26000, "早割2食")]
    assert offers[0].url == "https://travel.rakuten.co.jp/1/早割2食" and offers[0].review == 4.4
    assert parse_offers(raw, "Хаконэ", "2026-10-17", 2, two_meals=False)[0].price == 18000


def test_room_price_variants():
    assert room_price({"total": 30000, "rakutenCharge": 15000, "chargeFlag": 0}, 2) == 30000
    assert room_price({"rakutenCharge": 15000, "chargeFlag": 0}, 2) == 30000   # за человека × 2
    assert room_price({"rakutenCharge": 15000, "chargeFlag": 1}, 2) == 15000   # за номер


class FakeApi:
    """prices[(hotel_no, суббота)] = цена 2食; отели — все из names."""

    def __init__(self, prices, names=None):
        self.prices = prices
        self.names = names or {}
        self.calls = []

    def search(self, area, checkin, adults, nights, page=1):
        self.calls.append((area.name, checkin))
        return [hotel(no, self.names.get(no, f"Рёкан {no}"), [(p, 1, 1, "2食付き")])
                for (no, sat), p in self.prices.items() if sat == checkin]


def weekends(no, this, others):
    sats = saturdays(TODAY, 6)
    return {(no, sats[0]): this, **{(no, s): others for s in sats[1:]}}


def run_hotels(cfg, api, store=None, now=NOW, force=False):
    cfg.hotels.areas = [HotelArea(name="Хаконэ-Юмото", lat=35.23, lon=139.10)]
    store = store or EventStore(":memory:")
    sender = PrintTelegram()
    report = check_hotels(cfg, store, sender, now, httpx.Client(), force=force, api=api)
    return report, sender, store


def test_ryokan_35k_to_10k_is_purple(cfg):
    api = FakeApi({**weekends(1, 10000, 35000), **weekends(2, 30000, 31000)}, names={1: "Рёкан Юмото"})
    report, sender, _ = run_hotels(cfg, api)
    assert report.sent == 1 and len(api.calls) == 6
    text, silent = sender.sent[0]
    assert text.startswith("🟣 ♨️ −71% · <b>Рёкан Юмото</b> · Хаконэ-Юмото") and not silent
    assert "📅 17–18 октября (сб–вс) · 2 взрослых · ужин и завтрак" in text
    assert "💴 10 000 ¥ вместо 35 000 ¥ (обычно на выходные) · −71%" in text
    assert "Забронировать на Rakuten Travel" in text and "google.com/maps" in text
    assert "\n\n" in text  # пустые строки между частями


def test_small_discounts_are_ignored(cfg):
    # −5% — меньше порога; −12%, но экономия 1 200 ¥ — меньше min_saving_yen
    api = FakeApi({**weekends(1, 28500, 30000), **weekends(2, 8800, 10000)})
    report, _, _ = run_hotels(cfg, api)
    assert report.sent == 0 and report.offers == 12


def test_color_tiers(cfg):
    api = FakeApi({**weekends(1, 26000, 30000), **weekends(2, 20000, 30000)})  # −13% 🟢, −33% 🟠
    _, sender, _ = run_hotels(cfg, api)
    heads = sorted(t.split(" · ")[0] for t, _ in sender.sent)
    assert heads == ["🟠 ♨️ −33%", "🟢 ♨️ −13%"]
    silent = {t.split(" · ")[0]: s for t, s in sender.sent}
    assert silent["🟢 ♨️ −13%"] is True  # зелёные — без звука, как события


def test_price_drop_vs_history_and_no_repeat(cfg):
    store = EventStore(":memory:")
    sats = saturdays(TODAY, 6)
    # первый запуск: везде по 30 000 — скидок нет, цены запомнены
    report, sender, store = run_hotels(cfg, FakeApi({(1, s): 30000 for s in sats}), store)
    assert report.sent == 0
    # вечером на ближайшую субботу подешевело до 20 000, на остальные — тоже упало (обычная цена не помогает)
    evening = NOW.replace(hour=19)
    api = FakeApi({(1, s): 20000 for s in sats})
    report, sender, store = run_hotels(cfg, api, store, now=evening)
    assert report.sent == 3  # три ближайшие субботы подешевели к прошлой цене
    assert "(раньше на эту дату)" in sender.sent[0][0]
    # то же самое ещё раз — не повторяем
    report, sender, store = run_hotels(cfg, api, store, now=evening + timedelta(hours=2), force=True)
    assert report.sent == 0
    # стало ещё дешевле — 🔴
    report, sender, store = run_hotels(cfg, FakeApi({(1, s): 15000 for s in sats}), store,
                                       now=evening + timedelta(hours=4), force=True)
    assert report.sent == 3 and all(t.startswith("🔴 Ещё дешевле") for t, _ in sender.sent)


def test_low_rated_hotels_skipped(cfg):
    api = FakeApi(weekends(1, 10000, 35000))
    api.search_orig = api.search
    def search(area, checkin, adults, nights, page=1):
        out = api.search_orig(area, checkin, adults, nights)
        for h in out:
            h["hotel"][0]["hotelBasicInfo"]["reviewAverage"] = 2.9
        return out
    api.search = search
    report, _, _ = run_hotels(cfg, api)
    assert report.sent == 0


def test_runs_only_at_configured_hours_and_needs_keys(cfg, monkeypatch):
    api = FakeApi(weekends(1, 10000, 35000))
    report, _, _ = run_hotels(cfg, api, now=NOW.replace(hour=13))
    assert report.checked == 0 and not api.calls
    report, _, _ = run_hotels(cfg, api, now=NOW.replace(hour=13), force=True)
    assert report.sent == 1
    monkeypatch.delenv("RAKUTEN_APPLICATION_ID", raising=False)
    monkeypatch.delenv("RAKUTEN_ACCESS_KEY", raising=False)
    cfg.hotels.areas = [HotelArea(name="x", lat=1, lon=1)]
    assert check_hotels(cfg, EventStore(":memory:"), PrintTelegram(), NOW, httpx.Client()).checked == 0


def test_rakuten_request_params_and_errors():
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.params["checkinDate"] == "2026-10-24":
            return httpx.Response(404, json={"error": "not_found"})
        if request.url.params["checkinDate"] == "2026-10-31":
            return httpx.Response(400, json={"error": "wrong_parameter", "error_description": "bad"})
        return httpx.Response(200, json={"hotels": [hotel(1, "A", [(20000, 1, 1, "p")])]})

    api = Rakuten("APP", "KEY", httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None)
    area = HotelArea(name="Хаконэ", lat=35.2, lon=139.1, radius_km=3, onsen=True)
    assert len(api.search(area, date(2026, 10, 17), 2, 1)) == 1
    q = seen[0].url.params
    assert (q["applicationId"], q["checkoutDate"], q["adultNum"], q["datumType"], q["squeezeCondition"],
            q["searchPattern"], q["sort"]) == ("APP", "2026-10-18", "2", "1", "onsen", "1", "+roomCharge")
    assert seen[0].headers["accessKey"] == "KEY"
    assert api.search(area, date(2026, 10, 24), 2, 1) == []  # свободных номеров нет
    try:
        api.search(area, date(2026, 10, 31), 2, 1)
        raise AssertionError("ожидалась ошибка")
    except Exception as e:
        assert "400 bad" in str(e)


def test_find_deals_needs_enough_other_weekends(cfg):
    from tokyo_events.hotels import Offer

    store = EventStore(":memory:")
    from tokyo_events.hotels import SCHEMA
    store.db.executescript(SCHEMA)
    sats = [s.isoformat() for s in saturdays(TODAY, 2)]
    offers = [Offer("1", "A", "x", sats[0], 10000, "", "u"), Offer("1", "A", "x", sats[1], 35000, "", "u")]
    assert find_deals(store.db, offers, cfg, TODAY) == []  # одна другая суббота — мало для «обычной цены»


def test_format_deal_escapes(cfg):
    from tokyo_events.hotels import Deal, Offer

    o = Offer("1", "Рёкан <A&B>", "Хаконэ", "2026-10-17", 10000, "план <1>", "https://x.jp/?a=1&b=2", review=4.5)
    text = format_deal(Deal(o, 35000, 71, "weekends", "purple"), cfg)
    assert "<b>Рёкан &lt;A&amp;B&gt;</b>" in text and 'href="https://x.jp/?a=1&amp;b=2"' in text
    assert "⭐ 4.5 · план: «план &lt;1&gt;»" in text and text.endswith("#отели #Хаконэ")
