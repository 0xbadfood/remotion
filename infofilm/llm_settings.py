"""LLM connections and the planner/reviewer assignment (the web app's LLM settings page).

Modelled on Lesson Studio's model settings: typed connections (a local OpenAI-compatible server, OpenRouter, the
OpenAI Responses API), two roles, and named configurations. API keys live in a separate mode-600 file and are never
returned to the browser or written into a job's config snapshot; workers read them again at call time.

A job freezes its endpoints: each role's `<role>_url` becomes `<url>#<connection id>` (so two roles on one base URL,
e.g. OpenRouter, still have distinct cache identities) and `llm_endpoints` maps that identity to the connection.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import httpx

TYPES = {
    'openai_chat': {'label': 'Local LLM (OpenAI-compatible URL)', 'needs_key': False, 'default_url': '',
                    'default_concurrency': 1},
    'openrouter': {'label': 'OpenRouter', 'needs_key': True, 'default_url': 'https://openrouter.ai/api/v1',
                   'default_concurrency': 8, 'key_env': 'OPENROUTER_API_KEY'},
    'openai_responses': {'label': 'OpenAI (Responses API, e.g. Luna)', 'needs_key': True,
                         'default_url': 'https://api.openai.com/v1', 'default_concurrency': 8, 'key_env': 'OPENAI_API_KEY'},
}
ROLES = {
    'planner': {'label': 'Planner', 'description': 'Drafts the film and chapter plans, evidence, scripts, visual beats, '
                'image prompts and repairs.', 'needs_images': False},
    'reviewer': {'label': 'Reviewer', 'description': 'Reviews every draft, checks generated images, summarises sources '
                 'and adjudicates narration checks. Use a different model from the planner (no self-review).',
                 'needs_images': True},
    'premium_image': {'label': 'Premium images', 'description': 'Image model used when the standard image (Qwen) keeps '
                      'failing review: an OpenRouter model with image output, e.g. google/gemini-3.1-flash-image. '
                      'Optional; without it the premium tier is unavailable.', 'needs_images': False, 'optional': True,
                      'types': ['openrouter']},
}
SETTINGS_FILE, SECRETS_FILE = 'llm-settings.json', 'llm-secrets.json'


def _id(label):
    return re.sub(r'[^a-z0-9]+', '-', label.lower()).strip('-')[:40] or 'connection'


def api_key(secrets_root, cid, connection):
    """A key saved in Settings wins; otherwise the service environment. Local servers use VLLM_API_KEY if set."""
    if connection.get('type', 'openai_chat') == 'openai_chat':
        return os.environ.get('VLLM_API_KEY')
    path = Path(secrets_root) / SECRETS_FILE if secrets_root else None
    saved = json.loads(path.read_text()).get(cid) if path and path.exists() else None
    names = [connection.get('key_env')] + (['OPENAI_TOKEN'] if connection.get('type') == 'openai_responses' else [])
    return saved or next((os.environ[n] for n in names if n and os.environ.get(n)), None)


def detect_models(url, key=None, timeout=8):
    response = httpx.get(url.rstrip('/') + '/models', timeout=timeout,
                         headers={'Authorization': 'Bearer ' + key} if key else {})
    response.raise_for_status()
    ids = [m.get('id') for m in response.json().get('data', []) if m.get('id')]
    if not ids:
        raise ValueError('The server answered but listed no models')
    return ids


class Registry:
    def __init__(self, root, config):
        self.root = Path(root)
        self.path, self.secret_path = self.root / SETTINGS_FILE, self.root / SECRETS_FILE
        self.config = config

    # ------------------------------------------------------------ storage
    def _defaults(self):
        """First run: the endpoints from config.json as local connections."""
        connections, roles = {}, {}
        for role in ROLES:
            url = self.config.get(role + '_url', '').split('#')[0]
            if not url:
                continue
            cid = _id('local ' + url.split('//')[-1].split('/')[0])
            connections.setdefault(cid, {'type': 'openai_chat', 'label': 'Local · ' + url.split('//')[-1].split('/')[0],
                                         'url': url, 'model': '', 'vision': True, 'timeout': 900, 'max_concurrency': 1})
            roles[role] = cid
        return {'connections': connections, 'roles': roles, 'configurations': {}}

    def load(self):
        data = json.loads(self.path.read_text()) if self.path.exists() else self._defaults()
        for key in ('connections', 'roles', 'configurations'):
            data.setdefault(key, {})
        return data

    def save(self, data):
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self.path)

    def _secrets(self):
        return json.loads(self.secret_path.read_text()) if self.secret_path.exists() else {}

    def _set_secret(self, cid, value):
        values = self._secrets()
        if value:
            values[cid] = value
        else:
            values.pop(cid, None)
        self.root.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.secret_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(values, f)

    # ------------------------------------------------------------ public view (no secrets)
    def public(self):
        data = self.load()
        connections = {cid: {**c, 'key_set': bool(api_key(self.root, cid, c)) if TYPES[c['type']]['needs_key'] else None,
                             'in_use': [r for r, v in data['roles'].items() if v == cid]}
                       for cid, c in data['connections'].items()}
        return {'types': TYPES, 'roles': [{'id': r, **spec} for r, spec in ROLES.items()], 'connections': connections,
                'assignments': data['roles'], 'configurations': data['configurations'],
                'active_configuration': self.matching(data)}

    # ------------------------------------------------------------ changes
    def upsert(self, payload):
        data = self.load()
        kind = payload.get('type')
        if kind not in TYPES:
            raise ValueError(f'Unknown connection type; choose one of {list(TYPES)}')
        label = str(payload.get('label') or '').strip()
        url = str(payload.get('url') or '').strip().rstrip('/')
        model = str(payload.get('model') or '').strip()
        if not label or not url.startswith(('http://', 'https://')) or (TYPES[kind]['needs_key'] and not model):
            raise ValueError('Give a name, an http(s) URL and a model')
        cid = payload.get('id') or _id(label)
        if not payload.get('id') and cid in data['connections']:
            cid = f'{cid}-{len(data["connections"]) + 1}'
        try:
            concurrency = int(payload.get('max_concurrency') or TYPES[kind]['default_concurrency'])
            timeout = int(payload.get('timeout') or 900)
        except (TypeError, ValueError):
            raise ValueError('Concurrency and timeout must be whole numbers')
        if not 1 <= concurrency <= 64 or not 30 <= timeout <= 3600:
            raise ValueError('Concurrency must be 1-64 and the timeout 30-3600 seconds')
        connection = {'type': kind, 'label': label[:80], 'url': url, 'model': model, 'vision': bool(payload.get('vision')),
                      'timeout': timeout, 'max_concurrency': concurrency}
        if kind == 'openai_responses':
            effort = payload.get('reasoning_effort') or 'low'
            if effort not in ('low', 'medium', 'high'):
                raise ValueError('Reasoning effort must be low, medium or high')
            connection['reasoning_effort'] = effort
        if TYPES[kind]['needs_key']:
            connection['key_env'] = payload.get('key_env') or TYPES[kind]['key_env']
        data['connections'][cid] = connection
        if payload.get('api_key'):
            self._set_secret(cid, str(payload['api_key']).strip())
        self.save(data)
        return cid

    def delete(self, cid):
        data = self.load()
        users = [r for r, v in data['roles'].items() if v == cid]
        users += [f'configuration "{c["label"]}"' for c in data['configurations'].values() if cid in c['roles'].values()]
        if users:
            raise ValueError(f'In use by: {", ".join(users)}. Assign or delete those first.')
        data['connections'].pop(cid, None)
        self.save(data)
        self._set_secret(cid, None)

    @staticmethod
    def _merged(current, roles):
        """Roles sent from the page over the saved ones; an optional role sent empty is cleared."""
        merged = {**current, **{r: c for r, c in roles.items() if c}}
        for role, cid in roles.items():
            if not cid and ROLES.get(role, {}).get('optional'):
                merged.pop(role, None)
        return merged

    def assign(self, roles):
        data = self.load()
        merged = self._merged(data['roles'], roles)
        self._check_roles(data, merged)
        data['roles'] = merged
        self.save(data)

    def _check_roles(self, data, roles):
        for role, cid in roles.items():
            if role not in ROLES or cid not in data['connections']:
                raise ValueError(f'Unknown role or connection: {role} → {cid}')
            if ROLES[role]['needs_images'] and not data['connections'][cid].get('vision'):
                raise ValueError(f'The {role} checks images; choose a connection marked as supporting images')
        for role, cid in roles.items():
            allowed = ROLES[role].get('types')
            if allowed and data['connections'][cid]['type'] not in allowed:
                raise ValueError(f'The {role} role needs a connection of type: {", ".join(allowed)}')
        missing = [r for r in ROLES if r not in roles and not ROLES[r].get('optional')]
        if missing:
            raise ValueError('Assign a connection to: ' + ', '.join(missing))

    # ------------------------------------------------------------ named configurations
    @staticmethod
    def matching(data):
        return next((pid for pid, p in data['configurations'].items() if p['roles'] == data['roles']), None)

    def save_configuration(self, label, note='', roles=None):
        """Save the roles selected on the page (they also become the current roles), not only the last saved ones."""
        data = self.load()
        label = str(label or '').strip()
        if not label:
            raise ValueError('Name the configuration')
        if roles:
            data['roles'] = self._merged(data['roles'], roles)
        self._check_roles(data, data['roles'])
        pid = _id(label)
        data['configurations'][pid] = {'label': label[:60], 'note': str(note or '').strip()[:400],
                                       'roles': dict(data['roles']), 'created': time.time()}
        self.save(data)
        return pid

    def _configuration(self, data, pid):
        if pid not in data['configurations']:
            raise ValueError('Unknown configuration: ' + str(pid))
        chosen = data['configurations'][pid]
        missing = sorted({c for c in chosen['roles'].values() if c not in data['connections']})
        if missing:
            raise ValueError('This configuration uses deleted connections: ' + ', '.join(missing))
        return chosen

    def apply_configuration(self, pid):
        data = self.load()
        data['roles'] = dict(self._configuration(data, pid)['roles'])
        self.save(data)

    def delete_configuration(self, pid):
        data = self.load()
        data['configurations'].pop(pid, None)
        self.save(data)

    # ------------------------------------------------------------ runtime
    def freeze(self, config, configuration=None):
        """Write the chosen endpoints into a job's config (no secrets; workers read keys at call time)."""
        data = self.load()
        roles = dict(self._configuration(data, configuration)['roles']) if configuration else dict(data['roles'])
        if configuration and 'premium_image' not in roles and data['roles'].get('premium_image'):
            # The premium image tier is an image setting, not part of an LLM pairing: a saved configuration that
            # does not name one (e.g. saved before the role existed) keeps the current choice.
            roles['premium_image'] = data['roles']['premium_image']
        if not configuration and not self.path.exists() and not {'planner', 'reviewer'} <= set(roles):
            config['llm_configuration'] = None      # nothing configured anywhere: leave the config as it is
            return config
        self._check_roles(data, roles)
        endpoints = {}
        for role, cid in roles.items():
            connection = data['connections'][cid]
            if role == 'premium_image':              # an image tier, not an LLM role: frozen separately
                config['premium_image_endpoint'] = {**connection, 'id': cid}
                continue
            identity = connection['url'] + '#' + cid
            config[role + '_url'] = identity
            endpoints[identity] = {**connection, 'id': cid}
        pid = configuration or self.matching({**data, 'roles': roles})
        config['llm_endpoints'] = endpoints
        config['llm_secrets_root'] = str(self.root)
        config['llm_configuration'] = {'id': pid, 'label': data['configurations'][pid]['label']} if pid else None
        return config


def llm_config(config):
    """The LLM part of a config, for workers that build their own Models (research, person references)."""
    return {k: config[k] for k in ('planner_url', 'reviewer_url', 'llm_endpoints', 'llm_secrets_root') if k in config}


def endpoint(config, role):
    """The connection behind a role. Without frozen settings: the legacy local server at <role>_url."""
    identity = config[role + '_url']
    found = (config.get('llm_endpoints') or {}).get(identity)
    if found:
        return found
    return {'type': 'openai_chat', 'url': identity, 'model': '', 'id': None, 'legacy': True}
