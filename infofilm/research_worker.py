"""Run the local, bounded kbagent-derived harness in kbagent's environment."""
import json
import os
import sys
import time
from pathlib import Path

from .core import Store, digest, read, write
from .llm import Models, llm_slot
from .llm_settings import api_key, endpoint
from .research_digest import MODEL_MESSAGE_LIMIT, choose_passages, digest_errors, source_summary_prompt
from .research_harness import ResearchTools, QwenClient, run_agent, TOOLS, canonical_url, HarnessError


def main():
    request = read(sys.argv[1])
    directory = Path(request["directory"])
    from tavily import TavilyClient

    subject = request["subject"]
    project_store = Store(directory.parent.parent)
    summaries = directory / "source-summaries"
    full_sources = directory / "full-sources"
    llm = request.get("llm") or {"reviewer_url": request["model_url"]}
    reviewer = Models(llm, project_store)
    agent = endpoint(llm, request.get("agent_role", "reviewer"))

    def summarize_source(url, content):
        identity = digest({"url": url, "full_sha256": digest(content), "policy": "source-digest-v1"})
        path = summaries / (identity[:20] + ".json")
        if path.exists():
            return read(path)
        full_sources.mkdir(parents=True, exist_ok=True)
        source_file = full_sources / (digest(url)[:16] + ".md")
        if source_file.exists() and source_file.read_text() != content:
            old_source = source_file.with_suffix(".previous.md")
            if not old_source.exists():
                old_source.write_text(source_file.read_text())
            source_file.write_text(content)
        elif not source_file.exists():
            source_file.write_text(content)
        passages = choose_passages(content, subject)
        passage_ids = {p["id"] for p in passages}
        prompt = source_summary_prompt(subject, url, passages)
        response = None
        errors = []
        for attempt in range(2):
            correction = ("\nCorrect these output structure errors without adding unsupported claims: " + "; ".join(errors)) if errors else ""
            try:
                response = reviewer.call("reviewer", f"research-digest-{identity[:14]}-{attempt}", prompt + correction, max_tokens=3200)
                errors = digest_errors(response, passage_ids)
            except ValueError as exc:
                # Invalid model JSON is a source-level structural error. Model
                # transport/server failures still propagate rather than hiding an outage.
                response = None
                errors = [f"Source digest was not a valid JSON object: {exc}"]
            write(summaries / f"{identity[:20]}-candidate-{attempt}.json", {"response": response, "errors": errors})
            if not errors:
                break
        if errors:
            raise HarnessError(f"Source digest needs structural repair: {errors}")
        usable = bool(response["claims"]) and response.get("status") != "unusable"
        text = "\n".join(f"- [{', '.join(c['passage_ids'])}] {c['claim']}" for c in response["claims"])
        if response.get("cautions"):
            text += "\nQualifications: " + "; ".join(str(c) for c in response["cautions"])
        value = {"url": url, "full_sha256": digest(content), "full_file": str(source_file),
                 "passages": passages, "claims": response["claims"],
                 "usable": usable, "reason": response.get("reason", ""),
                 "cautions": response.get("cautions", []), "text": text,
                 "selection_policy": "source-order lead plus relevant bounded passages; every passage maps to the full page",
                 "human_source_review": "pending"}
        write(path, value)
        return value

    class ArchiveTools(ResearchTools):
        def search(self, query):
            result = super().search(query)
            path = directory/f"search-{self.searches_used}.json"
            if "results" in result and not path.exists():
                write(path,result)
            return result

        def fetch(self, url):
            result = super().fetch(url)
            path = directory/"fetches"/(digest(url)[:16]+".json")
            if not path.exists():
                write(path,result)
            if result.get("usable") is False:
                project_store.event("research-source", "skipped", {"url": url, "reason": result["error"]})
            if "content" in result and url:
                write(directory/"extracted.json",{"results":[{"url":u,"raw_content":t}
                    for u,t in self.fetch_cache.items() if u in self.fetched_urls]})
            return result

    class ArchiveModel(QwenClient):
        count = max((int(p.stem.removeprefix('call-')) for p in (directory/'model-calls').glob('call-*.json')
                     if p.stem.removeprefix('call-').isdigit()), default=0)
        def complete(self,messages,tools=TOOLS):
            self.count += 1
            start=time.monotonic()
            result = None
            try:
                with llm_slot(agent):
                    result=super().complete(messages,tools)
                return result
            finally:
                write(directory/"model-calls"/f"call-{self.count}.json",{"model":self.model,"messages":messages,
                    "assistant":result,"latency_seconds":time.monotonic()-start,
                    "context_bound": self.last_context_report,
                    "response_attempts": self.last_response_reports})

    tools=ArchiveTools(TavilyClient(api_key=os.environ.get("TAVILY_API_KEY")),
        Path(request["kbagent_root"])/"obscura",max_searches=request.get("budget",{}).get("max_searches",3),
        max_fetches=request.get("budget",{}).get("max_fetches",8),
        fallback=TavilyClient(api_key=os.environ["TAVILY_FALLBACK"]) if os.environ.get("TAVILY_FALLBACK") else None)
    tools.source_summarizer = summarize_source
    tools.source_archive = full_sources
    prior_searches = sorted(directory.glob("search-*.json"))
    prior_fetches = sorted((directory / "fetches").glob("*.json"))
    tools.searches_used = min(len(prior_searches), tools.max_searches)
    tools.fetches_used = min(len(prior_fetches), tools.max_fetches)
    for path in prior_fetches:
        item = read(path)
        url = item.get("url", "")
        if item.get("error"):
            if url and item.get("usable") is False:
                tools.source_errors[canonical_url(url)] = item
            continue
        old_full = item.get("content", "") if not item.get("full_characters") else ""
        source_path = full_sources / (digest(url)[:16] + ".md")
        content = source_path.read_text() if source_path.exists() else old_full
        if url and len(content.strip()) >= 100:
            normalized = canonical_url(url)
            tools.fetch_cache[normalized] = content
    leads = []
    for path in prior_searches:
        for item in read(path).get("results", []):
            url = item.get("url")
            if url and url not in leads:
                leads.append(url)
    resumed = (" A previous bounded attempt archived search leads and fetched pages. "
               "Search budget is exhausted; first use the archived fetched pages, then fetch other promising scholarly or institutional leads if useful. "
               "Available leads: " + "; ".join(leads[:18]) + ".") if prior_searches else ""
    if tools.fetch_cache:
        for url in list(tools.fetch_cache):
            tools.fetch(url)  # Revalidate archived content before allowing citations.
        seed_digests = [tools.digest_cache[url] for url in tools.fetch_cache if url in tools.fetched_urls]
        resumed += "\nFirst-pass summaries of already fetched, archived pages:\n" + "\n".join(
            item["url"] + "\n" + item["text"] for item in seed_digests)
    key = os.environ.get("VLLM_API_KEY") if agent.get("legacy") else api_key(llm.get("llm_secrets_root"), agent.get("id"), agent)
    model=ArchiveModel(agent["url"],agent.get("model") or None,key,timeout=900,local=agent["type"]=="openai_chat")
    guidance = ''
    if request.get('subject_guidance') is not None:
        guidance = ('\nResearch guidance from a reviewed subject profile (questions, not facts):\n' +
                    json.dumps(request['subject_guidance'], ensure_ascii=False) +
                    '\nUse relevant questions only. Keep the exact requested focus; do not expand scope or treat the graph as evidence.')
    answer=run_agent("Research an evergreen, general-audience explanation of "+request["subject"]+
        ". Prefer primary sources and substantive institutional scholarship. Explain the main mechanism or chronology, important qualifications and practical significance. "
        "Keep distinct events and disputed interpretations separate. Avoid forecasts and dated statistics."+guidance+resumed,
        model,tools,max_steps=20,verbose=True)
    if not tools.fetched_urls:
        raise RuntimeError("Research produced no usable fetched source documents")
    # Supply the next planner with the same concise, traceable first-pass briefs.
    results = []
    for url, content in tools.fetch_cache.items():
        if url not in tools.fetched_urls:
            continue
        summary = summarize_source(url, content)
        remaining = 7000
        cited = {reference for claim in summary["claims"] for reference in claim["passage_ids"]}
        previews = []
        for passage in summary["passages"]:
            if passage["id"] not in cited or remaining < 100:
                continue
            fragment = passage["text"][:min(900, remaining)]
            previews.append({"id": passage["id"], "offset_start": passage["offset_start"],
                             "offset_end": passage["offset_start"] + len(fragment), "text": fragment})
            remaining -= len(fragment)
        results.append({"url": url, "raw_content": content, "source_digest": {
            "claims": summary["claims"], "cautions": summary["cautions"],
            "passages": previews, "text": summary["text"]}})
    write(directory/"extracted.json", {"results": results})
    (directory/"answer.txt").write_text(answer)
    write(directory/"result.json",{"subject":request["subject"],"searches":tools.searches_used,
        "fetches":tools.fetches_used,"fetched_urls":sorted(tools.fetched_urls),"model_calls":model.count,
        "skipped_sources": list(tools.source_errors.values()),
        "source_digests": len(results), "context_limit_bytes": MODEL_MESSAGE_LIMIT,
        "citation_validation":"Upstream harness requires at least one fetched URL citation; downstream evidence checks fact-level references."})


if __name__=="__main__":
    failure_path = Path(read(sys.argv[1])["directory"]) / 'failure.json'
    failure_path.unlink(missing_ok=True)
    try:
        main()
    except Exception as exc:
        write(failure_path, {'type': type(exc).__name__, 'error': str(exc), 'time': time.time()})
        raise
