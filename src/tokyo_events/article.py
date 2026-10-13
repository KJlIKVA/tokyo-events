"""Статья без ИИ: раскрыть ссылку Google Новостей, скачать страницу, достать текст и ссылку на официальный сайт.

Официальный сайт ищется по самой статье: японские СМИ почти всегда пишут «公式サイト：…» / «特設サイト» /
«詳細は…» рядом со ссылкой. Модель в выборе ссылок не участвует.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.parse
from dataclasses import dataclass, field
from html.parser import HTMLParser

import httpx

log = logging.getLogger(__name__)

BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/126.0 Safari/537.36")
MAX_HTML = 3_000_000

# соцсети, магазины, агрегаторы, реклама — это не официальный сайт события
NOT_OFFICIAL = (
    "twitter.com", "x.com", "facebook.com", "instagram.com", "youtube.com", "youtu.be", "tiktok.com", "line.me",
    "lin.ee", "google.com", "goo.gl", "g.co", "apple.com", "amazon.co.jp", "amazon.com", "rakuten.co.jp",
    "yahoo.co.jp", "news.google.com", "prtimes.jp", "atpress.ne.jp", "value-press.com", "hatena.ne.jp",
    "pinterest.com", "threads.net", "note.com", "ameblo.jp", "doubleclick.net", "a8.net", "valuecommerce.com",
    "linksynergy.com", "accesstrade.net", "moshimo.com", "afi-b.com", "addtoany.com", "wikipedia.org",
    "walkerplus.com", "timeout.jp", "timeout.com", "fashion-press.net", "natalie.mu", "oricon.co.jp",
    "bijutsutecho.com", "tokyoartbeat.com", "jorudan.co.jp", "navitime.co.jp", "tabelog.com", "jalan.net",
    "docomo.ne.jp", "livedoor.com", "infoseek.co.jp", "excite.co.jp", "nikkei.com", "asahi.com", "yomiuri.co.jp",
    "mainichi.jp", "sankei.com", "nhk.or.jp", "ticketjam.jp", "impress.co.jp", "traicy.com", "travel.co.jp",
    "travelvoice.jp", "tabikobo.com", "trip.com", "tripadvisor.jp", "tripadvisor.com", "jalan.net",
)
FILE_EXT = re.compile(r"\.(jpe?g|png|gif|webp|svg|pdf|mp4|zip)(\?|$)", re.I)
OFFICIAL_WORDS = re.compile(r"公式サイト|公式HP|公式ホームページ|公式ウェブサイト|オフィシャルサイト|"
                            r"特設サイト|特設ページ|公式ページ|official\s*(web)?\s*site|website", re.I)
# подпись прямо перед ссылкой: «公式サイト：», «【公式サイト】», «■特設サイト»
LABEL_BEFORE = re.compile(r"(公式サイト|公式HP|公式ホームページ|特設サイト|オフィシャルサイト|公式|URL|HP|ホームページ|"
                          r"Website|Official)\s*[】\]）)>]?\s*[:：]?\s*$", re.I)
# адрес прямо в тексте: «公式サイト：https://…»
PLAIN_URL = re.compile(r"(公式サイト|公式HP|公式ホームページ|公式ページ|特設サイト|特設ページ|"
                       r"オフィシャルサイト|URL|HP)\s*[】\]]?\s*[:：]?\s*"
                       r"(https?://[^\s<>「」）)]+)")
DETAIL_WORDS = re.compile(r"詳細|詳しく|イベント情報|展覧会情報|公演情報|チケット|ticket|more info|details", re.I)
SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "nav", "header", "footer", "aside", "form", "button",
             "select", "iframe"}
BLOCK_TAGS = {"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "dt", "dd", "table", "section",
              "article", "figcaption", "blockquote"}


@dataclass
class Link:
    url: str
    text: str
    before: str  # текст прямо перед ссылкой (там бывает «公式サイト：»)


@dataclass
class Article:
    url: str
    title: str = ""
    description: str = ""
    text: str = ""
    links: list[Link] = field(default_factory=list)
    plain_urls: list[tuple[str, str]] = field(default_factory=list)  # (подпись, url) из текста «公式サイト：https://…»


# --- Google Новости ---------------------------------------------------------------------------

def resolve_google_news(url: str, client: httpx.Client) -> str | None:
    """Настоящий адрес статьи за ссылкой news.google.com/rss/articles/… (тем же способом, что страница Google
    в браузере). Не получилось — None."""
    art_id = urllib.parse.urlparse(url).path.rsplit("/", 1)[-1]
    if not art_id:
        return None
    try:
        # follow_redirects обязательно: Google отвечает перенаправлением на страницу статьи
        page = client.get(f"https://news.google.com/rss/articles/{art_id}", headers={"User-Agent": BROWSER_UA},
                          follow_redirects=True).text
        sg = re.search(r'data-n-a-sg="([^"]+)"', page)
        ts = re.search(r'data-n-a-ts="([^"]+)"', page)
        if not sg or not ts:
            log.info("Google Новости: на странице нет подписи для раскрытия ссылки")
            return None
        inner = (f'["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,null,null,null,null,0,1],'
                 f'"X","X",1,[1,1,1],1,1,null,0,0,null,0],"{art_id}",{ts.group(1)},"{sg.group(1)}"]')
        r = client.post("https://news.google.com/_/DotsSplashUi/data/batchexecute",
                        headers={"content-type": "application/x-www-form-urlencoded;charset=UTF-8",
                                 "User-Agent": BROWSER_UA},
                        content="f.req=" + urllib.parse.quote(json.dumps([[["Fbv4je", inner]]])),
                        follow_redirects=True)
        data = json.loads(r.text.split("\n\n")[1])
        real = json.loads(data[0][2])[1]
    except (httpx.HTTPError, ValueError, IndexError, KeyError, TypeError) as e:
        log.info("Google Новости: не раскрыл ссылку (%s)", type(e).__name__)
        return None
    if not (isinstance(real, str) and real.startswith("http")):
        log.info("Google Новости: неожиданный ответ при раскрытии ссылки")
        return None
    return real


# --- страница ---------------------------------------------------------------------------

def _decode(content: bytes, header_charset: str | None) -> str:
    charset = header_charset
    if not charset:
        m = re.search(rb'<meta[^>]+charset=["\']?([\w-]+)', content[:4096], re.I)
        charset = m.group(1).decode("ascii", "ignore") if m else "utf-8"
    try:
        return content.decode(charset, errors="replace")
    except LookupError:
        return content.decode("utf-8", errors="replace")


class _PageParser(HTMLParser):
    """Текст и ссылки страницы. Если есть <article> (или <main>) — берём только его: там сама статья."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.scopes = {"article": 0, "main": 0}
        self.parts: dict[str, list[str]] = {"article": [], "main": [], "body": []}
        self.links: dict[str, list[Link]] = {"article": [], "main": [], "body": []}
        self.href: str | None = None
        self.href_text: list[str] = []
        self.href_before = ""
        self.meta: dict[str, str] = {}
        self.title = ""
        self._in_title = False

    def _targets(self) -> list[str]:
        return [s for s in ("article", "main") if self.scopes[s]] + ["body"]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or "" for k, v in attrs}
        if tag == "meta":
            key = a.get("property") or a.get("name")
            if key in ("og:description", "description", "og:title") and a.get("content"):
                self.meta.setdefault(key, a["content"])
            return
        if tag == "title":
            self._in_title = True
        if tag in SKIP_TAGS:
            self.skip += 1
        if tag in self.scopes:
            self.scopes[tag] += 1
        if tag in BLOCK_TAGS:
            self._add("\n")
        if tag == "a" and not self.skip:
            self.href = a.get("href")
            self.href_text = []
            self.href_before = "".join(self.parts["body"])[-40:]

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag in SKIP_TAGS and self.skip:
            self.skip -= 1
        if self.scopes.get(tag):
            self.scopes[tag] -= 1
        if tag == "a" and self.href is not None:
            link = Link(self.href, re.sub(r"\s+", " ", "".join(self.href_text)).strip(),
                        re.sub(r"\s+", " ", self.href_before).strip())
            for t in self._targets():
                self.links[t].append(link)
            self.href = None
        if tag in BLOCK_TAGS:
            self._add("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        if self.skip:
            return
        self._add(data)
        if self.href is not None:
            self.href_text.append(data)

    def _add(self, s: str) -> None:
        for t in self._targets():
            self.parts[t].append(s)


def parse_article(html: str, url: str, max_chars: int = 5000) -> Article:
    p = _PageParser()
    p.feed(html)
    scope = next(s for s in ("article", "main", "body") if "".join(p.parts[s]).strip())  \
        if any("".join(v).strip() for v in p.parts.values()) else "body"
    raw = "".join(p.parts[scope])
    text = re.sub(r"[ \t　]+", " ", raw)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    links = []
    for link in p.links[scope]:
        absolute = urllib.parse.urljoin(url, (link.url or "").strip())
        if absolute.startswith(("http://", "https://")):
            links.append(Link(absolute, link.text, link.before))
    plain = [(m.group(1), m.group(2).rstrip(".,、。）)」")) for m in PLAIN_URL.finditer(text)]
    return Article(url=url, title=(p.meta.get("og:title") or p.title).strip(),
                   description=(p.meta.get("og:description") or p.meta.get("description") or "").strip(),
                   text=text[:max_chars], links=links, plain_urls=plain)


def fetch_article(url: str, client: httpx.Client, max_chars: int = 5000) -> Article | None:
    if urllib.parse.urlparse(url).hostname == "news.google.com":
        real = resolve_google_news(url, client)
        if not real:
            return None
        url = real
    url = _canonical(url)
    try:
        r = client.get(url, headers={"User-Agent": BROWSER_UA, "Accept-Language": "ja,en;q=0.8"},
                       follow_redirects=True)
        r.raise_for_status()
    except httpx.HTTPError as e:
        log.info("статья %s не скачалась (%s)", url, type(e).__name__)
        return None
    if "html" not in r.headers.get("content-type", "html"):
        log.info("статья %s — не HTML (%s)", url, r.headers.get("content-type"))
        return None
    html = _decode(r.content[:MAX_HTML], r.charset_encoding)
    return parse_article(html, str(r.url), max_chars)


def _canonical(url: str) -> str:
    # Walker+ отдаёт галерею «/article/39894/image204729.html» — сама статья по «/article/39894/»
    return re.sub(r"(walkerplus\.com/article/\d+/)image\d+\.html?$", r"\1", url)


# --- официальный сайт ---------------------------------------------------------------------------

def _host(url: str) -> str:
    return (urllib.parse.urlparse(url).hostname or "").lower().removeprefix("www.")


def _site(host: str) -> str:
    """Регистрируемый домен: news.yahoo.co.jp → yahoo.co.jp, bijutsutecho.com → bijutsutecho.com."""
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "or", "ne", "ac", "go", "gr", "ed", "lg", "com", "org", "net"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _blocked(host: str) -> bool:
    return any(host == d or host.endswith("." + d) for d in NOT_OFFICIAL)


def _overlaps_title(text: str, title: str) -> bool:
    """Текст ссылки — название события из заголовка («シンシナティ美術館展»)."""
    names = re.findall(r"[「『“\"]([^」』”\"]{4,40})[」』”\"]", title)
    t = re.sub(r"\s+", "", text)
    return len(t) >= 4 and any(n.replace(" ", "") in t or t in n.replace(" ", "") for n in names)


def official_candidates(article: Article, title: str = "", exclude: tuple[str, ...] = ()) -> list[str]:
    """Ссылки, похожие на официальный сайт события, от самой вероятной. Только другие сайты, не само СМИ
    (и не издание из exclude — например, оригинал перепечатки Yahoo), не картинки и не файлы."""
    own = {_site(_host(article.url)), *(_site(_host(u)) for u in exclude if u)}
    title = title or article.title
    scored: dict[str, int] = {}

    def add(url: str, score: int) -> None:
        host = _host(url)
        if not host or _blocked(host) or _site(host) in own or FILE_EXT.search(url) or score <= 0:
            return
        scored[url] = max(scored.get(url, 0), score)

    for label, url in article.plain_urls:
        add(url, 10 if label != "URL" and label != "HP" else 8)
    for link in article.links:
        score = 0
        if OFFICIAL_WORDS.search(link.text):
            score = 10
        elif LABEL_BEFORE.search(link.before) and (link.text.startswith("http") or len(link.text) < 40):
            score = 9
        elif "公式" in link.text:
            score = 8
        elif _overlaps_title(link.text, title):
            score = 6
        elif DETAIL_WORDS.search(link.text):
            score = 4
        add(link.url, score)
    return [u for u, _ in sorted(scored.items(), key=lambda kv: -kv[1])]
