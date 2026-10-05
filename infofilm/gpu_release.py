"""Verify the installed VibeVoice worker actually releases its CUDA allocations."""
from __future__ import annotations

import subprocess
import time

LOCAL_VIBE_URLS = {"http://127.0.0.1:8092", "http://localhost:8092"}
UNIT = "vibevoice-api-local.service"
IDLE_LIMIT_MIB = 512


def _pid():
    result = subprocess.run(["systemctl", "show", UNIT, "--property=MainPID", "--value"],
                            check=True, capture_output=True, text=True, timeout=15)
    pid = int(result.stdout.strip())
    if pid <= 0:
        raise RuntimeError("VibeVoice API is reachable but its installed service has no main PID")
    return pid


def _memory(pid):
    result = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"],
                            check=True, capture_output=True, text=True, timeout=15)
    total = 0
    for row in result.stdout.splitlines():
        fields = [part.strip() for part in row.split(",")]
        if len(fields) != 2:
            raise RuntimeError("Cannot verify GPU memory: unexpected nvidia-smi response")
        if int(fields[0]) == pid:
            try:
                total += int(fields[1])
            except ValueError as exc:
                raise RuntimeError("Cannot verify VibeVoice GPU memory") from exc
    return total


def _idle(health):
    if health.get("active_job") or health.get("queued_or_running_jobs"):
        raise RuntimeError("VibeVoice has active work; refusing GPU release or service recycle")


def release_vibevoice(config, store=None, *, request_fn=None, allow_restart=True):
    """Call under the GPU lease, after native recordings have been checkpointed.

    The API can report model_loaded=null while live tensors still occupy VRAM.
    Only the known local idle unit may be recycled, using existing Polkit rights.
    Other CUDA processes and remote providers are never stopped.
    """
    if request_fn is None:
        from .media import request as request_fn

    def event(state, detail):
        if store is not None:
            store.event("gpu", state, detail)

    health = request_fn(config, "tts", "GET", "/health")
    _idle(health)
    local = config["tts_url"].rstrip("/") in LOCAL_VIBE_URLS
    if health.get("model_loaded") or local:
        result = request_fn(config, "tts", "POST", "/unload")
        if result.get("unload_disabled") or result.get("model_loaded") is not None:
            raise RuntimeError("VibeVoice did not unload; GPU consumers remain blocked")
    if not local:
        return {"gpu_released": True, "verification": "provider_api", "recycled": False}
    pid = _pid()
    memory = _memory(pid)
    if memory > IDLE_LIMIT_MIB:
        event("release_incomplete", {"unit": UNIT, "pid": pid, "used_mib": memory,
                                     "model_loaded": None})
        if not allow_restart:
            event("release_deferred", {"reason": "Provider outputs still need reconciliation", "used_mib": memory})
            return {"gpu_released": False, "used_mib": memory, "recycled": False}
        # Recheck just before stop: the lease does not govern external callers.
        _idle(request_fn(config, "tts", "GET", "/health"))
        event("service_recycling", {"unit": UNIT, "pid": pid, "used_mib": memory})
        manager = ["systemctl", "--no-ask-password"]
        try:
            subprocess.run(manager + ["stop", UNIT], check=True, capture_output=True, text=True, timeout=60)
            if _memory(pid):
                raise RuntimeError("Stopped VibeVoice still owns CUDA memory; shared GPU remains blocked")
        finally:
            subprocess.run(manager + ["start", UNIT], check=True, capture_output=True, text=True, timeout=60)
        deadline = time.monotonic() + 60
        while True:
            try:
                health = request_fn(config, "tts", "GET", "/health", timeout=3)
                break
            except Exception:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Recycled VibeVoice API did not become ready")
                time.sleep(1)
        _idle(health)
        memory = _memory(_pid())
        if health.get("model_loaded") or memory > IDLE_LIMIT_MIB:
            raise RuntimeError("Recycled VibeVoice still owns model VRAM; shared GPU remains blocked")
        event("service_recycled", {"unit": UNIT, "used_mib": memory, "gpu_released": True})
        return {"gpu_released": True, "verification": "nvidia_smi", "used_mib": memory, "recycled": True}
    event("released", {"unit": UNIT, "pid": pid, "used_mib": memory, "gpu_released": True})
    return {"gpu_released": True, "verification": "nvidia_smi", "used_mib": memory, "recycled": False}
