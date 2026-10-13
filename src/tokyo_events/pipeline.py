"""Один запуск: собрать → отсеять старое и виденное → отобрать моделью → найти подробности → в Telegram."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

import httpx

from .classify import BudgetExceeded, Classifier, LLMError, Verdict
from .config import Config, Source
from .enrich import Details, Enricher
from .events_db import Event, EventStore, diff
from .sources import Item, artist_matcher, fetch_source, mentions_artist
from .state import State
from .telegram import TelegramError, format_message, format_update

log = logging.getLogger(__name__)


class Sender(Protocol):
    def send(self, text: str, silent: bool = False) -> list[str]:
        """Список проблем с частью получателей; не дошло никуда — TelegramError."""


Fetcher = Callable[[Source], list[Item]]


@dataclass
class Report:
    fetched: int = 0
    new: int = 0
    candidates: int = 0
    sent: dict[str, int] = field(default_factory=lambda: {"green": 0, "orange": 0, "purple": 0})
    skipped: int = 0
    duplicates: int = 0
    no_dates: int = 0
    updates: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        sent = ", ".join(f"{k}={v}" for k, v in self.sent.items())
        return (f"собрано {self.fetched}, новых {self.new}, на оценку {self.candidates}; отправлено: {sent}, "
                f"обновлений 🔴: {self.updates}; "
                f"не событие/не подходит: {self.skipped}, дублей: {self.duplicates}, без чётких дат: {self.no_dates}; "
                f"ошибок: {len(self.errors)}")


def http_fetcher(client: httpx.Client) -> Fetcher:
    return lambda source: fetch_source(source, client)


def collect(cfg: Config, state: State, fetch: Fetcher, now: datetime, report: Report) -> list[Item]:
    """Новые находки, которые стоит показать модели. Остальное молча помечается просмотренным."""
    matcher = artist_matcher(cfg.artists)
    candidates: dict[str, Item] = {}
    title_keys: set[str] = set()

    def seen(it: Item) -> bool:
        return state.is_seen(it.key) or state.is_seen(it.title_key) or it.key in candidates \
            or it.title_key in title_keys

    def forget(it: Item) -> None:
        _mark_seen(state, it, now)

    for source in cfg.sources:
        try:
            items = fetch(source)
        except Exception as e:  # один упавший источник не должен ронять остальные
            msg = f"источник {source.id}: {type(e).__name__}: {str(e)[:200]}"
            log.warning(msg)
            report.errors.append(msg)
            continue
        report.fetched += len(items)
        fresh = []
        for it in items:
            if not seen(it):
                fresh.append(it)
                title_keys.add(it.title_key)  # дубль заголовка внутри одной ленты тоже отсеиваем
        report.new += len(fresh)
        first_time = source.id not in state.sources_ready
        # первый запуск источника: не вываливать всё за max_age_hours, только самое свежее
        max_age = timedelta(hours=cfg.filters.first_run_hours if first_time else cfg.filters.max_age_hours)
        undated_new = [it for it in fresh if it.published is None]
        # без дат «новое» = «не видели»: при первом запуске или перестройке сайта это всё старое
        baseline_undated = first_time or len(undated_new) > cfg.filters.max_new_undated
        if undated_new and baseline_undated and not first_time:
            log.warning("источник %s: разом %d новых ссылок без дат — похоже на перестройку сайта, запоминаю молча",
                        source.id, len(undated_new))
        for it in fresh:
            stale = baseline_undated if it.published is None else now - it.published > max_age
            off_topic = source.artists_only and not mentions_artist(it, matcher)
            if stale or off_topic:
                forget(it)
            else:
                candidates[it.key] = it
        if first_time:
            state.sources_ready.append(source.id)
    # сначала свежие; без даты — как самые свежие (на страницах без дат это только что появившиеся ссылки)
    return sorted(candidates.values(), key=lambda it: it.published or now, reverse=True)


def run(cfg: Config, state: State, store: EventStore, fetch: Fetcher, classifier: Classifier | None,
        enricher: Enricher | None, sender: Sender, now: datetime | None = None) -> Report:
    now = (now or datetime.now(UTC)).astimezone(ZoneInfo(cfg.timezone))
    report = Report()
    candidates = collect(cfg, state, fetch, now, report)
    if len(candidates) > cfg.llm.max_items_per_run:
        log.info("на оценку %d находок, берём %d свежих, остальные — в следующий раз",
                 len(candidates), cfg.llm.max_items_per_run)
        candidates = candidates[: cfg.llm.max_items_per_run]
    report.candidates = len(candidates)
    if candidates and (classifier is None or enricher is None):
        report.errors.append("нет OPENAI_API_KEY — находки не оценены и подождут следующего запуска")
        return _finish(cfg, state, store, now, report)
    assert classifier is not None and enricher is not None

    ctx = _Ctx(cfg, state, store, enricher, sender, now, report)
    try:
        for start in range(0, len(candidates), cfg.llm.batch_size):
            batch = candidates[start: start + cfg.llm.batch_size]
            ids = [(f"n{start + i + 1}", it) for i, it in enumerate(batch)]
            try:
                verdicts, tokens = classifier.classify(ids, store.recent(cfg.filters.memory_events), now,
                                                       ctx.tokens_left())
            except LLMError as e:
                if isinstance(e, BudgetExceeded):
                    raise
                report.errors.append(f"модель: {e}")
                log.warning("модель: %s", e)
                continue
            ctx.spend(tokens)
            ctx.handle_batch(verdicts, dict(ids))
    except BudgetExceeded as e:
        report.errors.append(str(e))
        log.warning("%s — остальное подождёт следующего запуска", e)
    return _finish(cfg, state, store, now, report)


def _mark_seen(state: State, item: Item, now: datetime) -> None:
    state.mark_seen(item.key, now)
    state.mark_seen(item.title_key, now)


def _effective_rank(v: Verdict) -> str:
    # правило пользователя: концерт любимого артиста — всегда фиолетовый
    if v.relevant and v.artist and v.category == "concert":
        return "purple"
    return v.rank if v.relevant else "none"


def deal_rank(pct: int, cfg: Config) -> str | None:
    """Цвет скидки по размеру: от 10% — 🟢, от 30% — 🟠, от 50% — 🟣 (пороги в config.yaml → deals)."""
    t = cfg.deals
    if pct >= t.purple_pct:
        return "purple"
    if pct >= t.orange_pct:
        return "orange"
    if pct >= t.min_pct:
        return "green"
    return None


def _location_ok(v: Verdict, rank: str, location: str) -> bool:
    """Правило пользователя: 🟢 и 🟠 — только Токио; 🟣 за масштаб и скидки для поездок — вся Япония;
    концерты своих артистов — где угодно."""
    if v.artist and v.category == "concert":
        return True
    if rank == "purple" or v.category == "deal":  # скидки для поездок — по всей Японии
        return location in ("tokyo", "japan", "unknown")
    return location in ("tokyo", "unknown")


@dataclass
class _Ctx:
    cfg: Config
    state: State
    store: EventStore
    enricher: Enricher
    sender: Sender
    now: datetime
    report: Report
    enriched: int = 0
    checked: set[str] = field(default_factory=set)  # события, которые уже сверили в этом запуске

    def _day(self) -> str:
        return self.now.astimezone(UTC).date().isoformat()

    def tokens_left(self) -> int:
        return self.cfg.llm.daily_token_limit - self.state.tokens_on(self._day())

    def spend(self, tokens: int) -> None:
        self.state.add_tokens(self._day(), tokens)

    def handle_batch(self, verdicts: list[Verdict], items: dict[str, Item]) -> None:
        notify = set(self.cfg.filters.notify_ranks)
        wanted = {v.id for v in verdicts if _effective_rank(v) in notify}
        dropped: set[str] = set()  # id находок пакета, отброшенных как дубли
        for v in verdicts:
            self._handle(v, items[v.id], wanted, dropped)

    def _is_batch_duplicate(self, v: Verdict, wanted: set[str], dropped: set[str]) -> bool:
        ref = v.duplicate_of.strip()
        if not ref or ref == v.id:
            return False
        if ref.startswith("n") and ref[1:].isdigit():
            # дубль находки этого же пакета — независимо от порядка: «оригинал» отправится сам,
            # если он подходит и сам не отброшен как дубль (так из пары взаимных ссылок останется одна)
            return ref in wanted and ref not in dropped
        log.warning("непонятная ссылка на дубль %r — считаю дублем, как решила модель", ref)
        return True

    def _enrich(self, v: Verdict, item: Item, previous: Event | None) -> Details | None:
        """Подробности; None — не вышло (лимит, ошибка): находка подождёт следующего запуска."""
        if self.enriched >= self.cfg.llm.enrich.max_per_run:
            log.info("лимит подробностей за запуск — «%s» подождёт следующего запуска", item.title[:60])
            return None
        try:
            details, tokens = self.enricher.enrich(item, v, previous, self.now, self.tokens_left())
        except LLMError as e:
            if isinstance(e, BudgetExceeded):
                raise
            self.report.errors.append(f"модель: {e}")
            log.warning("подробности «%s»: %s", item.title[:60], e)
            return None
        self.enriched += 1
        self.spend(tokens)
        return details

    def _send(self, text: str, silent: bool) -> bool:
        try:
            problems = self.sender.send(text, silent=silent)
        except TelegramError as e:
            self.report.errors.append(str(e))
            log.warning("%s", e)
            return False  # не помечаем просмотренным: попробуем в следующий запуск
        # часть аккаунтов не получила — сообщаем, но не повторяем: остальным пришёл бы дубль
        self.report.errors += [f"Telegram: {p}" for p in problems]
        return True

    def _handle(self, v: Verdict, item: Item, wanted: set[str], dropped: set[str]) -> None:
        cfg, state, store, now, report = self.cfg, self.state, self.store, self.now, self.report
        rank = _effective_rank(v)
        log.info("[%s] %s — %s (%s)%s%s", rank, item.title[:80], v.reason, item.source,
                 f" дубль={v.duplicate_of}" if v.duplicate_of else "",
                 f" обновление={v.update_of}" if v.update_of else "")
        if rank == "none" or rank not in cfg.filters.notify_ranks:
            report.skipped += 1
            _mark_seen(state, item, now)
            return

        # событие уже в базе: сверяем (если модель видит новое) или молча записываем ещё один источник
        known = store.ids()
        upd, dup = v.update_of.strip(), v.duplicate_of.strip()
        event_id = upd if upd in known else dup if dup in known else ""
        if event_id:
            if event_id == upd and event_id not in self.checked:
                self._check_update(v, item, event_id)
            else:
                report.duplicates += 1
                store.add_source(event_id, item.key, item.link, item.title, "duplicate", now)
                _mark_seen(state, item, now)
            return
        if self._is_batch_duplicate(v, wanted, dropped):
            report.duplicates += 1
            dropped.add(v.id)
            _mark_seen(state, item, now)
            return
        self._new_event(v, item, rank)

    def _new_event(self, v: Verdict, item: Item, rank: str) -> None:
        cfg, state, now, report = self.cfg, self.state, self.now, self.report
        details = self._enrich(v, item, None)
        if details is None:
            return
        if not details.found:
            log.info("   не подтвердилось — пропуск")
            report.skipped += 1
            _mark_seen(state, item, now)
            return
        if not details.has_dates:
            # «2027 год» — слишком размыто: пропускаем; когда объявят даты, придёт новая новость
            log.info("   нет чётких дат (%s: %s) — ждём, пока объявят", details.date_precision, details.dates_ru)
            report.no_dates += 1
            _mark_seen(state, item, now)
            return
        if v.category == "deal":
            # скидка: цвет — по размеру; меньше порога (или процент неизвестен) — не присылаем
            rank = deal_rank(details.discount_pct, cfg) or ""
            if not rank:
                log.info("   скидка %d%% — меньше порога %d%%, пропуск", details.discount_pct, cfg.deals.min_pct)
                report.skipped += 1
                _mark_seen(state, item, now)
                return
        where_ok = _location_ok(v, rank, details.location)
        if not where_ok:
            log.info("   не то место для ранга %s (%s) — пропуск", rank, details.location)
            report.skipped += 1
            _mark_seen(state, item, now)
            return
        # открытие: end_date — день открытия; недавно открывшееся (до 2 недель) — всё ещё новость
        grace = timedelta(days=14) if v.category == "opening" else timedelta(0)
        if details.ended(now.date() - grace):
            log.info("   уже закончилось (%s) — пропуск", details.end_date)
            report.skipped += 1
            _mark_seen(state, item, now)
            return
        v = v.model_copy(update={"rank": rank})
        if not self._send(format_message(v, item, details), silent=rank in cfg.filters.silent_ranks):
            return
        _mark_seen(state, item, now)
        at = now.isoformat(timespec="seconds")
        self.store.add(Event(
            id=item.key, rank=rank, category=v.category, artist=v.artist,
            title=details.title_ru or v.title_ru or item.title, dates_ru=details.dates_ru,
            date_precision=details.date_precision, start_date=details.start_date, end_date=details.end_date,
            price_ru=details.price_ru, is_free=details.is_free, status=details.status,
            venues=[x.model_dump() for x in details.venues], official_url=details.official_url,
            venue_url=details.venue_url, first_sent_at=at, updated_at=at))
        self.store.add_source(item.key, item.key, item.link, item.title, "new", now)
        report.sent[rank] += 1

    def _check_update(self, v: Verdict, item: Item, event_id: str) -> None:
        """Сверить новость с базой: изменились даты, цена, площадка, статус — 🔴 обновление."""
        state, store, now, report = self.state, self.store, self.now, self.report
        self.checked.add(event_id)
        event = store.get(event_id)
        assert event is not None
        details = self._enrich(v, item, event)
        if details is None:
            return
        new = details.model_dump()
        changes = diff(event, new) if details.found else []
        if not changes:
            log.info("   нового по сравнению с базой нет — не присылаем")
            report.duplicates += 1
            if details.found:
                store.apply(event_id, new, [], now)  # дозаполнить пустые поля (ссылки и т.п.)
            store.add_source(event_id, item.key, item.link, item.title, "duplicate", now)
            _mark_seen(state, item, now)
            return
        log.info("   🔴 обновление: %s", "; ".join(f"{c.label}: {c.old or '—'} → {c.new}" for c in changes))
        if not self._send(format_update(event, v, item, details, changes), silent=False):
            return
        store.apply(event_id, new, changes, now)
        store.add_source(event_id, item.key, item.link, item.title, "update", now)
        _mark_seen(state, item, now)
        report.updates += 1


def _finish(cfg: Config, state: State, store: EventStore, now: datetime, report: Report) -> Report:
    state.prune(now, cfg.filters.seen_days, cfg.filters.memory_days)
    store.prune(now.date())
    log.info("итог: %s", report.summary())
    return report
