"""Сбор находок из источников: RSS, Google Новости, страницы новостей без ленты."""

from __future__ import annotations

import calendar
import hashlib
import html
import logging
import re
import urllib.parse
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser

import feedparser
import httpx

from .config import Artist, Source

log = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (compatible; tokyo-events/0.1; +https://github.com)"
SUMMARY_LIMIT = 500


@dataclass
class Item:
    source: str
    title: str
    link: str
    summary: str = ""
    published: datetime | None = None
    artist: str | None = None  # официальный источник артиста
    outlet: str = ""  # издание, если лента его называет (Google Новости — да)

    @property
    def key(self) -> str:
        """Ключ «уже видели»: по ссылке (одна статья в разных поисковых запросах — одна находка)."""
        return hashlib.sha1(self.link.strip().encode("utf-8")).hexdigest()[:16]

    @property
    def title_key(self) -> str:
        """Второй ключ — по заголовку: одна статья под разными ссылками (галереи «画像3 / 45＞…»)."""
        return "t" + hashlib.sha1(normalize_title(self.title).encode("utf-8")).hexdigest()[:15]


_GALLERY_PREFIX = re.compile(r"^[＜<]?\s*画像\s*\d+\s*/\s*\d+\s*[＞>]\s*")
_SOURCE_SUFFIX = re.compile(r"\s+-\s+[^-]{1,40}$")  # « - ウォーカープラス» в заголовках Google Новостей


def normalize_title(title: str) -> str:
    t = _GALLERY_PREFIX.sub("", title.strip())
    t = _SOURCE_SUFFIX.sub("", t)  # одна перепечатка в разных СМИ — одна находка
    t = re.sub(r"\(\d+/\d+\)", "", t)  # «(1/5)» — номер страницы статьи
    return re.sub(r"\s+", " ", t).strip().lower()


def clean_text(raw: str, limit: int = SUMMARY_LIMIT) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def google_news_url(q: str, lang: str = "ja") -> str:
    params = {"q": q, "hl": "ja", "gl": "JP", "ceid": "JP:ja"} if lang == "ja" else \
        {"q": q, "hl": "en-US", "gl": "JP", "ceid": "JP:en"}
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode(params)


def _entry_time(entry: dict) -> datetime | None:
    for field in ("published_parsed", "updated_parsed"):
        t = entry.get(field)
        if t:
            return datetime.fromtimestamp(calendar.timegm(t), tz=UTC)
    return None


def parse_feed(source: Source, content: bytes) -> list[Item]:
    feed = feedparser.parse(content)
    if feed.bozo and not feed.entries:
        raise ValueError(f"лента не разобрана: {type(feed.bozo_exception).__name__}")
    items = []
    for e in feed.entries:
        title = clean_text(e.get("title", ""), 300)
        link = (e.get("link") or "").strip()
        if not title or not link:
            continue
        items.append(Item(source=source.id, title=title, link=link,
                          summary=clean_text(e.get("summary", "") or e.get("description", "")),
                          published=_entry_time(e), artist=source.artist,
                          outlet=clean_text((e.get("source") or {}).get("title", ""), 60)))
    return items


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join(self._text)))
            self._href = None


def parse_page(source: Source, content: str, base_url: str) -> list[Item]:
    """Ссылки на новости со страницы. Дат нет — новизну определяет «ещё не видели»."""
    parser = _LinkParser()
    parser.feed(content)
    pattern = re.compile(source.link_pattern or "")
    items: dict[str, Item] = {}
    for href, text in parser.links:
        url = urllib.parse.urljoin(base_url, href.strip())
        title = clean_text(text, 300)
        if not pattern.search(url) or not title:
            continue
        if url in items:  # одна новость может быть ссылкой дважды (картинка + заголовок) — берём длинный текст
            if len(title) > len(items[url].title):
                items[url].title = title
            continue
        items[url] = Item(source=source.id, title=title, link=url, artist=source.artist)
    return list(items.values())


def fetch_source(source: Source, client: httpx.Client) -> list[Item]:
    url = google_news_url(source.q or "", source.lang) if source.kind == "google_news" else source.url
    assert url
    r = client.get(url, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
    r.raise_for_status()
    if source.kind == "page":
        return parse_page(source, r.text, str(r.url))
    return parse_feed(source, r.content)


# --- артисты ---------------------------------------------------------------------------

def _alias_pattern(alias: str) -> str:
    # латиницу — целым словом (Ado ≠ Adobe), японское — как подстроку (у японского нет пробелов)
    if re.fullmatch(r"[A-Za-z0-9 .\-]+", alias):
        return rf"(?<![A-Za-z]){re.escape(alias)}(?![A-Za-z])"
    return re.escape(alias)


def artist_matcher(artists: list[Artist]) -> re.Pattern[str]:
    return re.compile("|".join(_alias_pattern(a) for artist in artists for a in artist.aliases))


def mentions_artist(item: Item, matcher: re.Pattern[str]) -> bool:
    return bool(matcher.search(item.title) or matcher.search(item.summary))
