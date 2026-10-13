from __future__ import annotations

from datetime import date

import httpx

from tokyo_events.classify import Verdict
from tokyo_events.enrich import Enricher, Venue, maps_url, schema, url_alive
from tokyo_events.state import Notified

from .conftest import NOW, FakeTransport, details, item


def v():
    return Verdict(id="n1", relevant=True, rank="orange", category="exhibition", artist="", duplicate_of="",
                   update_of="", title_ru="", reason="")


def test_maps_url():
    assert maps_url(Venue(name="Tokyo Dome", address="1-3-61 Koraku, Bunkyo", dates_ru="")) == \
        "https://www.google.com/maps/search/?api=1&query=Tokyo+Dome+1-3-61+Koraku%2C+Bunkyo"


def test_schema_is_strict():
    s = schema()
    assert s["additionalProperties"] is False and set(s["required"]) == set(s["properties"])
    venue = s["properties"]["venues"]["items"]
    assert venue["additionalProperties"] is False and set(venue["required"]) == {"name", "address", "dates_ru"}


def test_url_alive():
    def handler(request):
        return {"/ok": httpx.Response(200), "/bot": httpx.Response(403)}.get(request.url.path, httpx.Response(404))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert url_alive("https://x.jp/ok", client) and url_alive("https://x.jp/bot", client)
    assert not url_alive("https://x.jp/missing", client)
    assert not url_alive("javascript:alert(1)", client)


def test_dead_official_url_is_dropped_and_venues_capped(cfg):
    transport = FakeTransport()
    many = [{"name": f"Hall {i}", "address": "", "dates_ru": ""} for i in range(12)] + \
        [{"name": " ", "address": "", "dates_ru": ""}]
    transport.details = {"Выставка": details(official_url="https://made-up.example/expo", venues=many)}
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    d, _ = Enricher(cfg, transport, client).enrich(item("Выставка"), v(), None, NOW, 10**9)
    assert d.official_url == "" and len(d.venues) == 8


def test_prompt_has_news_link_and_update_context(cfg):
    transport = FakeTransport()
    prev = Notified(key="k", rank="orange", title="Ван Гог", when="осень 2026", where="Уэно", artist="", at="")
    prompt = Enricher(cfg, transport).build_prompt(item("ゴッホ展", link="https://news.example/a"), v(), prev, NOW)
    assert "заголовок: ゴッホ展" in prompt and "ссылка на новость: https://news.example/a" in prompt
    assert "Тип события: exhibition." in prompt and "Сегодня 2026-10-13" in prompt
    assert "ОБНОВЛЕНИЕ уже отправленного события: «Ван Гог» (было: когда — осень 2026, где — Уэно)" in prompt
    assert "{" + "today}" not in prompt


def test_dates_rules():
    from tokyo_events.enrich import Details

    base = details()
    assert Details(**base).has_dates
    assert Details(**{**base, "date_precision": "approximate", "dates_ru": "конец ноября 2026"}).has_dates
    assert not Details(**{**base, "date_precision": "year_only", "dates_ru": "2027 год"}).has_dates
    assert not Details(**{**base, "date_precision": "exact", "dates_ru": " "}).has_dates
    assert Details(**{**base, "end_date": "2026-10-12"}).ended(date(2026, 10, 13))
    assert not Details(**{**base, "end_date": "2026-10-13"}).ended(date(2026, 10, 13))
    assert not Details(**{**base, "end_date": "скоро"}).ended(date(2026, 10, 13))
