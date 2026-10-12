#!/usr/bin/env python3
"""Local kbagent research harness with bounded, source-grounded tool context.

Derived from /home/nitin/kbagent/search_agent.py. This copy keeps upstream's
search/fetch/citation contracts while the film workflow adds first-pass source
digests and a hard bound on the model conversation.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .research_digest import MODEL_MESSAGE_LIMIT, bound_messages
from .research_sources import fetch_pdf_text, readability_error

import httpx


SYSTEM_PROMPT = """You are a careful internet research agent. Answer the user's question accurately from authoritative web sources.

You have two tools:
- web_search: broad discovery through Tavily. Use short, varied search queries.
- fetch_url: retrieve the complete rendered Markdown of a promising page through Obscura.

Research rules:
1. Search broadly before answering. Prefer primary sources: government and institutional sites, official documentation, standards bodies, original research, and direct company statements. Treat blogs, aggregators, and search snippets only as leads.
2. Fetch and read the source summaries you rely on. Each summary is grounded in
   passages of a full fetched page archived alongside it. Do not cite a search snippet.
3. Cross-check important or disputed claims when possible. If reliable sources disagree, say so.
4. Answer the exact question. Separate established facts from inference and state material uncertainty.
5. Cite claims inline with Markdown links to the fetched source pages, for example [WHO](https://www.who.int/...). Finish with a short `Sources` list of the most important fetched pages. Never invent a URL or cite a URL you did not fetch.
6. Search and fetch budgets are enforced by the tools and may be partly spent on a resumed run. Stop early once the evidence is sufficient.
"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Broadly search the public web. Returns titles, URLs, relevance scores, and short discovery snippets; fetch a result before citing it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "A concise web search query, no more than 400 characters.",
                    }
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": "Fetch and archive a full HTTP(S) page. Return a bounded first-pass digest with passage references into the archived source. Fetch a source before relying on or citing it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The complete HTTP(S) URL to fetch."}
                },
                "required": ["url"],
                "additionalProperties": False,
            },
        },
    },
]


class HarnessError(RuntimeError):
    pass


def canonical_url(url: str) -> str:
    """Normalize enough for citation validation without changing URL semantics."""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if parts.port:
        host = f"{host}:{parts.port}"
    path = parts.path or "/"
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), host, path, parts.query, ""))


@dataclass
class ResearchTools:
    tavily: Any
    obscura_path: Path
    max_searches: int = 5
    max_fetches: int = 50
    search_results: int = 10
    fetch_timeout: int = 60
    searches_used: int = 0
    fetches_used: int = 0
    fetched_urls: set[str] = field(default_factory=set)
    fetch_cache: dict[str, str] = field(default_factory=dict)
    digest_cache: dict[str, dict[str, Any]] = field(default_factory=dict)
    source_summarizer: Any = None
    source_archive: Path | None = None
    source_errors: dict[str, dict[str, Any]] = field(default_factory=dict)
    fallback: Any = None  # second Tavily client (TAVILY_FALLBACK), used once the primary fails, e.g. out of credits

    def search(self, query: str) -> dict[str, Any]:
        query = query.strip()
        if not query:
            return {"error": "query must not be empty"}
        if len(query) > 400:
            return {"error": "query exceeds Tavily's 400-character limit"}
        if self.searches_used >= self.max_searches:
            return {"error": f"search budget exhausted ({self.max_searches}/{self.max_searches})"}

        self.searches_used += 1
        options = dict(query=query, search_depth="advanced", max_results=self.search_results,
                       include_answer=False, include_raw_content=False)
        try:
            response = self.tavily.search(**options)
        except Exception as exc:  # SDK exception types have changed between releases.
            if self.fallback is None:
                return {"error": f"Tavily search failed: {exc}"}
            print(f"[research] primary Tavily key failed ({exc}); switching to TAVILY_FALLBACK", flush=True)
            self.tavily, self.fallback = self.fallback, None
            try:
                response = self.tavily.search(**options)
            except Exception as fallback_exc:
                return {"error": f"Tavily search failed on both keys: {exc}; fallback: {fallback_exc}"}

        results = []
        for item in response.get("results", []):
            results.append(
                {
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "score": item.get("score"),
                    "snippet": str(item.get("content", ""))[:360],
                }
            )
        return {
            "query": query,
            "results": results,
            "budget": {"searches_used": self.searches_used, "searches_left": self.max_searches - self.searches_used},
        }

    def fetch(self, url: str) -> dict[str, Any]:
        url = url.strip()
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            return {"error": "fetch_url accepts only absolute HTTP(S) URLs"}

        normalized = canonical_url(url)
        if normalized in self.source_errors:
            return {**self.source_errors[normalized], "cached": True}
        if normalized in self.fetch_cache:
            content = self.fetch_cache[normalized]
            return self._summarize_fetched(url, normalized, content, cached=True)
        if self.fetches_used >= self.max_fetches:
            return {"error": f"fetch budget exhausted ({self.max_fetches}/{self.max_fetches})"}

        self.fetches_used += 1
        if parts.path.lower().endswith(".pdf"):
            try:
                content = fetch_pdf_text(normalized, self.fetch_timeout, self.source_archive)
            except (httpx.HTTPError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
                return self._source_error(normalized, f"PDF source could not be read: {exc}")
        else:
            try:
                completed = subprocess.run(
                    [str(self.obscura_path), "fetch", "--dump", "markdown", "--timeout", str(self.fetch_timeout), url],
                    text=True, capture_output=True, timeout=self.fetch_timeout + 10, check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                return {"error": f"Obscura fetch failed: {exc}", "url": url}
            if completed.returncode != 0:
                detail = completed.stderr.strip() or f"exit status {completed.returncode}"
                return {"error": f"Obscura fetch failed: {detail}", "url": url}
            content = completed.stdout
        return self._summarize_fetched(url, normalized, content)

    def _source_error(self, normalized: str, reason: str) -> dict[str, Any]:
        result = {"url": normalized, "error": reason, "usable": False,
                  "instruction": "Do not cite this source; use another readable source."}
        self.source_errors[normalized] = result
        self.fetched_urls.discard(normalized)
        return result

    def _summarize_fetched(self, url: str, normalized: str, content: str, cached: bool = False) -> dict[str, Any]:
        # Also handle redirects and archived data from older fetchers.
        if content.lstrip().startswith("%PDF-"):
            try:
                content = fetch_pdf_text(normalized, self.fetch_timeout, self.source_archive)
                self.digest_cache.pop(normalized, None)
            except (httpx.HTTPError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
                return self._source_error(normalized, f"PDF source could not be read: {exc}")
        error = readability_error(content)
        if error:
            return self._source_error(normalized, error)
        self.fetch_cache[normalized] = content
        try:
            digest = self._source_digest(normalized, content)
        except HarnessError as exc:
            return self._source_error(normalized, f"Source summary could not be validated: {exc}")
        if digest.get("usable") is False:
            return self._source_error(normalized, digest.get("reason") or "; ".join(digest.get("cautions", [])) or "Source had no relevant supported claims")
        self.fetched_urls.add(normalized)
        return {
            "url": url, "content": digest["text"], "full_characters": len(content), "cached": cached,
            "budget": {"fetches_used": self.fetches_used, "fetches_left": self.max_fetches - self.fetches_used},
        }

    def _source_digest(self, normalized: str, content: str) -> dict[str, Any]:
        if normalized not in self.digest_cache:
            if not self.source_summarizer:
                raise HarnessError("Source summarizer is required before fetched content reaches the model")
            self.digest_cache[normalized] = self.source_summarizer(normalized, content)
        return self.digest_cache[normalized]

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "web_search":
            return self.search(str(arguments.get("query", "")))
        if name == "fetch_url":
            return self.fetch(str(arguments.get("url", "")))
        return {"error": f"unknown tool: {name}"}


class QwenClient:
    def __init__(self, base_url: str, model: str | None, api_key: str | None = None, timeout: float = 180.0,
                 max_response_attempts: int = 3, local: bool = True) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.local = local
        if not 1 <= max_response_attempts <= 5:
            raise ValueError('Research response attempts must be between one and five')
        self.max_response_attempts = max_response_attempts
        self.last_response_reports: list[dict[str, Any]] = []
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.client = httpx.Client(timeout=timeout, headers=headers)

    def discover_model(self) -> str:
        if self.model:
            return self.model
        try:
            response = self.client.get(f"{self.base_url}/models")
            response.raise_for_status()
            models = response.json().get("data", [])
            if not models:
                raise HarnessError("the model server returned no models")
            self.model = str(models[0]["id"])
            return self.model
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise HarnessError(f"cannot discover a model at {self.base_url}/models: {exc}") from exc

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = TOOLS) -> dict[str, Any]:
        messages, self.last_context_report = bound_messages(messages, MODEL_MESSAGE_LIMIT)
        payload: dict[str, Any] = {
            "model": self.discover_model(),
            "messages": messages,
            "temperature": 0.1,
            "max_tokens": 4000,
        }
        if self.local:      # llama.cpp / vLLM reasoning switch; hosted APIs reject unknown fields
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        else:               # hosted models count hidden reasoning inside max_tokens: keep it light, with room
            payload["reasoning"] = {"effort": "low"}
            payload["max_tokens"] += 8000
        if tools:
            payload.update({"tools": tools, "tool_choice": "auto"})
        self.last_response_reports = []
        for attempt in range(1, self.max_response_attempts + 1):
            try:
                response = self.client.post(f"{self.base_url}/chat/completions", json=payload)
                response.raise_for_status()
                raw = response.json()
                choice = raw["choices"][0]
                message = choice["message"]
                if not isinstance(message, dict):
                    raise ValueError('Model message must be an object')
            except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
                detail = ""
                if isinstance(exc, httpx.HTTPStatusError):
                    detail = f"; response: {exc.response.text[:1000]}"
                raise HarnessError(f"Qwen request failed: {exc}{detail}") from exc
            usable = bool(str(message.get('content') or '').strip() or message.get('tool_calls'))
            report = {'attempt': attempt, 'usable': usable, 'finish_reason': choice.get('finish_reason'),
                      'reasoning_only': bool(message.get('reasoning_content')) and not usable,
                      'usage': raw.get('usage', {})}
            self.last_response_reports.append(report)
            if usable:
                return message
            if attempt < self.max_response_attempts:
                print('[research-model] retrying: ' + json.dumps({
                    'attempt': attempt + 1, 'max_attempts': self.max_response_attempts,
                    'reason': 'reasoning_only_response' if report['reasoning_only'] else 'empty_response',
                    'finish_reason': report['finish_reason']}), file=sys.stderr, flush=True)
        raise HarnessError(f'Qwen returned no answer or tool calls after {self.max_response_attempts} attempts '
                           '(thinking disabled; reasoning-only responses cannot be used as evidence)')


def cited_fetched_urls(answer: str, fetched_urls: set[str]) -> set[str]:
    found = re.findall(r"https?://[^\s<>\])}]+", answer)
    cited: set[str] = set()
    for raw in found:
        candidate = raw.rstrip(".,;:'\"")
        try:
            normalized = canonical_url(candidate)
        except ValueError:
            continue
        if normalized in fetched_urls:
            cited.add(normalized)
    return cited


def run_agent(question: str, qwen: QwenClient, research: ResearchTools, max_steps: int = 60, verbose: bool = False) -> str:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]

    answer = ""
    for step in range(1, max_steps + 1):
        raw_message = qwen.complete(messages)
        message = {
            key: raw_message[key]
            for key in ("role", "content", "tool_calls")
            if key in raw_message
        }
        message.setdefault("role", "assistant")
        messages.append(message)
        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            answer = (message.get("content") or "").strip()
            break

        for call in tool_calls:
            function = call.get("function", {})
            name = str(function.get("name", ""))
            raw_arguments = function.get("arguments", "{}")
            arguments: dict[str, Any] = {}
            try:
                arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
                if not isinstance(arguments, dict):
                    raise ValueError("arguments must be an object")
                result = research.execute(name, arguments)
            except (json.JSONDecodeError, ValueError) as exc:
                result = {"error": f"invalid tool arguments: {exc}"}
            if verbose:
                print(
                    f"[{step}] {name} {json.dumps(arguments)} -> "
                    f"{len(json.dumps(result))} bytes",
                    file=sys.stderr,
                )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id", f"call-{step}"),
                    "name": name,
                    "content": json.dumps(result, ensure_ascii=False),
                }
            )
    else:
        messages.append(
            {
                "role": "user",
                "content": "The research step limit is reached. Answer now using only fetched evidence, with inline citations.",
            }
        )
        answer = (qwen.complete(messages, tools=None).get("content") or "").strip()

    if not answer:
        raise HarnessError("Qwen returned an empty answer")
    if not research.fetched_urls:
        raise HarnessError("the agent produced an answer without successfully fetching a source")

    if not cited_fetched_urls(answer, research.fetched_urls):
        allowed = "\n".join(sorted(research.fetched_urls))
        messages.extend(
            [
                {"role": "assistant", "content": answer},
                {
                    "role": "user",
                    "content": (
                        "Revise the answer so factual claims have inline Markdown citations. Cite only the fetched URLs "
                        f"listed below and retain a Sources section.\n\n{allowed}"
                    ),
                },
            ]
        )
        answer = (qwen.complete(messages, tools=None).get("content") or "").strip()
        if not cited_fetched_urls(answer, research.fetched_urls):
            raise HarnessError("Qwen did not cite any successfully fetched source")
    return answer


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("question", nargs="+", help="Question to research")
    parser.add_argument("--base-url", default="http://127.0.0.1:7001/v1", help="OpenAI-compatible API base URL")
    parser.add_argument("--model", help="Qwen model ID; defaults to the first model reported by /v1/models")
    parser.add_argument("--vllm-api-key", default=os.environ.get("VLLM_API_KEY"), help="Defaults to VLLM_API_KEY")
    parser.add_argument("--tavily-api-key", default=os.environ.get("TAVILY_API_KEY"), help="Defaults to TAVILY_API_KEY")
    parser.add_argument("--obscura", type=Path, default=Path(os.environ.get("INFOFILM_OBSCURA", "/home/nitin/kbagent/obscura")))
    parser.add_argument("--max-searches", type=int, default=5, choices=range(1, 6), metavar="1..5")
    parser.add_argument("--max-fetches", type=int, default=50, choices=range(1, 51), metavar="1..50")
    parser.add_argument("--max-steps", type=int, default=60)
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    from tavily import TavilyClient
    args = parse_args(argv)
    if not args.tavily_api_key:
        print("error: set TAVILY_API_KEY or pass --tavily-api-key", file=sys.stderr)
        return 2
    if not args.obscura.is_file():
        print(f"error: Obscura executable not found: {args.obscura}", file=sys.stderr)
        return 2

    research = ResearchTools(
        tavily=TavilyClient(api_key=args.tavily_api_key),
        obscura_path=args.obscura.resolve(),
        max_searches=args.max_searches,
        max_fetches=args.max_fetches,
    )
    try:
        answer = run_agent(
            " ".join(args.question),
            QwenClient(args.base_url, args.model, args.vllm_api_key),
            research,
            args.max_steps,
            args.verbose,
        )
    except (HarnessError, KeyboardInterrupt) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(answer)
    print(f"\nUsage: {research.searches_used}/{research.max_searches} searches, "
          f"{research.fetches_used}/{research.max_fetches} fetches", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
