"""Tests for the news agent itself, without the Entra sign-in.

1. Offline checks: duplicate-event heuristic and citation numbering.
2. Feed check: both Google News feeds return articles.
3. Live runs: the Mistral tool-calling loop returns 5 distinct, linked stories.

Needs internet access and MISTRAL_API_KEY in .env for the live runs.
Usage: python tests/test_news_agent.py [--runs N]   (N=0 skips the live runs)
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from agent import MistralChat, SpainNewsAgent  # noqa: E402
from news_tools import Article, NewsDesk  # noqa: E402

results: list[bool] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('  -> ' + detail) if detail else ''}")


def offline_checks() -> None:
    headlines = [
        "Spanish Court Lifts Arrest Warrant for Puigdemont, Allowing His Return to Catalonia",
        "Pedro Sánchez Announces Snap Elections for November 29, 2026",
        "Spain Approves Urgent Housing Measures Amid Protests",
        "Spanish Court Allows Puigdemont to Return Home Before Elections",
        "Housing Crisis Sparks Protests and Political Debate Before Snap Elections",
    ]
    accepted: list[dict] = []
    duplicates = []
    for headline in headlines:
        if SpainNewsAgent._same_event({"headline": headline}, accepted):
            duplicates.append(headline)
        else:
            accepted.append({"headline": headline})
    check("duplicate-event check flags only the repeated Puigdemont story", duplicates == [headlines[3]], str(duplicates))

    desk = NewsDesk()
    for article_id, title in (("A1", "First story"), ("A2", "Second story")):
        desk.articles[article_id] = Article(article_id, title, "Source", None, "https://example.com", 1, "")
    agent = SpainNewsAgent(llm=None, desk=desk)  # type: ignore[arg-type]
    text, sources = agent.cite("**Big news** [A2] and more [A1, A2] and unknown [A9].")
    check(
        "citations are renumbered, unknown ids dropped, markdown removed",
        text == "Big news [1] and more [2, 1] and unknown." and [s.id for s in sources] == ["A2", "A1"],
        text,
    )


def feed_check() -> None:
    desk = NewsDesk()
    spanish = desk.get_spain_headlines("spanish")
    english = desk.get_spain_headlines("english")
    check("Google News feeds return articles", len(spanish) > 0 and len(english) > 0,
          f"spanish={len(spanish)}, english={len(english)}")


def live_runs(runs: int) -> None:
    llm = MistralChat(os.environ["MISTRAL_API_KEY"], os.environ.get("MISTRAL_MODEL", "mistral-small-latest"))
    for run in range(1, runs + 1):
        started = time.time()
        agent = SpainNewsAgent(llm, NewsDesk(), log=lambda message: print(f"      {message.strip()}"))
        picks = agent.run()
        ids = [article.id for _, article in picks]
        check(
            f"live run {run}: 5 distinct stories with links",
            len(picks) == 5 and len(set(ids)) == 5 and all(a.link.startswith("https://") for _, a in picks),
            f"{len(agent.desk.articles)} articles seen, {time.time() - started:.1f}s",
        )
        for i, (item, article) in enumerate(picks, 1):
            print(f"      {i}. {item['headline'][:90]}  [{article.source}]")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Tests for the news agent (no sign-in).")
    parser.add_argument("--runs", type=int, default=1, help="number of live agent runs (0 = skip)")
    args = parser.parse_args()
    offline_checks()
    feed_check()
    if args.runs:
        live_runs(args.runs)
    print(f"\n{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
