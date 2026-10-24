"""Media factory: LuxTTS narration and MMS word alignment on the amdserver (user, 2026-10-24: "transfer narration to
amdserver lux and align"; it shares GPU 1 with HiDream, which is idle while a film narrates).

  POST /api/tts      form `manifest` (JSON: model, speed, num_steps, t_shift, seed, rms, reference_text,
                     tasks [{id, index, text}]) and file `reference` -> job; clips are mono PCM16 WAV at 48 kHz
  GET  /api/jobs/{id}                 status, finished clip ids, timings
  GET  /outputs/{id}/{clip}.wav       one clip
  POST /api/align    file `audio`, form `words` (JSON list, already tokenised by the caller) -> {payload, metadata}
                     in the MFA runner's shape (mms_align.py, the same code infofilm runs locally)
  GET  /api/status   POST /api/load   (load both models; they stay resident: LuxTTS ~1 GB, MMS ~1.2 GB)

Clips match infofilm's lux_worker.py exactly (same seeding: seed*1000 + index), so a remote and a local take of the
same manifest are the same audio. One GPU job runs at a time; jobs and clips are kept 4 hours.
"""
import hashlib
import json
import os
import queue as jobqueue
import shutil
import sys
import tempfile
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Annotated

import numpy as np
import soundfile as sf
import torch
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / 'LuxTTS'))
sys.path.insert(0, str(HERE))
import mms_align  # noqa: E402

DATA = Path(os.environ.get('MF_DATA', HERE / 'data')) / 'jobs'
DATA.mkdir(parents=True, exist_ok=True)
RETENTION = float(os.environ.get('MF_RETENTION_HOURS', '4')) * 3600
DEVICE = 'cuda' if torch.cuda.is_available() else 'cpu'

app = FastAPI(title='Media factory (LuxTTS narration, MMS alignment)')
gpu = threading.Lock()
jobs, pending = {}, jobqueue.Queue()
models = {'lux': {}, 'prompts': {}}
state = {'state': 'unloaded', 'detail': ''}


def lux(model):
    if model not in models['lux']:
        from zipvoice.luxvoice import LuxTTS
        models['lux'][model] = LuxTTS(model, device=DEVICE, threads=4)
    return models['lux'][model]


def prompt(tts, model, reference, reference_text, rms):
    key = (model, hashlib.sha256(reference.read_bytes()).hexdigest(), reference_text, rms)
    if key not in models['prompts']:
        if reference_text:
            tts.transcriber = lambda _audio, known=reference_text: {'text': known}
        models['prompts'][key] = tts.encode_prompt(str(reference), rms=rms, duration=1000)
    return models['prompts'][key]


def run_tts(job):
    folder = DATA / job['id']
    manifest = json.loads((folder / 'manifest.json').read_text())
    job.update(status='running', started_at=time.time())
    with gpu:
        started = time.monotonic()
        tts = lux(manifest['model'])
        encoded = prompt(tts, manifest['model'], folder / 'reference.wav', manifest.get('reference_text'),
                         manifest.get('rms', 0.01))
        job['ready_seconds'] = round(time.monotonic() - started, 2)
        for task in manifest['tasks']:
            torch.manual_seed(int(manifest.get('seed', 0)) * 1000 + int(task.get('index', 0)))
            t = time.monotonic()
            wav = tts.generate_speech(task['text'], encoded, num_steps=manifest.get('num_steps', 4),
                                      t_shift=manifest.get('t_shift', 0.5), speed=manifest['speed']).numpy().squeeze()
            wav = np.clip(wav.astype(np.float32), -1, 1)
            part = folder / f"{task['id']}.wav.part"
            sf.write(part, wav, 48000, subtype='PCM_16', format='WAV')
            part.replace(folder / f"{task['id']}.wav")
            job['clips'].append({'id': task['id'], 'seconds': round(len(wav) / 48000, 3),
                                 'took': round(time.monotonic() - t, 3)})
    job.update(status='complete', finished_at=time.time())


def worker():
    while True:
        job = pending.get()
        try:
            run_tts(job)
        except Exception as exc:  # noqa: BLE001 - the caller sees the error and may retry or narrate locally
            job.update(status='error', message=f'{type(exc).__name__}: {exc}'[:500], finished_at=time.time())
            traceback.print_exc()


def cleanup():
    while True:
        now = time.time()
        for job_id, job in list(jobs.items()):
            if job['status'] in ('complete', 'error') and now - job.get('finished_at', now) > RETENTION:
                shutil.rmtree(DATA / job_id, ignore_errors=True)
                jobs.pop(job_id, None)
        time.sleep(600)


@app.on_event('startup')
def startup():
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=cleanup, daemon=True).start()


@app.post('/api/load')
def load(model: str = 'YatharthS/LuxTTS'):
    state.update(state='loading', detail='Loading LuxTTS and MMS')
    try:
        with gpu:
            lux(model)
            mms_align._model(DEVICE)
        state.update(state='loaded', detail='LuxTTS and MMS resident')
    except Exception as exc:  # noqa: BLE001
        state.update(state='error', detail=f'{type(exc).__name__}: {exc}'[:300])
    return state


@app.get('/api/status')
def status():
    return {**state, 'device': DEVICE, 'gpu': torch.cuda.get_device_name(0) if DEVICE == 'cuda' else None,
            'allocated_gb': round(torch.cuda.memory_allocated() / 1e9, 2) if DEVICE == 'cuda' else 0,
            'busy': gpu.locked(), 'queued': pending.qsize(), 'jobs': len(jobs)}


@app.post('/api/tts', status_code=202)
async def tts_job(manifest: Annotated[str, Form()], reference: Annotated[UploadFile, File()]):
    value = json.loads(manifest)
    if not value.get('tasks') or 'speed' not in value or 'model' not in value:
        raise HTTPException(422, 'manifest needs model, speed and tasks')
    job_id = uuid.uuid4().hex
    folder = DATA / job_id
    folder.mkdir(parents=True)
    (folder / 'manifest.json').write_text(json.dumps(value))
    (folder / 'reference.wav').write_bytes(await reference.read())
    job = {'id': job_id, 'status': 'queued', 'clips': [], 'created_at': time.time(), 'tasks': len(value['tasks'])}
    jobs[job_id] = job
    pending.put(job)
    return job


@app.get('/api/jobs/{job_id}')
def job_status(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, 'Job not found (unknown, or older than the retention window)')
    return jobs[job_id]


@app.get('/outputs/{job_id}/{clip}.wav')
def clip(job_id: str, clip: str):
    path = DATA / job_id / f'{clip}.wav'
    if job_id not in jobs or '/' in clip or not path.is_file():
        raise HTTPException(404, 'Clip not found')
    return FileResponse(path, media_type='audio/wav')


@app.post('/api/align')
async def align(audio: Annotated[UploadFile, File()], words: Annotated[str, Form()]):
    tokens = json.loads(words)
    with tempfile.NamedTemporaryFile(suffix='.wav') as handle:
        handle.write(await audio.read())
        handle.flush()
        with gpu:
            payload, metadata = mms_align.align(Path(handle.name), '', tokenize=lambda _text: tokens, device=DEVICE)
    return {'payload': payload, 'metadata': metadata}
