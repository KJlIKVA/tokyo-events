from __future__ import annotations

import json
from datetime import timedelta

from tokyo_events.classify import Verdict, schema
from tokyo_events.config import Source
from tokyo_events.enrich import Details, Venue
from tokyo_events.sources import google_news_url, normalize_title, parse_feed, parse_page
from tokyo_events.state import Notified, State
from tokyo_events.telegram import format_message

from .conftest import NOW, item

RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>t</title>
<item><title>\xe7\xbe\x8e\xe6\xb3\xa2 Tour</title><link>https://www.373official.com/posts/1</link>
<description>&lt;p&gt;2027\xe5\xb9\xb41\xe6\x9c\x88 &amp;amp; more&lt;/p&gt;</description>
<pubDate>Fri, 02 Oct 2026 13:00:16 +0000</pubDate></item>
<item><title></title><link>https://x/2</link></item>
</channel></rss>"""

PAGE = """<html><body>
<a href="/news/detail/11837"><img src="a.jpg"></a>
<a href="/news/detail/11837"><span>2026.10.01</span> <b>ヨルシカ LIVE TOUR 2027 開催決定</b></a>
<a href="/news/detail/11836">新曲配信</a>
<a href="/news/5/?page=2">2</a>
<a href="https://yorushika.com/about">about</a>
</body></html>"""


def test_parse_feed_cleans_html_and_skips_empty():
    src = Source(id="minami_official", kind="rss", url="https://x", artist="美波")
    items = parse_feed(src, RSS)
    assert len(items) == 1
    it = items[0]
    assert it.title == "美波 Tour" and it.summary == "2027年1月 & more" and it.artist == "美波"
    assert it.published is not None and it.published.isoformat() == "2026-10-02T13:00:16+00:00"


def test_parse_page_takes_matching_links_with_longest_text():
    src = Source(id="yorushika_official", kind="page", url="https://yorushika.com/news",
                 link_pattern=r"/news/detail/\d+$", artist="ヨルシカ")
    items = parse_page(src, PAGE, "https://yorushika.com/news/5/")
    assert [(i.link, i.title) for i in items] == [
        ("https://yorushika.com/news/detail/11837", "2026.10.01 ヨルシカ LIVE TOUR 2027 開催決定"),
        ("https://yorushika.com/news/detail/11836", "新曲配信"),
    ]
    assert all(i.published is None and i.artist == "ヨルシカ" for i in items)


def test_google_news_url():
    assert google_news_url("美波 ライブ when:3d").startswith("https://news.google.com/rss/search?q=%E7%BE%8E")
    assert "ceid=JP%3Aen" in google_news_url("Tokyo", "en")


def test_normalize_title():
    assert normalize_title("画像29 / 45＞「グランスタ東京」がオープン！(1/5) - ウォーカープラス") == "「グランスタ東京」がオープン！"


def _v(rank="purple", category="concert", artist="美波"):
    return Verdict(id="n1", relevant=True, rank=rank, category=category, artist=artist, duplicate_of="",
                   update_of="", title_ru="", reason="")


def _d(**kw):
    base = dict(found=True, title_ru="美波 — тур <Timesick Traveler 2027>", dates_ru="январь 2027",
                date_precision="approximate", end_date="", price_ru="", is_free=False,
                venues=[], official_url="https://www.373official.com/posts/1?a=1&b=2",
                summary_ru="3 концерта в Японии & 5 городов Азии.")
    base.update(kw)
    return Details(**base)


def test_format_message_full_event():
    d = _d(venues=[Venue(name="上野の森美術館 (Ueno Royal Museum)", address="東京都台東区上野公園1-2", dates_ru="")],
           price_ru="взрослые 2 400 ¥", dates_ru="10 октября 2026 – 10 января 2027", title_ru="Ван Гог в Уэно")
    text = format_message(_v("orange", "exhibition", ""), item("x", link="https://news.google.com/rss/articles/a"), d)
    blocks = text.split("\n\n")  # между частями — пустая строка
    assert blocks[0] == "🟠 <b>Ван Гог в Уэно</b>"
    assert blocks[1] == "📅 10 октября 2026 – 10 января 2027"
    assert blocks[2] == "💴 взрослые 2 400 ¥"
    assert blocks[3] == ('🗺 <a href="https://www.google.com/maps/search/?api=1&amp;query='
                        '%E4%B8%8A%E9%87%8E%E3%81%AE%E6%A3%AE%E7%BE%8E%E8%A1%93%E9%A4%A8+%28Ueno+Royal+Museum%29+'
                        '%E6%9D%B1%E4%BA%AC%E9%83%BD%E5%8F%B0%E6%9D%B1%E5%8C%BA%E4%B8%8A%E9%87%8E%E5%85%AC%E5%9C%921-2">'
                        '上野の森美術館 (Ueno Royal Museum)</a>')
    assert blocks[4] == '🌐 <a href="https://www.373official.com/posts/1?a=1&amp;b=2">Официальный сайт</a>'
    assert blocks[5] == "3 концерта в Японии &amp; 5 городов Азии." and blocks[6] == "#выставка"
    assert "\n\n\n" not in text
    assert "news.google.com" not in text  # ссылки на новость нет, когда есть официальный сайт
    assert text.endswith("#выставка")


def test_format_message_escapes_free_tour_and_tags():
    d = _d(is_free=True, venues=[Venue(name="Tokyo Dome", address="", dates_ru="11 марта"),
                                 Venue(name="Osaka-jo Hall", address="", dates_ru="20 марта")])
    text = format_message(_v(), item("x"), d)
    assert text.splitlines()[0] == "🟣 <b>美波 — тур &lt;Timesick Traveler 2027&gt;</b>"
    assert "💴 Бесплатно" in text and "&amp; 5 городов" in text
    assert "query=Tokyo+Dome\">Tokyo Dome</a> — 11 марта\n🗺 " in text and "Osaka-jo Hall</a> — 20 марта" in text
    assert text.endswith("#концерт #美波")


def test_format_message_unknown_price_and_no_official_site():
    d = _d(official_url="", title_ru="")
    text = format_message(_v("green", "other", ""), item("Оригинальный заголовок", link="https://example.com/n"), d)
    assert text.splitlines()[0] == "🟢 <b>Оригинальный заголовок</b>"
    assert "💴 Цена пока не объявлена" in text
    assert '🔗 <a href="https://example.com/n">Подробнее: example.com</a>' in text


def test_format_message_update():
    assert format_message(_v(), item("x"), _d(), update=True).startswith("🟣 🔄 Обновление: <b>")


def test_state_roundtrip_and_prune(tmp_path):
    s = State()
    s.mark_seen("new", NOW)
    s.mark_seen("old", NOW - timedelta(days=90))
    s.notified.append(Notified(key="k", rank="green", title="t", when="", where="", artist="",
                               at=(NOW - timedelta(days=50)).isoformat()))
    s.notified.append(Notified(key="k2", rank="purple", title="t2", when="", where="", artist="Ado",
                               at=NOW.isoformat()))
    s.add_tokens("2026-10-13", 5)
    s.add_tokens("2026-09-01", 5)
    s.prune(NOW, seen_days=60, memory_days=45)
    path = tmp_path / "state" / "state.json"
    s.save(path)
    loaded = State.load(path)
    assert set(loaded.seen) == {"new"}
    assert [n.key for n in loaded.notified] == ["k2"]
    assert loaded.tokens == {"2026-10-13": 5}
    assert json.loads(path.read_text())["version"] == 1


def test_missing_state_file_is_empty(tmp_path):
    assert State.load(tmp_path / "nope.json").seen == {}


def test_schema_is_strict_and_lists_artists(cfg):
    s = schema([a.name for a in cfg.artists])
    v = s["properties"]["items"]["items"]
    assert v["additionalProperties"] is False and set(v["required"]) == set(v["properties"])
    assert v["properties"]["artist"]["enum"] == ["", "Ado", "YOASOBI", "RADWIMPS", "美波", "ヨルシカ"]


def test_prompt_has_rules_artists_memory_and_items(cfg, classifier):
    memory = [Notified(key="k1", rank="purple", title="Ado — тур", when="2027", where="", artist="Ado",
                       at=NOW.isoformat())]
    it = item("美波「Timesick Traveler 2027」ツアー開催決定", artist="美波", summary="国内3公演")
    prompt = classifier.build_prompt([("n1", it)], memory, NOW)
    assert "{" + "items" + "}" not in prompt and "{today}" not in prompt
    assert "Токио (23 района и район Тама)" in prompt
    assert "カワキヲアメク" in prompt and "актрисой 美波" in prompt  # различаем певицу и актрису
    assert "k1 | purple | Ado — тур | 2027 | ?" in prompt
    assert "id: n1\nзаголовок: 美波「Timesick Traveler 2027」ツアー開催決定\nтекст: 国内3公演" in prompt
    assert "официальный источник: 美波" in prompt
    assert 'Верни JSON: {"items": [...]}' in prompt


def test_config_sources_are_valid(cfg):
    assert len(cfg.sources) >= 10
    assert {s.artist for s in cfg.sources if s.artist} <= {a.name for a in cfg.artists}


def test_parse_chat_ids():
    from tokyo_events.telegram import parse_chat_ids

    assert parse_chat_ids("123") == ["123"]
    assert parse_chat_ids(" 123, -100456 ,") == ["123", "-100456"]


def test_telegram_sends_to_every_chat_and_reports_partial_failure():
    import httpx
    import pytest

    from tokyo_events.telegram import Telegram, TelegramError

    sent = []

    def handler(request):
        chat = json.loads(request.content)["chat_id"]
        sent.append(chat)
        if chat == "222":
            return httpx.Response(403, json={"ok": False, "error_code": 403, "description": "Forbidden"})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    tg = Telegram("t", ["111", "222"], client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert tg.send("привет") == ["чат …222: Telegram: 403 Forbidden"]
    assert sent == ["111", "222"]
    with pytest.raises(TelegramError):
        Telegram("t", ["222"], client=httpx.Client(transport=httpx.MockTransport(handler))).send("x")


def test_only_terra_model_is_allowed(cfg):
    import pytest
    from pydantic import ValidationError

    from tokyo_events.config import LLMConfig

    assert cfg.llm.model == "gpt-5.6-terra"
    with pytest.raises(ValidationError, match="не разрешена"):
        LLMConfig(model="gpt-5.6-luna")


def test_google_news_outlet_is_used_when_no_official_site():
    rss = """<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>ゴッホ展 - 美術手帖</title><link>https://news.google.com/rss/articles/abc</link>
<source url="https://bijutsutecho.com">美術手帖</source></item></channel></rss>""".encode()
    it = parse_feed(Source(id="gn", kind="google_news", q="x"), rss)[0]
    assert '<a href="https://news.google.com/rss/articles/abc">Подробнее: 美術手帖</a>' in \
        format_message(_v("orange", "exhibition", ""), it, _d(official_url=""))


def test_run_is_skipped_until_telegram_is_configured(monkeypatch, tmp_path, capsys):
    from tokyo_events import cli

    monkeypatch.setattr(cli, "load_dotenv", lambda: None)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setattr(cli, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("не должен запускаться")))
    state = tmp_path / "state.json"
    assert cli.main(["--state", str(state), "run"]) == 0
    assert "Telegram не настроен" in capsys.readouterr().out
    assert not state.exists()


def test_clear_chat_deletes_range_but_keeps_users_own_messages():
    import httpx

    from tokyo_events.telegram import Telegram

    deleted: list[int] = []

    def handler(request):
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content)
        if method == "getUpdates":
            return httpx.Response(200, json={"ok": True, "result": [
                {"message": {"message_id": 1, "chat": {"id": 111}}},   # /start пользователя
                {"message": {"message_id": 5, "chat": {"id": 222}}},   # другой чат
            ]})
        if method == "sendMessage":
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 250}})
        if method == "deleteMessages":
            assert len(body["message_ids"]) <= 100
            deleted.extend(body["message_ids"])
            return httpx.Response(200, json={"ok": True, "result": True})
        return httpx.Response(404, json={"ok": False})

    tg = Telegram("t", ["111"], client=httpx.Client(transport=httpx.MockTransport(handler)))
    keep = tg.own_messages_hint()
    assert keep == {"111": {1}, "222": {5}}
    assert tg.clear("111", keep["111"]) == 249
    assert deleted == list(range(2, 251))  # всё до служебного сообщения включительно, кроме /start


def test_place_without_map_is_plain_text():
    d = _d(venues=[Venue(name="田園都市線 (Den-en-toshi Line)", address="", area_ru="Токио", on_map=False,
                         dates_ru="с 11 октября"),
                   Venue(name="東京ドーム (Tokyo Dome)", address="", area_ru="Бункё, Токио", on_map=True, dates_ru="")])
    text = format_message(_v(), item("x"), d)
    assert "📍 田園都市線 (Den-en-toshi Line), Токио — с 11 октября" in text
    assert '🗺 <a href="https://www.google.com/maps/search/?api=1&amp;query=%E6%9D%B1' in text
    assert text.count("google.com/maps") == 1
