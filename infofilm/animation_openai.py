"""OpenAI Responses adapter for the isolated single-model animation pilot."""
from __future__ import annotations

import json
import os
import time

import httpx

from .core import digest, read, write
from .llm import ModelOutputError


class OutputBudgetError(ModelOutputError):
    output_budget_exhausted = True
    def __init__(self, budget, raw):
        self.budget, self.usage = budget, raw.get('usage', {})
        reasoning = self.usage.get('output_tokens_details', {}).get('reasoning_tokens', 0)
        super().__init__(f'OpenAI output budget exhausted at {budget} tokens '
                         f'({reasoning} reasoning tokens); incomplete response preserved')


class OpenAIAnimationModels:
    def __init__(self, config, store, *, max_output_recovery_tokens=64000, animation_output_tokens=48000):
        self.config, self.store = config, store
        self.model = config['planner_model']
        self.role = config.get('role', 'planner')
        base = config.get('max_output_tokens', 24000)
        if any(type(v) is not int or not 1000 <= v <= 128000
               for v in (base, max_output_recovery_tokens, animation_output_tokens)):
            raise ValueError('Invalid animation output-token budget')
        self.recovery_limit = max(base, max_output_recovery_tokens)
        self.animation_budget = min(self.recovery_limit, max(base, animation_output_tokens))

    def call(self, role, name, prompt, *, max_tokens=7000, **kwargs):
        if role != self.role or kwargs:
            raise ValueError('The single-model pilot permits text planning only; no reviewer calls')
        # JSON mode requires an explicit JSON instruction in input messages,
        # even when the separate Responses instructions already contain it.
        if 'json' not in prompt.lower():prompt='Return the requested complete JSON object.\n'+prompt
        budget = self.config.get('max_output_tokens', 24000)
        if role == 'planner' and name.startswith('animation-animation-'):
            budget = self.animation_budget
        while True:
            try:
                return self._call_once(role, name, prompt, budget)
            except OutputBudgetError as exc:
                if budget >= self.recovery_limit:
                    raise
                following = min(self.recovery_limit, budget * 2)
                self.store.event(name, 'output_budget_retry', {
                    'previous_max_output_tokens': budget, 'max_output_tokens': following,
                    'reasoning_tokens': exc.usage.get('output_tokens_details', {}).get('reasoning_tokens', 0),
                    'reported_output_tokens': exc.usage.get('output_tokens'),
                    'semantic_attempt_consumed': False})
                budget = following

    def _call_once(self, role, name, prompt, budget):
        settings = {'model': self.model, 'reasoning_effort': self.config['reasoning_effort'],
                    'max_output_tokens': budget}
        instructions=('Independently assess the supplied candidate against its objective, facts and renderer contract. '
                      'Identify concrete semantic defects and actionable repairs. Return only the requested JSON review. '
                      'Do not invent facts, constraints or capabilities. Runtime is advisory.' if role=='reviewer' else
                      'You own the complete educational animation plan. Return only the requested complete JSON object. '
                      'Use compact JSON and concise explanations. Retain all required objects, timing anchors and '
                      'teaching behaviors. Follow the renderer contract and supplied facts. '
                      'Do not claim review or generate executable code.')
        identity = digest({'adapter':'infofilm.animation-openai-responses.v2',
                           'url':self.config['planner_url'], 'prompt':prompt, 'settings':settings,
                           'instructions':instructions,
                           **({'role':role} if role!='planner' else {})})
        prefix = self.store.root/'requests'/f'{name}-{identity[:12]}'
        if prefix.with_suffix('.result.json').exists():
            return read(prefix.with_suffix('.result.json'))
        response_path = prefix.with_suffix('.response.json')
        if response_path.exists():
            saved = read(response_path)
            if saved.get('status') == 'incomplete' and (saved.get('incomplete_details') or {}).get('reason') == 'max_output_tokens':
                self.store.event(name, 'output_budget_failure_reused', {'max_output_tokens': budget})
                raise OutputBudgetError(budget, saved)
        key = os.environ.get('OPENAI_API_KEY') or os.environ.get('OPENAI_TOKEN')
        if not key:
            raise RuntimeError('Load OPENAI_API_KEY or OPENAI_TOKEN from your profile before running')
        body = {'model':self.model, 'instructions':instructions,
                'input':prompt, 'reasoning':{'effort':settings['reasoning_effort']},
                'max_output_tokens':settings['max_output_tokens'],
                'text':{'format':{'type':'json_object'}}, 'store':False}
        write(prefix.with_suffix('.request.json'), {'url':self.config['planner_url'], 'role':role,
              'model':self.model, 'prompt':prompt, 'instructions':instructions,
              'adapter':'infofilm.animation-openai-responses.v2', 'input_hash':identity, 'settings':settings})
        self.store.event(name, 'running', {'role':role, **settings})
        start = time.monotonic()
        with httpx.Client(timeout=900, headers={'Authorization':'Bearer '+key}) as client:
            response = client.post(self.config['planner_url']+'/responses', json=body)
            if response.status_code >= 400:
                try:
                    error = response.json().get('error', {})
                    message = str(error.get('message', 'Request failed')).replace(key, '[redacted]')
                except ValueError:
                    message = 'Request failed without a JSON error body'
                raise RuntimeError(f'OpenAI {response.status_code}: {message}')
            raw = response.json()
        raw['measured_latency_seconds'] = round(time.monotonic()-start, 3)
        write(prefix.with_suffix('.response.json'), raw)
        if raw.get('status') != 'completed':
            if (raw.get('incomplete_details') or {}).get('reason') == 'max_output_tokens':
                self.store.event(name, 'output_budget_exhausted', {'max_output_tokens': budget,
                    'latency_seconds':raw['measured_latency_seconds'], 'usage':raw.get('usage', {})})
                raise OutputBudgetError(budget, raw)
            raise ModelOutputError('OpenAI response did not complete: '+str(raw.get('incomplete_details') or raw.get('status')))
        parts = [part for item in raw.get('output', []) if item.get('type')=='message'
                 for part in item.get('content', [])]
        if any(part.get('type')=='refusal' for part in parts):
            raise ModelOutputError('OpenAI declined this plan; response preserved')
        text = ''.join(part.get('text','') for part in parts if part.get('type')=='output_text')
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ModelOutputError('OpenAI output is not valid JSON; response preserved') from exc
        if not isinstance(value,dict):
            raise ModelOutputError('OpenAI must return one JSON object; response preserved')
        write(prefix.with_suffix('.result.json'), value)
        self.store.event(name,'succeeded',{'model':raw.get('model',self.model),
            'latency_seconds':raw['measured_latency_seconds'],'usage':raw.get('usage',{})})
        return value
