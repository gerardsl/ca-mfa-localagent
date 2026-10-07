"""Tools the agent uses to check the news online (Google News RSS feeds)."""
from __future__ import annotations

import calendar
import html
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote_plus

import feedparser
import requests

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SpainNewsAgent/1.0"

# Google News editions: international English-language coverage vs. Spain's national press.
EDITIONS = {
    "english": "hl=en-US&gl=US&ceid=US:en",
    "spanish": "hl=es&gl=ES&ceid=ES:es",
}


@dataclass
class Article:
    id: str
    title: str
    source: str
    published: datetime | None
    link: str
    related_coverage: int
    related_headlines: str

    def for_llm(self) -> dict:
        item = {
            "id": self.id,
            "title": self.title,
            "source": self.source,
            "published": self.published.strftime("%Y-%m-%d %H:%M UTC") if self.published else "unknown",
        }
        if self.related_coverage > 1:
            item["outlets_covering_story"] = self.related_coverage
        if self.related_headlines:
            item["related_headlines"] = self.related_headlines
        return item


def _clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def normalize_title(title: str) -> str:
    return re.sub(r"[^\w]+", " ", title.lower()).strip()


class NewsDesk:
    """Fetches live news and remembers every article seen in this run by a short id (A1, A2...),
    so the model can reference articles without copying long URLs."""

    def __init__(self, timeout: int = 20):
        self.timeout = timeout
        self.articles: dict[str, Article] = {}
        self._by_title: dict[str, Article] = {}

    def get_spain_headlines(self, edition: str = "english", max_results: int = 20) -> list[Article]:
        edition = edition if edition in EDITIONS else "english"
        if edition == "spanish":
            url = f"https://news.google.com/rss?{EDITIONS['spanish']}"
        else:
            url = f"https://news.google.com/rss/search?q={quote_plus('Spain when:1d')}&{EDITIONS['english']}"
        return self._fetch(url, max_results)

    def search_news(self, query: str, edition: str = "english", days: int = 2, max_results: int = 15) -> list[Article]:
        edition = edition if edition in EDITIONS else "english"
        query = (query or "Spain").strip()[:120]
        url = f"https://news.google.com/rss/search?q={quote_plus(f'{query} when:{days}d')}&{EDITIONS[edition]}"
        return self._fetch(url, max_results)

    def _fetch(self, url: str, max_results: int) -> list[Article]:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=self.timeout)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
        results: list[Article] = []
        for entry in feed.entries:
            article = self._register(entry)
            if article and article not in results:
                results.append(article)
            if len(results) >= max_results:
                break
        return results

    def _register(self, entry) -> Article | None:
        source = _clean((entry.get("source") or {}).get("title", ""))
        title = _clean(entry.get("title", ""))
        if source and title.endswith(f" - {source}"):
            title = title[: -len(f" - {source}")].strip()
        if not title:
            return None
        key = normalize_title(title)
        if key in self._by_title:
            return self._by_title[key]

        published = None
        if entry.get("published_parsed"):
            published = datetime.fromtimestamp(calendar.timegm(entry.published_parsed), tz=timezone.utc)

        # Clustered stories list the other outlets covering them as <li> items.
        summary_html = entry.get("summary", "")
        related = re.findall(r"<li>(.*?)</li>", summary_html, flags=re.S)
        related_headlines = " | ".join(_clean(r) for r in related[1:4])[:400]

        article = Article(
            id=f"A{len(self.articles) + 1}",
            title=title,
            source=source or "unknown",
            published=published,
            link=entry.get("link", ""),
            related_coverage=max(1, len(related)),
            related_headlines=related_headlines,
        )
        self.articles[article.id] = article
        self._by_title[key] = article
        return article
