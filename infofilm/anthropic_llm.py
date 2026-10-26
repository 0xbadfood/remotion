"""Claude through the native Anthropic Messages API (user, 2026-10-26: off OpenRouter, onto Anthropic directly).

Two uses: one JSON call (the planner and reviewer roles) and one turn of the research agent, whose loop speaks the
chat-completions shape (tool_calls, role "tool"); the agent turn is translated at this boundary so the loop stays
as it is. A billing failure (credit balance too low) stops the job at once: retrying it only burns the run's time.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

import anthropic

MEDIA = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp', '.gif': 'image/gif'}


class LLMBillingError(RuntimeError):
    """The account can't pay for the call; the job stops rather than retrying."""


def client(key, timeout=900):
    # The SDK retries connection errors, 408/409/429 and 5xx (529 overloaded included) with backoff.
    return anthropic.Anthropic(api_key=key, timeout=float(timeout), max_retries=6)


def _billing(exc):
    text = str(getattr(exc, 'message', '') or exc).lower()
    return 'credit balance' in text or 'billing' in text or getattr(exc, 'status_code', None) == 402


def _image(path):
    path = Path(path)
    return {'type': 'image', 'source': {'type': 'base64', 'media_type': MEDIA.get(path.suffix.lower(), 'image/png'),
                                        'data': base64.standard_b64encode(path.read_bytes()).decode()}}


def _final(cli, label, **params):
    """Stream the request (long thinking + answer would otherwise risk an HTTP timeout) and return the message."""
    try:
        with cli.messages.stream(**params) as stream:
            message = stream.get_final_message()
    except anthropic.APIStatusError as exc:
        if _billing(exc):
            raise LLMBillingError(f'{label}: the Anthropic account cannot pay for this call ({exc.message}); '
                                  'the job stops here: top up the credit balance, then resume the job') from exc
        raise
    if message.stop_reason == 'refusal':
        details = message.stop_details
        raise RuntimeError(f'{label} declined the request (refusal'
                           + (f': {details.category}' if details and details.category else '') + ')')
    return message


def usage(message):
    """The usage block, with the chat-completions names the run's token accounting reads."""
    u = message.usage.to_dict()
    return {**u, 'prompt_tokens': u.get('input_tokens', 0) + u.get('cache_read_input_tokens', 0)
            + u.get('cache_creation_input_tokens', 0), 'completion_tokens': u.get('output_tokens', 0)}


def json_call(connection, key, *, system, prompt, images, max_tokens, label):
    """One JSON answer. Thinking is adaptive (always on for current Claude models) at the connection's effort, and
    counts inside max_tokens, so it gets room on top of the answer budget. Returns (raw, text, truncated)."""
    content = [_image(p) for p in images] + [{'type': 'text', 'text': prompt}]
    message = _final(client(key, connection.get('timeout', 900)), label, model=connection['model'],
                     max_tokens=max_tokens, system=system, messages=[{'role': 'user', 'content': content}],
                     output_config={'effort': connection.get('reasoning_effort', 'low')})
    text = ''.join(b.text for b in message.content if b.type == 'text')
    raw = message.to_dict()
    raw['usage'] = usage(message)
    return raw, text, message.stop_reason == 'max_tokens'


def _tools(tools):
    return [{'name': t['function']['name'], 'description': t['function'].get('description', ''),
             'input_schema': t['function'].get('parameters') or {'type': 'object', 'properties': {}}}
            for t in tools or [] if t.get('type') == 'function']


def _messages(messages):
    """Chat-completions messages as (system, Messages API messages): tool results of one turn go back together in a
    single user message, as Claude's parallel tool use expects."""
    system, out = [], []
    for m in messages:
        role = m.get('role')
        if role == 'system':
            system.append(str(m.get('content') or ''))
        elif role == 'tool':
            block = {'type': 'tool_result', 'tool_use_id': m['tool_call_id'], 'content': str(m.get('content') or '')}
            if out and out[-1]['role'] == 'user' and isinstance(out[-1]['content'], list) \
                    and out[-1]['content'] and out[-1]['content'][-1].get('type') == 'tool_result':
                out[-1]['content'].append(block)
            else:
                out.append({'role': 'user', 'content': [block]})
        elif role == 'assistant':
            blocks = [{'type': 'text', 'text': m['content']}] if str(m.get('content') or '').strip() else []
            for call in m.get('tool_calls') or []:
                try:
                    arguments = json.loads(call['function'].get('arguments') or '{}')
                except json.JSONDecodeError:
                    arguments = {}
                blocks.append({'type': 'tool_use', 'id': call['id'], 'name': call['function']['name'], 'input': arguments})
            out.append({'role': 'assistant', 'content': blocks or [{'type': 'text', 'text': '(no answer)'}]})
        else:
            out.append({'role': 'user', 'content': str(m.get('content') or '')})
    return '\n\n'.join(system), out


def agent_turn(cli, model, messages, tools, *, max_tokens, label='research agent'):
    """One research-agent turn: chat-completions messages and tools in, a chat-completions assistant message out
    ({role, content, tool_calls}), with the finish reason and usage. Thinking stays off here (as on the local model):
    the loop keeps only text and tool calls, and Claude needs its thinking blocks back between tool calls."""
    system, converted = _messages(messages)
    system += ('\n\nWhen you use a tool, you may say a brief sentence first. If no tool can express what is asked, '
               'say so instead of guessing. Do not include internal or system XML tags in your response.')
    params = {'model': model, 'max_tokens': max_tokens, 'system': system.strip(), 'messages': converted,
              'thinking': {'type': 'disabled'}, 'output_config': {'effort': 'low'}}
    if tools:
        params.update(tools=_tools(tools), tool_choice={'type': 'auto'})
    message = _final(cli, label, **params)
    text = ''.join(b.text for b in message.content if b.type == 'text')
    calls = [{'id': b.id, 'type': 'function', 'function': {'name': b.name, 'arguments': json.dumps(b.input)}}
             for b in message.content if b.type == 'tool_use']
    reply = {'role': 'assistant', 'content': text, **({'tool_calls': calls} if calls else {})}
    finish = {'end_turn': 'stop', 'tool_use': 'tool_calls', 'max_tokens': 'length'}.get(message.stop_reason, message.stop_reason)
    return reply, finish, usage(message)
