from __future__ import annotations

import inspect
import json
from datetime import date

import httpx

from tokyo_events.article import Article, Link, official_candidates, parse_article, resolve_google_news
from tokyo_events.classify import OpenAITransport, Verdict
from tokyo_events.enrich import Details, Enricher, Venue, maps_url, schema, url_alive
from tokyo_events.state import Notified
from tokyo_events.venue import VenueSites, clean_name

from .conftest import NOW, FakeTransport, details, item


def v(category="exhibition", artist=""):
    return Verdict(id="n1", relevant=True, rank="orange", category=category, artist=artist, duplicate_of="",
                   update_of="", title_ru="", reason="")


# --- никакого веб-поиска ---------------------------------------------------------------------------

def test_model_cannot_search_the_web():
    """Решение пользователя: поиск модели слишком дорогой. В вызове нет инструментов вообще."""
    src = inspect.getsource(OpenAITransport)
    assert "tools" not in src and "web_search" not in src
    assert "web_search" not in inspect.signature(OpenAITransport.__call__).parameters
    s = schema()
    assert "official_url" not in s["properties"]  # ссылки модель не возвращает


# --- статья ---------------------------------------------------------------------------

PAGE = """<html><head><title>シンシナティ美術館展 | koubo</title>
<meta property="og:description" content="上野の森美術館で開幕"></head><body>
<nav><a href="https://koubo.jp/top">トップ</a> メニュー</nav>
<article><h1>「シンシナティ美術館展」開幕</h1>
<p>会期：2026年10月10日～2027年1月10日<br>料金：一般2,400円</p>
<p>【公式サイト】<a href="https://www.cincinnati-art2026.jp/">https://www.cincinnati-art2026.jp/</a></p>
<p>特設ページ：https://special.example.jp/event/。</p>
<p><a href="https://twitter.com/x">公式X</a> <a href="https://koubo.jp/other">公式サイトはこちら</a>
<a href="https://prtimes.jp/main/1">詳細はこちら</a> <a href="https://museum.example.jp/">シンシナティ美術館展</a></p>
</article><footer><a href="https://ads.example.com">広告</a></footer></body></html>"""


def test_parse_article_takes_article_text_and_links():
    a = parse_article(PAGE, "https://koubo.jp/press/1")
    assert a.title == "シンシナティ美術館展 | koubo" and a.description == "上野の森美術館で開幕"
    assert "会期：2026年10月10日～2027年1月10日" in a.text and "料金：一般2,400円" in a.text
    assert "メニュー" not in a.text  # навигация не попадает
    assert ("特設ページ", "https://special.example.jp/event/") in a.plain_urls


def test_official_candidates_ranking_and_filters():
    a = parse_article(PAGE, "https://koubo.jp/press/1")
    c = official_candidates(a, "「シンシナティ美術館展」開幕 - koubo")
    assert c[0] == "https://www.cincinnati-art2026.jp/"         # «【公式サイト】» перед ссылкой
    assert "https://special.example.jp/event/" in c              # адрес текстом после «特設ページ：»
    assert "https://museum.example.jp/" in c                    # текст ссылки = название события
    assert not any(x in u for u in c for x in ("twitter.com", "koubo.jp", "prtimes.jp", "ads.example"))


def test_official_candidates_empty_when_article_has_no_links():
    assert official_candidates(Article(url="https://www.walkerplus.com/article/1/", text="текст")) == []


def test_link_label_variants():
    for before in ("公式サイト：", "【公式サイト】", "■特設サイト", "公式HP:", "URL："):
        a = Article(url="https://news.example/a", links=[Link("https://event.example.jp/", "https://event.example.jp/",
                                                             f"会期は… {before}")])
        assert official_candidates(a) == ["https://event.example.jp/"], before


def test_resolve_google_news():
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, text='<c-wiz><div data-n-a-sg="SIG" data-n-a-ts="123"></div></c-wiz>')
        body = request.content.decode()
        assert "Fbv4je" in body and "SIG" in body and "123" in body and "CBMiABC" in body
        inner = json.dumps(["garturlres", "https://bijutsutecho.com/magazine/news/1", 1])
        return httpx.Response(200, text=")]}'\n\n" + json.dumps([["wrb.fr", "Fbv4je", inner]]))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert resolve_google_news("https://news.google.com/rss/articles/CBMiABC?oc=5", client) == \
        "https://bijutsutecho.com/magazine/news/1"


def test_resolve_google_news_failure_is_none():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>нет подписи</html>")))
    assert resolve_google_news("https://news.google.com/rss/articles/X", client) is None


# --- сайт площадки (Wikidata) ---------------------------------------------------------------------------

def test_venue_site_from_wikidata():
    calls = []

    def handler(request):
        action = request.url.params["action"]
        calls.append(action)
        if action == "wbsearchentities":
            assert request.url.params["search"] == "上野の森美術館"
            return httpx.Response(200, json={"search": [{"id": "Q1"}, {"id": "Q2"}]})
        return httpx.Response(200, json={"entities": {
            "Q1": {"claims": {}},
            "Q2": {"claims": {"P856": [{"mainsnak": {"datavalue": {"value": "https://www.ueno-mori.org/"}}}]}}}})

    sites = VenueSites(httpx.Client(transport=httpx.MockTransport(handler)))
    assert sites.lookup("上野の森美術館 (Ueno Royal Museum)") == "https://www.ueno-mori.org/"
    assert sites.lookup("上野の森美術館（Ueno Royal Museum）") == "https://www.ueno-mori.org/"
    assert calls == ["wbsearchentities", "wbgetentities"]  # второй раз — из кэша


def test_clean_name():
    assert clean_name("東京ドーム (Tokyo Dome)") == "東京ドーム"


# --- шаг 2 целиком ---------------------------------------------------------------------------

def site(pages: dict[str, str], wikidata_site: str | None = None):
    """Мини-интернет: статьи, живые/мёртвые сайты и Wikidata."""
    def handler(request):
        url = str(request.url)
        if "wikidata.org" in url:
            if request.url.params["action"] == "wbsearchentities":
                return httpx.Response(200, json={"search": [{"id": "Q1"}] if wikidata_site else []})
            claims = {"P856": [{"mainsnak": {"datavalue": {"value": wikidata_site}}}]} if wikidata_site else {}
            return httpx.Response(200, json={"entities": {"Q1": {"claims": claims}}})
        for prefix, body in pages.items():
            if url.startswith(prefix):
                return httpx.Response(200, text=body, headers={"content-type": "text/html; charset=utf-8"})
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_official_site_comes_from_article(cfg):
    transport = FakeTransport()
    client = site({"https://koubo.jp/press/1": PAGE, "https://www.cincinnati-art2026.jp/": "ok"})
    d, _ = Enricher(cfg, transport, client).enrich(item("シンシナティ美術館展", link="https://koubo.jp/press/1"),
                                                     v(), None, NOW, 10**9)
    assert d.official_url == "https://www.cincinnati-art2026.jp/" and d.venue_url == ""
    assert d.source_url == "https://koubo.jp/press/1"
    assert "会期：2026年10月10日～2027年1月10日" in transport.enrich_prompts[0]  # модель читает статью


def test_dead_official_link_is_skipped_then_venue_site_used(cfg):
    transport = FakeTransport()
    transport.details = {"Выставка": details(venues=[{"name": "上野の森美術館 (Ueno Royal Museum)",
                                                     "address": "", "area_ru": "", "on_map": True, "dates_ru": ""}])}
    page = '<article><p>公式サイト：<a href="https://dead.example.jp/">https://dead.example.jp/</a></p></article>'
    client = site({"https://news.example/a": page, "https://www.ueno-mori.org/": "ok"},
                  wikidata_site="https://www.ueno-mori.org/")
    d, _ = Enricher(cfg, transport, client).enrich(item("Выставка", link="https://news.example/a"), v(), None, NOW,
                                                     10**9)
    assert d.official_url == "" and d.venue_url == "https://www.ueno-mori.org/"


def test_artist_official_source_is_its_own_official_page(cfg):
    transport = FakeTransport()
    client = site({"https://www.373official.com/posts/1": "<article>ツアー開催決定</article>"})
    it = item("美波 ツアー", link="https://www.373official.com/posts/1", artist="美波")
    d, _ = Enricher(cfg, transport, client).enrich(it, v("concert", "美波"), None, NOW, 10**9)
    assert d.official_url == "https://www.373official.com/posts/1"


def test_article_unavailable_model_gets_title_only(cfg):
    transport = FakeTransport()
    client = site({})
    d, _ = Enricher(cfg, transport, client).enrich(item("Выставка", link="https://news.example/404"), v(), None, NOW,
                                                     10**9)
    assert "(текст статьи недоступен — есть только заголовок)" in transport.enrich_prompts[0]
    assert d.source_url == "https://news.example/404"


def test_venues_capped(cfg):
    transport = FakeTransport()
    many = [{"name": f"Hall {i}", "address": "", "area_ru": "", "on_map": True, "dates_ru": ""} for i in range(12)] + \
        [{"name": " ", "address": "", "area_ru": "", "on_map": True, "dates_ru": ""}]
    transport.details = {"Тур": details(venues=many)}
    d, _ = Enricher(cfg, transport).enrich(item("Тур"), v(), None, NOW, 10**9)
    assert len(d.venues) == 8


def test_prompt_has_update_context(cfg):
    prev = Notified(key="k", rank="orange", title="Ван Гог", when="осень 2026", where="Уэно", artist="", at="")
    prompt = Enricher(cfg, FakeTransport()).build_prompt(item("ゴッホ展"), v(), prev, NOW, None)
    assert "заголовок: ゴッホ展" in prompt and "Тип события: exhibition." in prompt and "Сегодня 2026-10-13" in prompt
    assert "ОБНОВЛЕНИЕ уже отправленного события: «Ван Гог» (было: когда — осень 2026, где — Уэно)" in prompt
    assert "ничего не ищи" in prompt and "{" + "article}" not in prompt


def test_maps_url():
    assert maps_url(Venue(name="Tokyo Dome", address="1-3-61 Koraku, Bunkyo", dates_ru="")) == \
        "https://www.google.com/maps/search/?api=1&query=Tokyo+Dome+1-3-61+Koraku%2C+Bunkyo"


def test_url_alive():
    def handler(request):
        return {"/ok": httpx.Response(200), "/bot": httpx.Response(403)}.get(request.url.path, httpx.Response(404))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert url_alive("https://x.jp/ok", client) and url_alive("https://x.jp/bot", client)
    assert not url_alive("https://x.jp/missing", client) and not url_alive("javascript:alert(1)", client)


def test_dates_rules():
    base = details()
    assert Details(**base).has_dates
    assert Details(**{**base, "date_precision": "approximate", "dates_ru": "конец ноября 2026"}).has_dates
    assert not Details(**{**base, "date_precision": "year_only", "dates_ru": "2027 год"}).has_dates
    assert not Details(**{**base, "date_precision": "exact", "dates_ru": " "}).has_dates
    assert Details(**{**base, "end_date": "2026-10-12"}).ended(date(2026, 10, 13))
    assert not Details(**{**base, "end_date": "2026-10-13"}).ended(date(2026, 10, 13))
    assert not Details(**{**base, "end_date": "скоро"}).ended(date(2026, 10, 13))


def test_resolve_google_news_follows_redirect():
    """Регресс: клиент бота без follow_redirects — Google отвечает 302, подпись на следующей странице."""
    def handler(request):
        if request.method == "GET" and "/rss/articles/" in request.url.path:
            return httpx.Response(302, headers={"location": "https://news.google.com/articles/ABC"})
        if request.method == "GET":
            return httpx.Response(200, text='<div data-n-a-sg="S" data-n-a-ts="1"></div>')
        inner = json.dumps(["garturlres", "https://example.jp/a", 1])
        return httpx.Response(200, text=")]}'\n\n" + json.dumps([["wrb.fr", "Fbv4je", inner]]))

    client = httpx.Client(transport=httpx.MockTransport(handler))  # как в боте: без follow_redirects
    assert resolve_google_news("https://news.google.com/rss/articles/ABC", client) == "https://example.jp/a"


def test_walkerplus_gallery_goes_to_article():
    from tokyo_events.article import _canonical

    assert _canonical("https://www.walkerplus.com/article/1207539/image13293138.htm") == \
        "https://www.walkerplus.com/article/1207539/"
    assert _canonical("https://www.walkerplus.com/article/39894/image204729.html") == \
        "https://www.walkerplus.com/article/39894/"
