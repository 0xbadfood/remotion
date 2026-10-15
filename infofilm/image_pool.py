"""Image worker pool: independent image tasks (one asset's render, review and repair loop) run up to `size` at once.

The pool knows nothing about where images are made. Its size, 1-N, comes from the `pools` section of
image-providers.json, per provider: a number, or "auto" to use what the provider reports (the Qwen image factory: one
worker per loaded GPU). This keeps one shape for a single GPU (1), the two-GPU factory, or a rate-limited cloud
service (a small fixed size). Several factory nodes will sit behind one wrapper API that manages them (user, 2026-10-15):
the pool still sees one endpoint, and the wrapper reports the workers of all nodes in its status (`factory.dispatch`,
one entry per worker), so "auto" sizes the pool to the whole fleet. `max_in_flight` is a separate, cross-process cap on
jobs at one endpoint, shared by parallel chapters and films. Pool settings are kept out of the provider entries so they
never change an image checkpoint's identity.
"""
from __future__ import annotations

import concurrent.futures
import contextlib
import threading

import httpx

from .core import digest, resource_slot

MAX_POOL = 64


class ImageWorkerPool:
    """Runs work(task) for every task with at most `size` running at once. Shared state is guarded by `lock`. A task
    that raises stops new tasks from starting; tasks already running finish (and keep their checkpoints), then the
    first error is raised. Size 1 runs the tasks in order on the calling thread."""

    def __init__(self, size):
        if type(size) is not int or not 1 <= size <= MAX_POOL:
            raise ValueError(f"Image pool size must be 1-{MAX_POOL}")
        self.size, self.lock = size, threading.Lock()

    def run(self, tasks, work):
        tasks, stop, failures = list(tasks), threading.Event(), []

        def one(task):
            if stop.is_set():
                return
            try:
                work(task)
            except Exception as exc:          # noqa: BLE001 - re-raised below, after the running tasks finish
                stop.set()
                with self.lock:
                    failures.append(exc)

        workers = min(self.size, len(tasks))
        if workers <= 1:
            for task in tasks:
                one(task)
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                list(pool.map(one, tasks))
        if failures:
            raise failures[0]


def validate_pools(registry):
    pools = registry.get("pools", {})
    if not isinstance(pools, dict):
        raise ValueError("Image pools must be an object")
    for name, pool in pools.items():
        if name not in registry["providers"] or not isinstance(pool, dict):
            raise ValueError(f"Image pool for unknown provider: {name}")
        size = pool.get("size", 1)
        if size != "auto" and (type(size) is not int or not 1 <= size <= MAX_POOL):
            raise ValueError(f"Image pool {name}.size must be 1-{MAX_POOL} or \"auto\"")
        limit = pool.get("max_in_flight")
        if limit is not None and (type(limit) is not int or not 1 <= limit <= 4 * MAX_POOL):
            raise ValueError(f"Image pool {name}.max_in_flight must be a positive integer")


def _pool(config):
    registry = config.get("image_provider_registry") or {}
    return (registry.get("pools") or {}).get(config.get("image_provider_id"), {})


def reported_capacity(config):
    """Workers the provider says it runs. Only the Qwen factory reports them (its status lists one dispatch entry per
    GPU worker); anything else, or a status that cannot be read, counts as one."""
    registry = config.get("image_provider_registry") or {}
    provider = (registry.get("providers") or {}).get(config.get("image_provider_id"), {})
    if provider.get("adapter", "qwen_async") != "qwen_async" or config.get("image_engine"):
        return 1
    from . import media
    try:
        status = media.request({**config, "provider_wait_seconds": 0}, "image", "GET", "/api/status", timeout=10)
        return max(1, min(MAX_POOL, len((status.get("factory") or {}).get("dispatch") or [])))
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        return 1


def pool_size(config):
    """`image_pool_size` in the run's config overrides (experiments); otherwise the provider's pool; otherwise 1."""
    size = config.get("image_pool_size") or _pool(config).get("size", 1)
    return reported_capacity(config) if size == "auto" else int(size)


def image_pool(config):
    return ImageWorkerPool(pool_size(config))


def in_flight(config):
    """Cross-process cap on jobs at this provider's endpoint, when its pool sets `max_in_flight`."""
    limit = _pool(config).get("max_in_flight")
    if not limit:
        return contextlib.nullcontext()
    return resource_slot("image-endpoint-" + digest(config["image_url"])[:12], limit)
