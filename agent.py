"""Spain News Agent: checks the news online and returns the top 5 news about Spain.

The agent is protected by Microsoft Entra ID. A user must sign in (the Conditional Access policy
created by setup/create_ca_policy.py requires MFA), then the agent runs as its Entra agent
identity on behalf of that user (OBO). The LLM is a Mistral model.

Usage:
    python agent.py                 # browser sign-in
    python agent.py --device-code   # device code sign-in
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from difflib import SequenceMatcher
from typing import Callable

import requests
from dotenv import load_dotenv

from entra_auth import AgentIdentityAuth, AgentSession, AuthError
from news_tools import Article, NewsDesk, normalize_title

MISTRAL_URL = "https://api.mistral.ai/v1/chat/completions"
MAX_TOOL_ROUNDS = 3
MAX_TOOL_CALLS = 6
TOP_N = 5

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_spain_headlines",
            "description": (
                "Get the latest headlines about Spain from Google News. "
                "edition 'spanish' = top stories of Spain's national press (in Spanish); "
                "edition 'english' = international English-language coverage of Spain."
            ),
            "parameters": {
                "type": "object",
                "properties": {"edition": {"type": "string", "enum": ["spanish", "english"]}},
                "required": ["edition"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_news",
            "description": "Search news articles from the last 48 hours, e.g. 'Spain economy' or 'Pedro Sanchez'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What to search for."},
                    "edition": {"type": "string", "enum": ["spanish", "english"]},
                },
                "required": ["query"],
            },
        },
    },
]

SYSTEM_PROMPT = """You are Spain News Agent. Today is {today}.
Your job: tell the user the {n} most important news stories about Spain right now.

Rules:
- Always check the news online with the tools; never answer from memory.
- Call get_spain_headlines with edition "spanish" AND with edition "english". Use search_news only if you need more detail.
- Only pick stories about Spain: Spanish politics, economy, society, major events in Spain, or Spain's role abroad.
- Pick {n} different events. If several articles cover the same event (same decree, court case, election...), keep only one of them.
- Prefer stories with national impact that several outlets cover.
- Use only facts found in the tool results (titles and related headlines). Never invent dates, numbers, names or causes.
- Write in English and translate Spanish titles accurately (e.g. "fallo judicial" means "court ruling").

When you have the news, reply with JSON only, in this exact format:
{{"top_news": [{{"id": "<article id from the tool results, e.g. A12>", "original_title": "<the article title exactly as in the tool results>", "headline": "<clear English headline>", "summary": "<2 factual sentences>", "why_it_matters": "<1 sentence>"}}]}}"""

CHAT_PROMPT = """You are Spain News Agent, an assistant for current news about Spain. Today is {today}.

Rules:
- Check the news online with the tools before answering; never answer from memory.
- Use only facts from the tool results or from earlier messages. If you can't find the answer, say so.
- Cite the articles you used with their ids in square brackets, for example [A12].
- Write your answer in English and translate Spanish sources; only use another language if the user's question is written in it.
- Keep it under 120 words, as plain text without markdown."""
MAX_CHAT_ROUNDS = 2
MAX_CHAT_TOOL_CALLS = 4


class MistralChat:
    def __init__(self, api_key: str, model: str):
        self.api_key = api_key
        self.model = model

    def complete(
        self, messages: list[dict], *, tool_choice: str = "auto", json_mode: bool = False, max_tokens: int = 1500
    ) -> dict:
        body: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": max_tokens,
            "tools": TOOLS,
            "tool_choice": tool_choice,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}
        for attempt in range(5):
            try:
                resp = requests.post(MISTRAL_URL, json=body, headers=headers, timeout=90)
            except (requests.Timeout, requests.ConnectionError):
                time.sleep(min(2**attempt, 10))
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                time.sleep(min(2**attempt, 20))
                continue
            if not resp.ok:
                raise RuntimeError(f"Mistral API error {resp.status_code}: {resp.text[:300]}")
            message = resp.json()["choices"][0]["message"]
            if isinstance(message.get("content"), list):
                message["content"] = "".join(
                    part.get("text", "") for part in message["content"] if isinstance(part, dict)
                )
            return message
        raise RuntimeError("The Mistral API is unavailable, slow or rate limited; please try again later.")


_STOPWORDS = {
    "spain", "spanish", "spain's", "after", "amid", "ahead", "before", "about", "with", "from", "over",
    "into", "this", "that", "their", "will", "could", "would", "says", "said", "news", "home", "more",
    "than", "what", "when", "where", "which", "while", "against", "under", "between", "first", "year",
}


def _keywords(text: str) -> set[str]:
    words = {w.strip("'") for w in re.findall(r"[^\W\d_]{4,}(?:'s)?", text.lower())}
    return {w.removesuffix("'s") for w in words} - _STOPWORDS


def _parse_top_news(content: str | None) -> list[dict]:
    match = re.search(r"\{.*\}", content or "", flags=re.S)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    items = data.get("top_news") if isinstance(data, dict) else None
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


class SpainNewsAgent:
    def __init__(self, llm: MistralChat, desk: NewsDesk, log: Callable[[str], None] = print):
        self.llm = llm
        self.desk = desk
        self.log = log

    def run(self) -> list[tuple[dict, Article]]:
        today = datetime.now().strftime("%A %d %B %Y")
        messages: list[dict] = [
            {"role": "system", "content": SYSTEM_PROMPT.format(today=today, n=TOP_N)},
            {"role": "user", "content": f"What are the top {TOP_N} news about Spain right now?"},
        ]
        last: dict | None = None
        tool_calls_made = 0
        for round_no in range(MAX_TOOL_ROUNDS):
            last = self.llm.complete(messages, tool_choice="any" if round_no == 0 else "auto", max_tokens=800)
            calls = last.get("tool_calls") or []
            if not calls:
                break
            messages.append({"role": "assistant", "content": last.get("content") or "", "tool_calls": calls})
            for call in calls:
                if tool_calls_made >= MAX_TOOL_CALLS:
                    messages.append(self._tool_result(call, {"error": "Tool budget used up. Write the final answer now."}))
                else:
                    tool_calls_made += 1
                    messages.append(self._run_tool(call))
            last = None
            if tool_calls_made >= MAX_TOOL_CALLS:
                break

        if len(self.desk.articles) < 10:
            # Make sure the model always has enough fresh news to choose from.
            extra = self.desk.get_spain_headlines("spanish") + self.desk.get_spain_headlines("english")
            self.log(f"  - runtime added {len(extra)} more headlines")
            messages.append(
                {
                    "role": "user",
                    "content": "More headlines checked online: "
                    + json.dumps([a.for_llm() for a in extra], ensure_ascii=False),
                }
            )
            last = None

        best: list[tuple[dict, Article]] = []
        problems: list[str] = []
        if last is not None:
            best, problems = self._validate(_parse_top_news(last.get("content")))
            messages.append({"role": "assistant", "content": last.get("content") or ""})
        base_prompt = (
            f"Reply now with the final answer as JSON only: exactly {TOP_N} stories about Spain, each about a "
            "different event, each with a valid article id (like A12) from the tool results."
        )
        attempts = 0
        while len(best) < TOP_N and attempts < 3:
            attempts += 1
            prompt = f"Your answer had problems ({'; '.join(problems[:5])}). {base_prompt}" if problems else base_prompt
            messages.append({"role": "user", "content": prompt})
            msg = self.llm.complete(messages, tool_choice="none", json_mode=True)
            messages.append({"role": "assistant", "content": msg.get("content") or ""})
            picks, problems = self._validate(_parse_top_news(msg.get("content")))
            if len(picks) > len(best):
                best = picks
        return best

    def chat(self, question: str, history: list[dict] | None = None) -> str:
        """Answer a follow-up question with the news tools. The raw answer cites articles by id
        ([A12]); use cite() to turn the ids into numbered sources."""
        today = datetime.now().strftime("%A %d %B %Y")
        messages: list[dict] = [
            {"role": "system", "content": CHAT_PROMPT.format(today=today)},
            *(history or [])[-8:],
            {"role": "user", "content": question},
        ]
        tool_calls_made = 0
        for round_no in range(MAX_CHAT_ROUNDS):
            msg = self.llm.complete(messages, tool_choice="any" if round_no == 0 else "auto", max_tokens=800)
            calls = msg.get("tool_calls") or []
            if not calls:
                if (msg.get("content") or "").strip():
                    return msg["content"].strip()
                break
            messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
            for call in calls:
                if tool_calls_made >= MAX_CHAT_TOOL_CALLS:
                    messages.append(self._tool_result(call, {"error": "Tool budget used up. Answer now."}))
                else:
                    tool_calls_made += 1
                    messages.append(self._run_tool(call))
        msg = self.llm.complete(messages, tool_choice="none", max_tokens=700)
        return (msg.get("content") or "").strip() or "Sorry, I couldn't find an answer in today's news."

    def cite(self, text: str) -> tuple[str, list[Article]]:
        """Replace article ids like [A12] or [A3, A7] with numbered references [1], [2]."""
        sources: list[Article] = []

        def number(match: re.Match) -> str:
            numbers: list[str] = []
            for article_id in re.findall(r"A\d+", match.group(1)):
                article = self.desk.articles.get(article_id)
                if article is None:
                    continue
                if article not in sources:
                    sources.append(article)
                numbers.append(str(sources.index(article) + 1))
            return f"[{', '.join(numbers)}]" if numbers else ""

        cited = re.sub(r"\[((?:\s*A\d+\s*,?)+)\]", number, text)
        cited = re.sub(r"(\*\*|__)(.+?)\1", r"\2", cited)  # small models add markdown emphasis anyway
        cited = re.sub(r"^\s*#+\s*", "", cited, flags=re.M)
        return re.sub(r"[ \t]+([.,;:])", r"\1", cited).strip(), sources

    @staticmethod
    def _tool_result(call: dict, payload: object) -> dict:
        return {
            "role": "tool",
            "tool_call_id": call.get("id"),
            "name": call.get("function", {}).get("name", ""),
            "content": json.dumps(payload, ensure_ascii=False),
        }

    def _run_tool(self, call: dict) -> dict:
        name = call.get("function", {}).get("name", "")
        raw_args = call.get("function", {}).get("arguments") or "{}"
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
        except (json.JSONDecodeError, TypeError, ValueError):
            args = {}
        if name not in ("get_spain_headlines", "search_news"):
            self.log("  - ignored an invalid tool call")
            return self._tool_result(
                call, {"error": "Unknown tool. Give the final answer as a normal message, not as a tool call."}
            )
        try:
            if name == "get_spain_headlines":
                articles = self.desk.get_spain_headlines(edition=str(args.get("edition", "english")))
            else:
                articles = self.desk.search_news(
                    query=str(args.get("query", "Spain")), edition=str(args.get("edition", "english"))
                )
            shown = ", ".join(f"{k}={v!r}" for k, v in args.items() if k in ("edition", "query"))
            self.log(f"  - {name}({shown}) -> {len(articles)} articles")
            return self._tool_result(call, [a.for_llm() for a in articles])
        except requests.RequestException as e:
            self.log(f"  - {name} failed: {e}")
            return self._tool_result(call, {"error": str(e)})

    @staticmethod
    def _same_event(item: dict, accepted: list[dict]) -> dict | None:
        """Cheap duplicate-event check on headline keywords (overlap coefficient)."""
        words = _keywords(item.get("headline", ""))
        for other in accepted:
            other_words = _keywords(other.get("headline", ""))
            shared = words & other_words
            if len(shared) >= 2 and len(shared) / max(1, min(len(words), len(other_words))) >= 0.5:
                return other
        return None

    def _resolve(self, item: dict) -> Article | None:
        """Map a pick to its article. Small models sometimes mix up ids, so the copied
        original title wins when it clearly matches a different article."""
        article = self.desk.articles.get(str(item.get("id", "")).strip())
        original = normalize_title(str(item.get("original_title") or ""))
        if not original:
            return article

        def similarity(candidate: Article) -> float:
            return SequenceMatcher(None, original, normalize_title(candidate.title)).ratio()

        if article and similarity(article) >= 0.6:
            return article
        best = max(self.desk.articles.values(), key=similarity, default=None)
        return best if best and similarity(best) >= 0.6 else article

    def _validate(self, items: list[dict]) -> tuple[list[tuple[dict, Article]], list[str]]:
        picks: list[tuple[dict, Article]] = []
        problems: list[str] = []
        seen: set[str] = set()
        for item in items:
            article = self._resolve(item)
            if article is None:
                problems.append(f"unknown article id {item.get('id')!r}")
            elif article.id in seen:
                problems.append(f"duplicate article {article.id}")
            elif not item.get("headline") or not item.get("summary"):
                problems.append(f"missing headline or summary for {article.id}")
            elif (twin := self._same_event(item, [p for p, _ in picks])) is not None:
                problems.append(
                    f"'{item['headline']}' is the same event as '{twin['headline']}'; replace it with a different event"
                )
            else:
                seen.add(article.id)
                picks.append((item, article))
        if len(picks) < TOP_N:
            problems.append(f"only {len(picks)} valid stories instead of {TOP_N}")
        return picks[:TOP_N], problems


def describe_identity(session: AgentSession, verbose: bool) -> None:
    me = session.get_me()
    graph_claims = session.graph_claims
    user_claims = session.user_claims
    methods = graph_claims.get("amr") or user_claims.get("amr") or []
    agent_name = graph_claims.get("app_displayname") or "agent identity"
    agent_app_id = graph_claims.get("appid") or graph_claims.get("azp")
    print(f"  Signed-in user : {me.get('displayName')} <{me.get('userPrincipalName')}>")
    print(f"  Authentication : {', '.join(methods) or 'n/a'}{'  (MFA satisfied)' if 'mfa' in methods else ''}")
    print(f"  Agent identity : {agent_name} (appId {agent_app_id}) acting on behalf of the user (OBO)")
    print(f"  Delegated scope: {graph_claims.get('scp')}")
    if verbose:
        keep = ("aud", "azp", "appid", "app_displayname", "idtyp", "upn", "scp", "amr", "xms_act_fct", "xms_sub_fct")
        print("  User token  :", json.dumps({k: user_claims[k] for k in keep if k in user_claims}))
        print("  Graph token :", json.dumps({k: graph_claims[k] for k in keep if k in graph_claims}))


def digest_text(picks: list[tuple[dict, Article]]) -> str:
    """Compact text version of a digest, used as chat context for follow-up questions."""
    lines = [f"Today's top {len(picks)} news about Spain:"]
    for i, (item, article) in enumerate(picks, 1):
        lines.append(f"{i}. [{article.id}] {item['headline'].strip()} - {item['summary'].strip()}")
    return "\n".join(lines)


def print_news(picks: list[tuple[dict, Article]]) -> None:
    title = f"TOP {len(picks)} NEWS ABOUT SPAIN - {datetime.now().strftime('%A %d %B %Y, %H:%M')}"
    print("\n" + "=" * len(title) + f"\n{title}\n" + "=" * len(title))
    for i, (item, article) in enumerate(picks, 1):
        when = article.published.astimezone().strftime("%d %b %H:%M") if article.published else "date unknown"
        print(f"\n{i}. {item['headline'].strip()}")
        print(f"   {article.source} | {when} | original: \"{article.title}\"")
        print(f"   {item['summary'].strip()}")
        if item.get("why_it_matters"):
            print(f"   Why it matters: {str(item['why_it_matters']).strip()}")
        print(f"   {article.link}")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Top 5 news about Spain (Entra Agent ID + Mistral).")
    parser.add_argument("--device-code", action="store_true", help="sign in with the device code flow")
    parser.add_argument("--login-hint", help="username to pre-fill on the sign-in page")
    parser.add_argument("-v", "--verbose", action="store_true", help="show token claims")
    args = parser.parse_args()

    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    api_key = os.environ.get("MISTRAL_API_KEY", "")
    if not api_key or api_key.startswith("<"):
        print("Set MISTRAL_API_KEY in .env (copy .env.example to .env first).", file=sys.stderr)
        return 1

    print("[1/3] Sign in to use the agent (Microsoft Entra ID)...", flush=True)
    try:
        session = AgentIdentityAuth.from_env().authenticate(args.device_code, args.login_hint)
        describe_identity(session, args.verbose)
    except (AuthError, requests.HTTPError) as e:
        print(f"Access denied: {e}", file=sys.stderr)
        return 1

    model = os.environ.get("MISTRAL_MODEL", "mistral-small-latest")
    print(f"\n[2/3] Checking the news online with Mistral model '{model}'...", flush=True)
    agent = SpainNewsAgent(MistralChat(api_key, model), NewsDesk(), log=lambda m: print(m, flush=True))
    try:
        picks = agent.run()
    except (RuntimeError, requests.RequestException) as e:
        print(f"The agent failed while checking the news: {e}", file=sys.stderr)
        return 1

    print("\n[3/3] Results", flush=True)
    if not picks:
        print("The agent could not produce a news digest. Please try again.", file=sys.stderr)
        return 1
    print_news(picks)
    if len(picks) < TOP_N:
        print(f"\n(Only {len(picks)} distinct stories could be verified.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
