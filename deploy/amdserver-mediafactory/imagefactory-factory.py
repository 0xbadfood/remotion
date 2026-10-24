"""Image factory: one public Qwen Image API in front of one worker per GPU.

Speaks the same API as a single Qwen Image 2.1 Studio (POST /api/generate, GET /api/jobs/{id}, GET /outputs/<file>,
GET /api/status, POST /api/load, POST /api/unload), so infofilm can point its image_url here unchanged.

Two queues:
  input queue    accepted jobs waiting for a GPU; bounded by IF_INPUT_QUEUE (default 4); a full queue answers 429
  dispatch       one slot per worker; a worker takes the next input job as soon as it is free
Every job and its image live under IF_DATA/jobs/<id>/ and survive restarts. Finished jobs (records and images) are
deleted IF_RETENTION_HOURS (default 4) after they finish; worker copies are removed as soon as they are collected.

Models (2026-10-24, user: klein on GPU 0 for first takes, HiDream-O1 Dev on GPU 1 for the takes after two failed
reviews): IF_WORKER_KINDS names each worker's model (default klein for all). A job asks for one with the optional
`model` form field; a worker takes only jobs for its own model. A model no worker serves falls back to the first
worker's (`model_fallback` on the job), so a caller never waits forever.
"""
import asyncio
import json
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Annotated, Any

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

DATA = Path(os.environ.get('IF_DATA', Path.home() / 'imagefactory' / 'data'))
JOBS = DATA / 'jobs'
WORKERS = [w.strip().rstrip('/') for w in os.environ.get('IF_WORKERS', 'http://127.0.0.1:8176,http://127.0.0.1:8177').split(',')]
_OUTPUTS = [Path(p) for p in os.environ.get('IF_WORKER_OUTPUTS', str(Path.home() / 'projects' / 'qwenimage2.1' / 'outputs')).split(',')]
WORKER_OUTPUTS = (_OUTPUTS * len(WORKERS))[:len(WORKERS)] if len(_OUTPUTS) == 1 else _OUTPUTS   # one folder per worker
KINDS = [k.strip() for k in os.environ.get('IF_WORKER_KINDS', ','.join(['klein'] * len(WORKERS))).split(',')]
INPUT_QUEUE = int(os.environ.get('IF_INPUT_QUEUE', '4'))
RETENTION = float(os.environ.get('IF_RETENTION_HOURS', '4')) * 3600
JOBS.mkdir(parents=True, exist_ok=True)

app = FastAPI(title='Image factory (Qwen Image 2.1 on both GPUs)')
jobs: dict[str, dict[str, Any]] = {}
queue: list[str] = []                 # input queue, oldest first
wake = asyncio.Event()
workers = [{'url': url, 'gpu': i, 'kind': KINDS[i], 'outputs': WORKER_OUTPUTS[i], 'job': None, 'done': 0, 'seconds': 0.0}
           for i, url in enumerate(WORKERS)]
DEFAULT_KIND = KINDS[0]
client = httpx.AsyncClient(timeout=httpx.Timeout(60, connect=5))


def folder(job_id):
    return JOBS / job_id


def save(job):
    path = folder(job['id']) / 'job.json'
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(job))
    tmp.replace(path)


def public(job):
    out = {k: v for k, v in job.items() if not k.startswith('_')}
    if job['status'] == 'queued' and job['id'] in queue:
        out['queue_position'] = queue.index(job['id']) + 1
    return out


def average_seconds():
    done = [w for w in workers if w['done']]
    return sum(w['seconds'] for w in done) / max(1, sum(w['done'] for w in done)) if done else 60.0


def restore():
    """Reload jobs after a restart: finished ones keep their tokens; interrupted ones go back to the queue."""
    for path in sorted(JOBS.glob('*/job.json'), key=lambda p: p.stat().st_mtime):
        try:
            job = json.loads(path.read_text())
        except Exception:
            shutil.rmtree(path.parent, ignore_errors=True)
            continue
        if job['status'] in ('queued', 'running'):
            job.update(status='queued', message='Requeued after a restart', gpu=None)
            job.pop('_worker_job', None)
            queue.append(job['id'])
            save(job)
        jobs[job['id']] = job


# ------------------------------------------------------------------ dispatch: one loop per GPU worker
def mine(worker):
    """The oldest waiting job for this worker's model."""
    return next((j for j in queue if jobs.get(j, {}).get('model', DEFAULT_KIND) == worker['kind']), None)


async def worker_loop(worker):
    while True:
        wake.clear()                       # clear before looking, so a job added meanwhile still wakes us
        if not mine(worker):
            await wake.wait()
            continue
        if not await worker_ready(worker):
            await asyncio.sleep(5)
            continue
        job_id = mine(worker)
        if not job_id:                     # another GPU of the same model took it meanwhile
            continue
        queue.remove(job_id)
        job = jobs.get(job_id)
        if not job:
            continue
        worker['job'] = job_id
        try:
            await run_on(worker, job)
        except Exception as exc:
            job['_attempts'] = job.get('_attempts', 0) + 1
            if job['_attempts'] < 2:
                job.update(status='queued', message=f'Retrying after a worker problem: {exc}'[:300], gpu=None)
                queue.insert(0, job_id)
                wake.set()
            else:
                job.update(status='error', message=f'{type(exc).__name__}: {exc}'[:500], finished_at=time.time())
            save(job)
        finally:
            worker['job'] = None


async def worker_ready(worker):
    try:
        return (await client.get(worker['url'] + '/api/status', timeout=5)).json().get('state') == 'loaded'
    except Exception:
        return False


async def run_on(worker, job):
    params = json.loads((folder(job['id']) / 'request.json').read_text())
    files = [('references', (p.name, p.read_bytes())) for p in sorted(folder(job['id']).glob('ref-*'))]
    job.update(status='running', message='Creating image…', gpu=worker['gpu'], started_at=time.time())
    save(job)
    response = await client.post(worker['url'] + '/api/generate', data={k: str(v) for k, v in params.items()},
                                 files=files or None)
    if response.status_code >= 400:
        raise RuntimeError(f'worker refused the job ({response.status_code}): {response.text[:200]}')
    worker_job = response.json()['id']
    job['_worker_job'] = worker_job
    while True:
        await asyncio.sleep(1)
        state = (await client.get(f"{worker['url']}/api/jobs/{worker_job}")).json()
        for key in ('phase', 'step', 'steps'):
            if key in state:
                job[key] = state[key]
        if state['status'] == 'complete':
            break
        if state['status'] == 'error':
            job.update(status='error', message=state.get('message', 'worker error'), finished_at=time.time())
            save(job)
            return
    name = state['url'].rsplit('/', 1)[-1]
    suffix = name.rsplit('.', 1)[-1]
    target = folder(job['id']) / f"image.{suffix}"
    source = worker['outputs'] / name
    if source.exists():
        shutil.move(source, target)            # collect and remove the worker's copy in one step
    else:
        target.write_bytes((await client.get(worker['url'] + state['url'])).content)
    timing = {k: state[k] for k in ('inference_seconds', 'encoding_seconds', 'decoding_seconds') if k in state}
    job.update(status='complete', message='Done', url=f"/outputs/{job['id']}.{suffix}", finished_at=time.time(),
               **timing)
    job.pop('_worker_job', None)
    worker['done'] += 1
    worker['seconds'] += job['finished_at'] - job['started_at']
    save(job)


# ------------------------------------------------------------------ retention
async def cleanup_loop():
    while True:
        now = time.time()
        for job_id, job in list(jobs.items()):
            if job['status'] in ('complete', 'error') and now - job.get('finished_at', now) > RETENTION:
                shutil.rmtree(folder(job_id), ignore_errors=True)
                jobs.pop(job_id, None)
        # strays in the workers' own output folder (e.g. images made in a worker's UI)
        for outputs in set(WORKER_OUTPUTS):
            for path in outputs.glob('*'):
                if path.is_file() and now - path.stat().st_mtime > RETENTION:
                    path.unlink(missing_ok=True)
        await asyncio.sleep(max(10, min(600, RETENTION / 4)))


@app.on_event('startup')
async def startup():
    restore()
    for worker in workers:
        asyncio.create_task(worker_loop(worker))
    asyncio.create_task(cleanup_loop())
    asyncio.create_task(load())          # a restart reloads both workers by itself
    if queue:
        wake.set()


# ------------------------------------------------------------------ API (Qwen Image Studio compatible)
@app.post('/api/generate', status_code=202)
async def generate(
    prompt: Annotated[str, Form(min_length=1, max_length=4000)],
    width: Annotated[int, Form(ge=512, le=2816)] = 1024,
    height: Annotated[int, Form(ge=512, le=2816)] = 1024,
    steps: Annotated[int, Form(ge=1, le=80)] = 30,
    guidance: Annotated[float, Form(ge=0, le=20)] = 0,
    seed: Annotated[int, Form(ge=-1, le=2147483647)] = -1,
    output_format: Annotated[str, Form(pattern='^(png|jpg|webp)$')] = 'png',
    references: Annotated[list[UploadFile] | None, File()] = None,
    model: Annotated[str, Form(max_length=40)] = '',
):
    if width % 16 or height % 16:
        raise HTTPException(422, 'Width and height must be divisible by 16')
    if len(references or []) > 10:
        raise HTTPException(422, 'At most 10 reference images are supported')
    if len(queue) >= INPUT_QUEUE:
        busy = sum(1 for w in workers if w['job'])
        wait = int(average_seconds() * max(1, len(queue)) / max(1, len(workers))) or 30
        return JSONResponse({'detail': f'Input queue is full ({INPUT_QUEUE} waiting, {busy} generating); retry later',
                             'retry_after_seconds': wait}, status_code=429, headers={'Retry-After': str(wait)})
    job_id = uuid.uuid4().hex
    folder(job_id).mkdir(parents=True)
    for i, upload in enumerate(references or []):
        data = await upload.read()
        if len(data) > 25 * 1024 * 1024:
            shutil.rmtree(folder(job_id), ignore_errors=True)
            raise HTTPException(413, f'{upload.filename} exceeds 25 MB')
        (folder(job_id) / f'ref-{i:02d}-{Path(upload.filename or "image").name}').write_bytes(data)
    if seed < 0:
        seed = int.from_bytes(os.urandom(4), 'big') % 2147483648
    params = dict(prompt=prompt.strip(), width=width, height=height, steps=steps, guidance=guidance, seed=seed,
                  output_format=output_format)
    (folder(job_id) / 'request.json').write_text(json.dumps(params))
    kind = model.strip() or DEFAULT_KIND
    job = {'id': job_id, 'status': 'queued', 'message': 'Queued', 'seed': seed, 'created_at': time.time(),
           'expires_after_hours': RETENTION / 3600, 'model': kind if kind in KINDS else DEFAULT_KIND}
    if kind not in KINDS:
        job['model_fallback'] = kind
    jobs[job_id] = job
    queue.append(job_id)
    save(job)
    wake.set()
    return public(job)


@app.get('/api/jobs/{job_id}')
async def job_status(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, 'Job not found (unknown, or older than the retention window)')
    return public(jobs[job_id])


@app.get('/outputs/{name}')
async def output(name: str):
    job_id, _, suffix = name.partition('.')
    path = folder(job_id) / f'image.{suffix}'
    if job_id not in jobs or not path.exists():
        raise HTTPException(404, 'Image not found (unknown, or older than the retention window)')
    return FileResponse(path)


async def worker_status(worker):
    try:
        return (await client.get(worker['url'] + '/api/status', timeout=5)).json()
    except Exception as exc:
        return {'state': 'down', 'detail': str(exc)[:200]}


@app.get('/api/status')
async def status():
    states = await asyncio.gather(*(worker_status(w) for w in workers))
    names = [s.get('state') for s in states]
    overall = ('loaded' if all(n == 'loaded' for n in names) else 'loading' if 'loading' in names
               else 'unloaded' if all(n in ('unloaded', 'down') for n in names) else 'partial')
    first = next((s for s in states if s.get('state') == 'loaded'), states[0])
    return {**{k: v for k, v in first.items() if k not in ('state', 'detail', 'busy', 'gpu')},
            'state': overall, 'detail': f"{sum(n == 'loaded' for n in names)}/{len(workers)} GPU workers loaded",
            'busy': any(w['job'] for w in workers),
            'factory': {'input_queue': {'limit': INPUT_QUEUE, 'waiting': len(queue)},
                        'dispatch': [{'gpu': w['gpu'], 'model': w['kind'], 'state': s.get('state'), 'job': w['job'],
                                      'images_done': w['done'], 'gpu_memory': s.get('gpu')} for w, s in zip(workers, states)],
                        'models': sorted(set(KINDS)),
                        'retention_hours': RETENTION / 3600, 'jobs_on_disk': len(jobs)}}


@app.post('/api/load', status_code=202)
async def load():
    async def staggered():                 # one worker at a time: loading peaks need ~20 GB of RAM each
        for worker in workers:
            try:
                for _ in range(60):          # a worker started with the factory needs a few seconds to listen
                    if (await worker_status(worker)).get('state') != 'down':
                        break
                    await asyncio.sleep(2)
                await client.post(worker['url'] + '/api/load')
                for _ in range(180):
                    if (await worker_status(worker)).get('state') in ('loaded', 'error'):
                        break
                    await asyncio.sleep(5)
            except Exception:
                pass
        wake.set()
    asyncio.create_task(staggered())
    return {'state': 'loading', 'detail': 'Loading the GPU workers one after the other'}


@app.post('/api/unload')
async def unload():
    if any(w['job'] for w in workers) or queue:
        raise HTTPException(409, 'Cannot unload while images are queued or being created')
    for worker in workers:
        try:
            await client.post(worker['url'] + '/api/unload', timeout=120)
        except Exception:
            pass
    return {'state': 'unloaded', 'detail': 'GPU workers unloaded'}


@app.get('/', response_class=HTMLResponse)
async def index():
    return ('<h1>Image factory</h1><p>Qwen Image 2.1 on both GPUs. See <a href="/docs">/docs</a> and '
            '<a href="/api/status">/api/status</a>.</p>')
