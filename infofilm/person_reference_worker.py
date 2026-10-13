"""Bounded photo/portrait research, executed in the existing research environment."""
from __future__ import annotations

import html
import io
import ipaddress
import json
import os
import re
import socket
import sys
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from PIL import Image, ImageOps

from .core import Store, digest, read, write
from .llm import Models, ModelOutputError
from .person_references import PROTOCOL, planned_kind, validate_reference

# Wikimedia refuses (HTTP 403) user agents without a contact URL or email: https://w.wiki/4wJS
USER_AGENT = 'InfoFilm/1.0 (source-backed educational portrait references; ' + os.environ.get('WIKIMEDIA_CONTACT', 'no contact set') + ')'


class ReferenceSearchClient:
    """Use the research stack's Tavily service/key without requiring its SDK venv."""
    def _call(self, operation, params):
        keys = [k for k in (os.environ.get('TAVILY_API_KEY'), os.environ.get('TAVILY_FALLBACK')) if k]
        if not keys:
            raise RuntimeError('TAVILY_API_KEY is required for portrait source discovery')
        for index, key in enumerate(keys):
            response = httpx.post('https://api.tavily.com/' + operation, json=params,
                                 headers={'Authorization': 'Bearer ' + key}, timeout=90)
            if response.is_success or index == len(keys) - 1 or response.status_code < 400:
                break  # a failed primary (e.g. out of credits) falls through to TAVILY_FALLBACK
        response.raise_for_status()
        return response.json()

    def search(self, **params):
        return self._call('search', params)

    def extract(self, **params):
        return self._call('extract', params)


def text(value):
    return html.unescape(re.sub(r'<[^>]+>', ' ', str(value or ''))).strip()


def public_url(url):
    parsed = urlsplit(url)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Reference source needs a public HTTP(S) URL')
    addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80),
                                   type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError('Reference download cannot use a private or local address')
    return url


def download_portrait(url, destination):
    """Bound downloads/redirects/pixels; normalize without inventing or cropping a face."""
    with httpx.Client(timeout=45, headers={'User-Agent': USER_AGENT}, follow_redirects=False) as client:
        for _ in range(5):
            public_url(url)
            with client.stream('GET', url) as response:
                if response.is_redirect:
                    url = urljoin(url, response.headers['location']); continue
                response.raise_for_status()
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 12*1024*1024:
                        raise ValueError('Reference download exceeds 12 MB')
                break
        else:
            raise ValueError('Reference has too many redirects')
    with Image.open(io.BytesIO(raw)) as image:
        if image.width * image.height > 20_000_000 or min(image.size) < 96:
            raise ValueError('Reference is too large or too small for a usable portrait')
        if image.format not in {'PNG', 'JPEG', 'WEBP', 'TIFF'}:
            raise ValueError('Reference is not a supported raster photograph/portrait')
        image.load()
        image = ImageOps.exif_transpose(image).convert('RGB')
        if all(lo == hi for lo, hi in image.getextrema()):
            raise ValueError('Reference is a blank image')
        image.thumbnail((768, 768), Image.Resampling.LANCZOS)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix('.tmp.png')
        image.save(temporary); temporary.replace(destination)
    return {'file': str(destination), 'sha256': digest(destination.read_bytes()),
            'original_sha256': digest(bytes(raw)), 'download_url': url}


def commons_candidates(person, maximum):
    params = {'action': 'query', 'format': 'json', 'generator': 'search', 'gsrnamespace': 6,
              'gsrsearch': 'filetype:bitmap "' + person['name'] + '"', 'gsrlimit': maximum,
              'prop': 'imageinfo', 'iiprop': 'url|extmetadata', 'iiurlwidth': 768}
    response = httpx.get('https://commons.wikimedia.org/w/api.php', params=params,
                         headers={'User-Agent': USER_AGENT}, timeout=45)
    response.raise_for_status()
    candidates = []
    for page in response.json().get('query', {}).get('pages', {}).values():
        info = (page.get('imageinfo') or [{}])[0]; metadata = info.get('extmetadata', {})
        caption = text(metadata.get('ImageDescription', {}).get('value'))
        image_url, source_url = info.get('thumburl', info.get('url')), info.get('descriptionurl')
        if not caption or not image_url or not source_url:
            continue
        candidates.append({'image_url': image_url, 'source_url': source_url, 'title': page['title'],
            'caption': caption[:5000], 'date': text(metadata.get('DateTimeOriginal', {}).get('value')),
            'artist': text(metadata.get('Artist', {}).get('value'))[:1000],
            'license': text(metadata.get('LicenseShortName', {}).get('value')),
            'license_url': text(metadata.get('LicenseUrl', {}).get('value')),
            'source_type': 'Wikimedia Commons file-page metadata'})
    return candidates


def tavily_candidates(person, tavily, maximum, max_pages, archive, *, avoid_wikimedia=False, broad=False):
    query = (person['name'] + ' ' + person['appearance_context'] + ' ' +
             person['reference_kind'].replace('_', ' ') + ' archive museum biography')[:390]
    domains = ['commons.wikimedia.org', 'wikipedia.org', 'loc.gov', 'archives.gov', 'si.edu',
               'npg.org.uk', 'getty.edu', 'nationalarchives.gov.uk', 'nobelprize.org', 'ias.edu',
               'princeton.edu', 'caltech.edu', 'fbi.gov', 'justice.gov', 'nps.gov', 'nasa.gov',
               'rct.uk', 'vam.ac.uk', 'metmuseum.org', 'britishmuseum.org']
    if avoid_wikimedia:
        domains = [d for d in domains if d not in {'commons.wikimedia.org', 'wikipedia.org'}]
    discovery = tavily.search(query=query, search_depth='advanced', max_results=max_pages,
                             include_answer=False, include_raw_content=False,
                             **({} if broad else {'include_domains': domains}))
    write(archive / 'search.json', discovery)
    urls = [r['url'] for r in discovery.get('results', []) if r.get('url')][:max_pages]
    if not urls:
        return []
    for url in urls:
        public_url(url)
    fetched = tavily.extract(urls=urls, extract_depth='advanced', include_images=True, format='markdown')
    write(archive / 'sources.json', fetched)
    candidates = []
    from .research_digest import choose_passages
    for page in fetched.get('results', []):
        caption = page.get('raw_content', '')
        if not caption or page.get('url') not in urls:
            continue
        images = page.get('images', [])
        # Markdown binds each image URL to the fetched page and its caption.
        images += re.findall(r'!\[[^\]]*\]\((https?://[^\s)]+)', caption)
        source_count = 0
        matches = list(re.finditer(r'!\[([^\]]*)\]\((https?://[^\s)]+)[^)]*\)', caption))
        excerpt = '\n\n'.join(p['text'] for p in choose_passages(caption, person['name'] + ' photograph portrait date credit', budget=3000))
        for image in images:
            image_url = image.get('url') if isinstance(image, dict) else image
            if not isinstance(image_url, str) or not image_url.startswith(('http://', 'https://')):
                continue
            if any(c['image_url'] == image_url for c in candidates):
                continue
            association = next((i for i, match in enumerate(matches) if match.group(2) == image_url), None)
            if association is not None:
                match = matches[association]
                end = matches[association+1].start() if association+1 < len(matches) else len(caption)
                local_caption = (match.group(1) + '\n' + caption[match.end():end].strip())[:1800]
            else:
                local_caption = excerpt
            candidates.append({'image_url': image_url, 'source_url': page['url'], 'title': page['url'],
                'caption': local_caption, 'page_context': excerpt, 'date': '', 'artist': '', 'license': 'Not supplied by source extraction',
                'license_url': '', 'source_type': 'Fetched source page and its extracted image URLs'})
            source_count += 1
            if len(candidates) >= maximum:
                return candidates
            if source_count >= 2:
                break
    return candidates


def selection_errors(value, candidates, person):
    if not isinstance(value, dict) or value.get('status') not in {'ready', 'unavailable'}:
        return ['Return status ready or unavailable']
    if not isinstance(value.get('reason'), str) or not value['reason'].strip():
        return ['Explain the source-backed selection or why no reference is suitable']
    if value['status'] == 'unavailable':
        return []
    candidate = next((c for c in candidates if c['id'] == value.get('candidate_id')), None)
    errors = []
    if not candidate:
        return ['Select only a supplied candidate_id']
    quote = value.get('caption_quote')
    # NBSP/newline normalization is formatting, not an invented quote.
    normalize = lambda s: re.sub(r'\s+', ' ', s).strip()
    if not isinstance(quote, str) or len(quote) < 8 or normalize(quote) not in normalize(candidate['caption']):
        errors.append('caption_quote must be an exact excerpt of the fetched candidate caption')
    if value.get('depiction_kind') != person['reference_kind']:
        errors.append('Use the requested photograph or historical_portrait kind; do not invent a photograph')
    if not isinstance(value.get('appearance_fit'), str) or not value['appearance_fit'].strip():
        errors.append('Explain the reference age/period fit and any uncertainty')
    return errors


def research_person(person, settings, archive, models, store, tavily_factory, search_root=None):
    """search_root holds the source searches for this name, shared by every scene and chapter of the film."""
    search_root = search_root or archive
    result_file = archive / 'result.json'
    if result_file.exists():
        result = read(result_file)
        if result['status'] == 'ready':
            validate_reference(result)
        return result
    errors, candidates = [], []
    avoid_wikimedia = False
    commons_file = search_root / 'commons.json'
    try:
        candidates = read(commons_file) if commons_file.exists() else commons_candidates(person, settings['max_candidates'])
        write(commons_file, candidates)
    except (httpx.HTTPError, OSError, ValueError) as exc:
        avoid_wikimedia = isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 403
        errors.append(str(exc))
        store.event('person-reference', 'source_unavailable', {'name': person['name'], 'source': 'Commons', 'error': str(exc)})
    task = (
        'Select a usable documentary appearance reference for the named public historical figure and stated life period. '
        'Use only the supplied fetched source captions, file titles, image URLs and metadata. A search match alone '
        'does not prove identity. The page must explicitly associate this particular portrait/image with the person; '
        'reject unrelated decorative photos, group photos, modern actors, AI reconstructions and another person with '
        'the same name. If multiple people/images occur on a page, require an explicit image-specific caption or '
        'URL/alt-label association, not simply a biography title. Prefer a clearly credited archival photograph '
        'for photograph requests. A dated portrait of a person of the photographic era held by a reputable archive '
        '(Nobel Foundation, government, university, museum or library collection) counts as a photograph unless its '
        'metadata indicates a painting, drawing or sculpture; the metadata need not state the medium. '
        'historical_portrait requests require a documented portrait/sculpture depiction '
        'and must not be claimed as a camera photograph. Prefer the requested life period and age. '
        'A documented adult likeness from a different adult period is usable for facial features if appearance_fit '
        'explicitly states the source period and necessary aging adjustment for the requested scene. A child portrait '
        'or modern impersonator is not a usable adult reference. Disclose uncertain portrait authenticity/age. '
        'Do not infer or identify a person from pixels. Identity support comes from fetched metadata. '
        'If no supplied candidate qualifies return unavailable with a reason. Return '
        '{status:"ready"|"unavailable", candidate_id:string|null, caption_quote:string copied exactly from '
        'the selected caption, depiction_kind:"photograph"|"historical_portrait", appearance_fit:string, reason:string}. '
        'Never invent an image URL or a caption. Keep the response concise.'
    )
    # Reserve one fetched page for broader discovery rather than assuming the
    # listed international archives cover every country's public figures.
    primary_pages = max(1, settings['max_source_pages'] - 1)
    for source_pass in range(3):
        if source_pass:
            max_pages = primary_pages if source_pass == 1 else settings['max_source_pages'] - primary_pages
            if not max_pages:
                continue
            try:
                search_archive = search_root / ('primary-search-v4' if source_pass == 1 else 'broader-search-v4')
                fallback_file = search_archive / 'candidates.json'
                candidates = (read(fallback_file) if fallback_file.exists() else tavily_candidates(person,
                    tavily_factory(), settings['max_candidates'], max_pages, search_archive,
                    avoid_wikimedia=avoid_wikimedia, broad=source_pass == 2))
                write(fallback_file, candidates)
            except Exception as exc:
                errors.append(str(exc)); candidates = []
                store.event('person-reference', 'source_unavailable', {'name': person['name'], 'source': 'research search', 'error': str(exc)})
        candidates = [{**c, 'id': f'S{i+1:02d}'} for i, c in enumerate(candidates)]
        write(archive / f'candidates-{source_pass}.json', candidates)
        while candidates:
            try:
                value = models.loop('person-reference-' + archive.name + f'-source-{source_pass}',
                    task + ' Select a single-person portrait, not a montage or group photograph. '
                    'Caption whitespace may be normalized, but preserve its exact words and punctuation.',
                    {'person': person, 'candidates': candidates},
                    lambda v: selection_errors(v, candidates, person), experimental=True)
            except (RuntimeError, ModelOutputError) as exc:
                errors.append(str(exc))
                store.event('person-reference', 'selection_needs_review', {'name': person['name'], 'error': str(exc)})
                break
            write(archive / f'selection-{source_pass}.json', value)
            if value['status'] == 'unavailable':
                errors.append(value['reason']); break
            chosen = next(c for c in candidates if c['id'] == value['candidate_id'])
            try:
                downloaded = download_portrait(chosen['image_url'], archive / 'portrait.png')
            except (httpx.HTTPError, OSError, ValueError) as exc:
                errors.append(str(exc)); candidates = [c for c in candidates if c['id'] != chosen['id']]
                store.event('person-reference', 'download_failed', {'name': person['name'], 'source_url': chosen['source_url'], 'error': str(exc)})
                continue
            result = {**downloaded, 'protocol': PROTOCOL, 'status': 'ready', 'person': person,
                'source_url': chosen['source_url'], 'image_url': chosen['image_url'],
                'license': chosen['license'], 'license_url': chosen['license_url'], 'artist': chosen['artist'],
                'date': chosen['date'], 'depiction_kind': value['depiction_kind'],
                'selection': value, 'source_type': chosen['source_type']}
            write(result_file, result)
            return result
    result = {'protocol': PROTOCOL, 'status': 'unavailable', 'person': person,
              'reason': '; '.join(errors) or 'No documented portrait reference was available'}
    write(result_file, result)
    return result


def main():
    request = read(sys.argv[1]); directory = Path(request['directory'])
    store = Store(request['project']); models = Models(request['model_config'], store)
    try:
        def tavily_factory():
            return ReferenceSearchClient()
        results = {}
        for position, (key, person) in enumerate(request['people'].items(), 1):
            person = planned_kind(person)     # normally already applied by prepare_person_references
            urls = {k: request['model_config'][k] for k in ('planner_url', 'reviewer_url') if k in request['model_config']}
            archive_id = digest({'protocol': PROTOCOL, 'person': person, 'settings': request['settings'],
                                 'models': urls})[:20]
            archive = Path(request['cache_root']) / 'people' / archive_id
            # Scenes describe the same person with different appearance_context; the portrait and its source
            # searches depend only on who it is and the kind of depiction, so they are shared across the film.
            by_name = Path(request['cache_root']) / 'people-by-name' / digest({'protocol': PROTOCOL,
                'name': ' '.join(person['name'].casefold().split()), 'reference_kind': person['reference_kind'],
                'settings': request['settings'], 'models': urls})[:20]
            store.event('person-reference', 'running', {'name': person['name'], 'appearance_context': person['appearance_context'],
                        'position': position, 'total': len(request['people'])})
            shared = by_name / 'result.json'
            if not (archive / 'result.json').exists() and shared.exists():
                result = {**read(shared), 'person': person}
                validate_reference(result)
                write(archive / 'result.json', result)
                store.event('person-reference', 'reused', {'name': person['name'], 'source_url': result.get('source_url')})
            else:
                result = research_person(person, request['settings'], archive, models, store, tavily_factory, by_name / 'search')
                if result['status'] == 'ready' and not shared.exists():
                    write(shared, result)
            results[key] = result
            store.event('person-reference', result['status'], {'name': person['name'], 'position': position,
                'total': len(request['people']), 'source_url': result.get('source_url'), 'reason': result.get('reason')})
        write(directory / 'result.json', {'protocol': PROTOCOL, 'people': results})
    finally:
        store.db.close()


if __name__ == '__main__':
    main()
