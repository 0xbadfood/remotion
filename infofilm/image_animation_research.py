"""Bounded research worker for the isolated, activity-routed animation CLI.

Search/fetch IO uses the existing kbagent-derived harness. Query selection,
source selection and passage-grounded digests use the selected research model,
including Luna when requested. Run in kbagent's Python for its Tavily SDK.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from .core import Store, digest, read, write
from .image_animation_models import ImageAnimationModels, ModelTransportError
from .research_digest import choose_passages, digest_errors, source_summary_prompt
from .research_harness import HarnessError, ResearchTools, canonical_url


def _redact(value):
    """SDK errors can echo keys; none may enter the research archive."""
    if isinstance(value, dict):
        return {key: _redact(item) for key, item in value.items()
                if key.lower() not in {'authorization', 'api_key', 'headers'}}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        for name in ('OPENAI_API_KEY', 'OPENAI_TOKEN', 'VLLM_API_KEY', 'TAVILY_API_KEY'):
            secret = os.environ.get(name)
            if secret:
                value = value.replace(secret, '[redacted]')
    return value


def _save(path, value):
    return write(path, _redact(value))


def _url(value):
    if not isinstance(value, str):
        raise ValueError('Source URLs must be strings')
    parts = urlsplit(value.strip())
    if parts.scheme.lower() not in {'http', 'https'} or not parts.netloc or parts.username or parts.password:
        raise ValueError('Source URLs must be absolute HTTP(S) URLs without credentials')
    return canonical_url(value)


def _queries(value):
    queries = value.get('queries') if isinstance(value, dict) else None
    if (not isinstance(queries, list) or not 2 <= len(queries) <= 4 or
        any(not isinstance(q, str) or not q.strip() or len(q) > 400 for q in queries)):
        raise ValueError('Research model must provide 2–4 bounded queries')
    return queries


def _selection(value, leads):
    urls = value.get('urls') if isinstance(value, dict) else None
    if not isinstance(urls, list) or not 1 <= len(urls) <= 8:
        raise ValueError('Research selection must use 1–8 unique discovered URLs')
    try:
        normalized = [_url(url) for url in urls]
    except (TypeError, ValueError):
        raise ValueError('Research selection must use 1–8 unique discovered URLs') from None
    if len(set(normalized)) != len(normalized) or any(url not in leads for url in normalized):
        raise ValueError('Research selection must use 1–8 unique discovered URLs')
    return normalized


def _repair_digest_prompt(subject, url, candidate, errors, passages):
    """Repair the parsed answer rather than asking for another whole-page digest."""
    # Keep source support available for citation repairs, but avoid repeating a
    # large article when only the claim count/JSON shape is wrong.
    claims = candidate.get('claims', []) if isinstance(candidate, dict) else []
    claims = claims if isinstance(claims, list) else []
    refs = {pid for c in claims if isinstance(c, dict) and isinstance(c.get('passage_ids'), list)
            for pid in c['passage_ids'] if isinstance(pid, str)}
    cited = [p for p in passages if p['id'] in refs] or passages[:6]
    allowance = min(600, 12000 // max(1, len(cited)))
    excerpts = [{"id": p["id"], "text": p["text"][:allowance]} for p in cited]
    return ('Repair output structure of this parsed source digest. Return only JSON '
        '{status:"usable"|"unusable", claims:[{claim,passage_ids:["P01"]}], '
        'cautions:[string], reason:string}. Select 1–12 distinct supported claims '
        'most relevant to the subject; retain qualifications and valid passage IDs. '
        'Do not add new facts. Remove claims that cannot be supported by the supplied '
        'excerpts. For an irrelevant source return unusable, empty claims and a reason.\n' +
        json.dumps({'subject': subject, 'url': url, 'errors': errors,
                    'candidate': candidate, 'supporting_excerpts': excerpts}, ensure_ascii=False))


def _source_model_failure(exc):
    if isinstance(exc, ModelTransportError):
        # Auth, malformed endpoints and other permanent request errors need
        # operator attention; they must not look like irrelevant source pages.
        return exc.retryable
    # Archived/third-party adapters may still expose the former safe facade.
    return isinstance(exc, RuntimeError) and str(exc) == 'local model request failed; credentials omitted'


def conduct_research(request, *, tools=None, models=None):
    directory = Path(request['directory']).resolve()
    store = Store(directory)
    try:
        models = models or ImageAnimationModels(request['config'], store,
            registry=request['registry']).for_activity('research')
        if tools is None:
            from tavily import TavilyClient
            if not os.environ.get('TAVILY_API_KEY'):
                raise RuntimeError('Set TAVILY_API_KEY for live research, or supply --sources')
            tools = ResearchTools(TavilyClient(api_key=os.environ['TAVILY_API_KEY']),
                Path(request['config']['kbagent_root']) / 'obscura', max_searches=4, max_fetches=8)
        subject = request['subject']
        tools.source_archive = directory / 'full-sources'

        def summarize(url, content):
            if _redact(content) != content:
                raise HarnessError('Fetched content contained credentials; source omitted')
            identity = digest({'url': url, 'content': digest(content), 'subject': subject,
                               'route': models.fingerprint, 'policy': 'image-animation-source-v1'})
            path = directory / 'summaries' / (identity[:20] + '.json')
            passages = choose_passages(content, subject)
            allowed = {p['id'] for p in passages}
            if path.exists():
                saved = read(path)
                if (not isinstance(saved, dict) or saved.get('full_sha256') != digest(content)
                        or saved.get('passages') != passages or saved.get('url') != url):
                    raise HarnessError('Archived source digest does not match the fetched passages')
                errors = digest_errors({**saved, 'status': 'usable' if saved.get('usable') else 'unusable'}, allowed)
                if errors:
                    raise HarnessError('Archived source digest is invalid: ' + '; '.join(errors))
                if _redact(saved) != saved:
                    raise HarnessError('Archived source digest contained credentials; source omitted')
                return saved
            # An explicit pending-route change must not regenerate already
            # accepted source work. Its source text and passage IDs still match.
            for prior_path in sorted((directory / 'summaries').glob('*.json')):
                if '-candidate-' in prior_path.name:
                    continue
                prior = read(prior_path)
                if (not isinstance(prior, dict) or prior.get('url') != url
                        or prior.get('full_sha256') != digest(content)
                        or prior.get('passages') != passages
                        or prior.get('subject', subject) != subject or _redact(prior) != prior):
                    continue
                if not digest_errors({**prior, 'status': 'usable' if prior.get('usable') else 'unusable'}, allowed):
                    _save(path, {**prior, 'reused_from': str(prior_path)})
                    return prior
            full = directory / 'full-sources' / (digest(url)[:16] + '.md')
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(content)
            prompt = source_summary_prompt(subject, url, passages)
            errors, candidate = [], None
            for attempt in range(2):
                candidate_path = path.with_name(path.stem + '-candidate-' + str(attempt) + '.json')
                previous_route = request.get('previous_route')
                if not candidate_path.exists() and previous_route:
                    old_identity = digest({'url': url, 'content': digest(content), 'subject': subject,
                        'route': previous_route, 'policy': 'image-animation-source-v1'})
                    old_candidate = directory / 'summaries' / (old_identity[:20] + '-candidate-' + str(attempt) + '.json')
                    if old_candidate.exists():
                        _save(candidate_path, read(old_candidate))
                if candidate_path.exists():
                    candidate = read(candidate_path)['candidate']
                else:
                    packet = prompt if attempt == 0 else _repair_digest_prompt(
                        subject, url, candidate, errors, passages)
                    candidate = models.call_json('planner', 'source-digest-' + identity[:12] + '-' + str(attempt),
                        packet, max_tokens=3200)
                errors = digest_errors(candidate, allowed)
                _save(candidate_path, {'candidate': candidate, 'errors': errors})
                if _redact(candidate) != candidate:
                    raise HarnessError('Source digest contained credentials; source omitted')
                if not errors:
                    break
            if errors:
                raise HarnessError('Invalid source digest: ' + '; '.join(errors))
            value = {'url': url, 'subject': subject, 'source_route': models.fingerprint,
                'claims': candidate['claims'], 'cautions': candidate.get('cautions', []),
                'passages': passages, 'full_file': str(full), 'full_sha256': digest(content),
                'usable': bool(candidate['claims']) and candidate.get('status') != 'unusable',
                'reason': candidate.get('reason', ''),
                'text': '\n'.join(c['claim'] for c in candidate['claims'])}
            _save(path, value)
            return value

        tools.source_summarizer = summarize
        queries_path = directory / 'queries.json'
        if queries_path.exists():
            query_plan = read(queries_path)
        else:
            query_plan = models.call_json('planner', 'research-queries',
                'Plan 2–4 short web search queries for primary or institutional evidence on the exact subject. '
                'A subject profile is guidance, not a fact. Seek causal explanations and qualifications. '
                'Return {queries:[string]}.\n' + json.dumps({'subject': subject,
                'guidance': request.get('guidance')}, ensure_ascii=False), max_tokens=1200)
        queries = _queries(query_plan)
        if _redact(query_plan) != query_plan:
            raise ValueError('Research queries contained credentials; query plan omitted')
        _save(queries_path, query_plan)
        leads = {}
        for number, query in enumerate(queries, 1):
            path = directory / 'searches' / f'{number}.json'
            result = _redact(read(path) if path.exists() else tools.search(query))
            _save(path, result)
            items = result.get('results', []) if isinstance(result, dict) else []
            for item in items if isinstance(items, list) else []:
                if not isinstance(item, dict):
                    continue
                try:
                    normalized = _url(item.get('url'))
                except (TypeError, ValueError):
                    continue
                leads[normalized] = item
            store.event('research', 'searched', {'position': number, 'total': len(queries),
                                               'leads': len(leads)})
        if not leads:
            raise RuntimeError('Bounded search produced no source leads; inspect searches/')
        selection_path = directory / 'source-selection.json'
        if selection_path.exists():
            selection = read(selection_path)
        else:
            selection = models.call_json('planner', 'research-source-selection',
                'Select up to eight promising URLs ONLY from the supplied leads. Prefer primary or institutional '
                'sources; cross-check important claims. Search snippets are discovery leads, never evidence. '
                'Return {urls:[string], rationale:string}.\n' + json.dumps({'subject': subject,
                    'leads': list(leads.values())}, ensure_ascii=False), max_tokens=1600)
        urls = _selection(selection, leads)
        if _redact(selection) != selection:
            raise ValueError('Research source selection contained credentials; selection omitted')
        _save(selection_path, selection)
        results, skipped = [], []

        def save_progress():
            _save(directory / 'result.json', {'subject': subject, 'source_count': len(results),
                'skipped': skipped, 'route': models.fingerprint, 'limits': {'searches': 4, 'fetches': 8}})
            if results:
                _save(directory / 'extracted.json', {'results': results})

        for number, normalized in enumerate(urls, 1):
            snapshot = directory / 'fetches' / (digest(normalized)[:16] + '.json')
            full = directory / 'full-sources' / (digest(normalized)[:16] + '.md')
            if full.exists():
                tools.fetch_cache[normalized] = full.read_text()
            elif snapshot.exists() and read(snapshot).get('error') and not read(snapshot).get('retryable'):
                skipped.append(_redact(read(snapshot)))
                save_progress()
                continue
            try:
                item = _redact(tools.fetch(normalized))
            except RuntimeError as exc:
                if not _source_model_failure(exc):
                    raise
                item = {'url': normalized, 'usable': False, 'retryable': True,
                        'error': 'Source digest model temporarily unavailable',
                        'diagnostic': getattr(exc, 'diagnostic', {'classification': 'transport_error'})}
                _save(snapshot, item)
                skipped.append(item)
                save_progress()
                store.event('research', 'source_deferred', {'position': number, 'total': len(urls),
                    'url': normalized, 'reason': item['error'], 'retryable': True})
                continue
            _save(snapshot, item)
            if normalized not in tools.fetched_urls:
                skipped.append(item)
                store.event('research', 'source_skipped', {'url': normalized, 'reason': item.get('error')})
                save_progress()
                continue
            content = tools.fetch_cache[normalized]
            summary = summarize(normalized, content)
            # Downstream sees a bounded, traceable excerpt set; full pages remain archived.
            cited = {pid for c in summary['claims'] for pid in c['passage_ids']}
            previews = [p for p in summary['passages'] if p['id'] in cited]
            results.append({'url': normalized, 'title': leads[normalized].get('title', normalized),
                'raw_content': content, 'source_digest': {'claims': summary['claims'],
                    'cautions': summary['cautions'], 'passages': previews, 'text': summary['text']}})
            save_progress()
            store.event('research', 'fetched', {'position': number, 'total': len(urls),
                                             'usable_sources': len(results)})
        save_progress()
        if not results:
            raise RuntimeError('No readable source documents; inspect research/result.json and fetches/')
        value = {'results': results}
        _save(directory / 'extracted.json', value)
        return value
    finally:
        store.db.close()


if __name__ == '__main__':
    conduct_research(read(sys.argv[1]))
