"""Сайт площадки без ИИ: по названию ищем объект в Wikidata и берём свойство «официальный сайт» (P856).

Нужен, когда в статье нет ссылки на официальный сайт события: тогда покажем сайт музея / стадиона / парка.
"""

from __future__ import annotations

import logging
import re

import httpx

log = logging.getLogger(__name__)

API = "https://www.wikidata.org/w/api.php"
UA = "tokyo-events/0.1 (https://github.com/KJlIKVA/tokyo-events)"  # Wikidata просит представляться


def clean_name(name: str) -> str:
    """«上野の森美術館 (Ueno Royal Museum)» → «上野の森美術館»."""
    return re.sub(r"\s*[（(][^)）]*[)）]\s*", " ", name).strip()


class VenueSites:
    def __init__(self, client: httpx.Client) -> None:
        self.client = client
        self.cache: dict[str, str | None] = {}

    def lookup(self, name: str) -> str | None:
        query = clean_name(name)
        if len(query) < 2:
            return None
        if query not in self.cache:
            self.cache[query] = self._lookup(query)
        return self.cache[query]

    def _lookup(self, query: str) -> str | None:
        try:
            found = self.client.get(API, headers={"User-Agent": UA}, params={
                "action": "wbsearchentities", "search": query, "language": "ja", "uselang": "ja", "type": "item",
                "limit": 3, "format": "json"}).json()
            ids = [x["id"] for x in found.get("search", [])]
            if not ids:
                return None
            entities = self.client.get(API, headers={"User-Agent": UA}, params={
                "action": "wbgetentities", "ids": "|".join(ids), "props": "claims", "format": "json"}).json()
        except (httpx.HTTPError, ValueError) as e:
            log.info("Wikidata: %s — %s", query, type(e).__name__)
            return None
        for entity_id in ids:  # по порядку релевантности поиска
            claims = entities.get("entities", {}).get(entity_id, {}).get("claims", {})
            for claim in claims.get("P856", []):
                url = claim.get("mainsnak", {}).get("datavalue", {}).get("value")
                if isinstance(url, str) and url.startswith(("http://", "https://")):
                    return url
        return None
