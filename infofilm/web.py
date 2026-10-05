from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import time
import threading
import wave
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import httpx
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, field_validator, model_validator

from .auth import Auth
from .core import ROOT, configuration, digest, read, write
from .queue import Queue
from .creative import MOODS, INTENSITIES, BALANCES, MUSIC
from .narrators import narrator_profiles

PUBLIC_ORIGIN = "https://remotion.photovault.live"
COOKIE = "remotion_session"
SESSION_SECONDS = 12 * 3600


class Login(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=1024)


class MusicRevision(BaseModel):
    music_engine: Literal["none", "acestep"] = "acestep"


class ResumeRequest(BaseModel):
    image_review: bool | None = Field(default=None, strict=True)


class FilmRequest(BaseModel):
    subject: str = Field(min_length=3, max_length=300)
    durations: list[int] = Field(default_factory=lambda: [60], min_length=1, max_length=3)
    generation_mode: Literal["short", "long"] = "short"
    production_target: Literal['video', 'audio'] = 'video'
    subject_graph: bool = Field(default=False, strict=True)
    orientation: Literal["landscape", "portrait", "both"] = "landscape"
    review_mode: Literal["off", "experimental"] = "experimental"
    image_review: bool = Field(default=True, strict=True)
    voice_id: str = "default"
    tts_engine: Literal["vibevoice", "omnivoice"] = "vibevoice"
    omni_speed: float = Field(default=.8, ge=.5, le=2)
    mood: Literal["auto", "professional", "warm", "funny", "curious", "serious", "reflective", "inspiring"] = "auto"
    mood_intensity: Literal["restrained", "standard", "strong"] = "restrained"
    visual_balance: Literal["auto", "image-led", "balanced", "explanation-led"] = "auto"
    music_engine: Literal["none", "acestep"] = "none"
    reference_transcript: str | None = Field(default=None, max_length=5000)
    submission_id: str = Field(min_length=8, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")

    @field_validator("subject")
    @classmethod
    def clean_subject(cls, value):
        value = " ".join(value.split())
        if len(value) < 3 or any(ord(c) < 32 for c in value):
            raise ValueError("Enter a subject with at least three characters")
        return value

    @field_validator("durations")
    @classmethod
    def advisory_durations(cls, value):
        return list(dict.fromkeys(value))

    @model_validator(mode="after")
    def generation_limits(self):
        if self.subject_graph and self.generation_mode != "long":
            raise ValueError("Subject graph planning requires long generation")
        if self.generation_mode == "long":
            if len(self.durations) != 1 or not 300 <= self.durations[0] <= 1800:
                raise ValueError("Long generation needs one advisory length between 5 and 30 minutes")
        elif any(not 30 <= d <= 180 for d in self.durations):
            raise ValueError("Short generation uses 30–180-second advisory lengths")
        return self


def readiness(config):
    headers = {"Authorization": "Bearer " + os.environ["VLLM_API_KEY"]} if os.environ.get("VLLM_API_KEY") else {}
    image_endpoint = config["image_url"] + "/api/status"
    registry = config.get("image_provider_registry")
    if registry:
        policy = registry["policies"][config.get("image_provider_policy", registry["default_policy"])]
        provider = registry["providers"][policy["tiers"][0]["provider"]]
        image_endpoint = provider["url"].rstrip("/") + ("/api/models" if provider["adapter"] == "imagestudio_sdapi" else "/api/status")
    endpoints = [("planner", "Planner LLM", config["planner_url"] + "/models"),
                 ("reviewer", "Reviewer LLM", config["reviewer_url"] + "/models"),
                 ("audio", "Narration", config["tts_url"] + "/health"),
                 ("images", "Images", image_endpoint)]
    def check(entry):
        key, label, url = entry
        try:
            response = httpx.get(url, headers=headers if key in {"planner", "reviewer"} else None, timeout=5)
            response.raise_for_status()
            data = response.json()
            busy = bool(data.get("busy") or data.get("queued_or_running_jobs") or data.get("active_job"))
            return {"id": key, "label": label, "ready": True, "busy": busy,
                    "detail": "Busy" if busy else "Available"}
        except Exception as exc:
            return {"id": key, "label": label, "ready": False, "busy": False, "detail": str(exc)}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        services = list(pool.map(check, endpoints))
    dependencies = [
        ("Research environment", Path(config.get("research_python", str(Path(config["kbagent_root"]) / "env/bin/python")))),
        ("MFA", Path(config["story_backend"]) / "mfa_env/bin/mfa"),
        ("Remotion", ROOT / "renderer/node_modules/.bin/remotion"),
        ("Browser", Path(config["browser"])),
        ("Narrator reference", Path(config["reference_audio"])),
    ]
    for label, path in dependencies:
        services.append({"id": label, "label": label, "ready": path.exists(), "busy": False,
                         "detail": "Available" if path.exists() else f"Missing: {path}"})
    omni_installed = (Path(config.get("omni_python", "")).is_file()
                      and (Path(config.get("omni_model", "")) / "config.json").is_file())
    services.append({"id": "omnivoice", "label": "OmniVoice · on demand", "ready": omni_installed,
                     "busy": False, "detail": "Installed" if omni_installed else "Runtime or model missing"})
    music_installed = (Path(config.get("acestep_python", "")).is_file() and
        (Path(config.get("acestep_root", "")) / "checkpoints" / config.get("acestep_model", "acestep-v15-turbo") / "config.json").is_file())
    services.append({"id": "acestep", "label": "ACE-Step · optional score", "ready": music_installed,
        "optional": True, "busy": False, "detail": "Installed · on demand" if music_installed else "Runtime or model missing"})
    for binary in ["ffmpeg", "ffprobe", "node"]:
        available = shutil.which(binary) is not None
        services.append({"id": binary, "label": binary, "ready": available, "busy": False,
                         "detail": "Available" if available else "Not on PATH"})
    services.append({"id": "research_key", "label": "Research credentials", "ready": bool(os.environ.get("TAVILY_API_KEY")),
        "busy": False, "detail": "Available" if os.environ.get("TAVILY_API_KEY") else "TAVILY_API_KEY is missing from the service environment"})
    return {"checked_at": time.time(), "ready": all(s["ready"] for s in services if not s.get("optional")), "services": services}


def create_app(config_path=ROOT / "config.json", directory=ROOT / "artifacts/interface", *, start_worker=True):
    queue = Queue(directory, config_path)
    auth = Auth(directory)
    health_cache = {"value": None, "time": 0}
    failures = {}
    failure_lock = threading.Lock()

    @asynccontextmanager
    async def lifespan(app):
        try:
            auth.load()
        except FileNotFoundError:
            raise RuntimeError("Login is not configured. Run scripts/change-password.sh first.")
        if start_worker:
            queue.start()
        try:
            yield
        finally:
            if start_worker:
                queue.close()

    app = FastAPI(title="Remotion Video", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.queue = queue
    app.state.auth = auth

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        if request.url.path == "/api/login":
            return JSONResponse({"detail": "Enter a valid username and password"}, status_code=422)
        return JSONResponse({"detail": "; ".join(e["msg"] for e in exc.errors())}, status_code=422)

    @app.middleware("http")
    async def access_control(request, call_next):
        path = request.url.path
        origin = request.headers.get("origin")
        local_origin = str(request.base_url).rstrip("/")
        if origin and origin not in {PUBLIC_ORIGIN, local_origin}:
            return JSONResponse({"detail": "This origin is not allowed"}, status_code=403)
        # CORS preflights do not carry login cookies.
        if request.method == "OPTIONS":
            response = await call_next(request)
            return response
        if request.method == "POST" and path != "/api/voices" and not request.headers.get("content-type", "").startswith("application/json"):
            return JSONResponse({"detail": "Use application/json"}, status_code=415)
        protected = path.startswith(("/api/", "/media/", "/experiments/"))
        public = path in {"/api/login", "/api/session"}
        if protected and not public:
            token = request.cookies.get(COOKIE, "")
            with queue.connection() as db:
                row = db.execute("SELECT * FROM sessions WHERE token_hash=? AND expires>?",
                    (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
            if not row or row["revision"] != auth.load()["revision"]:
                return JSONResponse({"detail": "Please sign in"}, status_code=401)
            request.state.username = row["username"]
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        if path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        if path.startswith(("/media/", "/experiments/")):
            response.headers["Cache-Control"] = "private, no-store"
        return response

    @app.get("/")
    def index():
        return FileResponse(ROOT / "infofilm/static/index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/static/{name}")
    def static(name: str):
        if name not in {"app.js", "style.css"}:
            raise HTTPException(404)
        return FileResponse(ROOT / "infofilm/static" / name, headers={"Cache-Control": "no-cache"})

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.api_route("/experiments/{experiment_id}/{name:path}", methods=["GET", "HEAD"])
    def experiment_report(experiment_id: str, name: str):
        # Reports opt in to publication with an explicit file allowlist. Provider
        # requests, logs, local paths and source archives remain private.
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}", experiment_id):
            raise HTTPException(404)
        catalog = (ROOT / "artifacts/experiments").resolve()
        base = (catalog / experiment_id).resolve()
        if not base.is_relative_to(catalog):
            raise HTTPException(404)
        marker = base / "public-report.json"
        if not marker.is_file() or not marker.resolve().is_relative_to(base):
            raise HTTPException(404)
        try:
            publication = read(marker)
        except (ValueError, OSError):
            raise HTTPException(404)
        if publication.get("schema_version") != "infofilm.public-experiment-report.v1" or name not in publication.get("files", []):
            raise HTTPException(404)
        target = (base / name).resolve()
        if not target.is_relative_to(base) or target.suffix.lower() not in {".html", ".json", ".png", ".jpg"} or not target.is_file():
            raise HTTPException(404)
        return FileResponse(target)

    @app.get("/api/session")
    def session(request: Request):
        token = request.cookies.get(COOKIE, "")
        with queue.connection() as db:
            row = db.execute("SELECT username,revision FROM sessions WHERE token_hash=? AND expires>?",
                (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
        valid = bool(row and row["revision"] == auth.load()["revision"])
        return {"authenticated": valid, "username": row["username"] if valid else None}

    @app.post("/api/login")
    def login(body: Login, request: Request):
        peer = request.client.host if request.client else "unknown"
        now = time.time()
        with failure_lock:
            failures[peer] = [t for t in failures.get(peer, []) if t > now - 900]
            if len(failures[peer]) >= 5:
                raise HTTPException(429, "Too many attempts. Try again in 15 minutes.")
            failures[peer].append(now)
        revision = auth.verify(body.username, body.password)
        if not revision:
            raise HTTPException(401, "Invalid username or password")
        with failure_lock:
            failures.pop(peer, None)
        token = secrets.token_urlsafe(32)
        with queue.connection() as db:
            db.execute("DELETE FROM sessions WHERE expires<? OR revision!=?", (now, revision))
            db.execute("INSERT INTO sessions VALUES(?,?,?,?)",
                (hashlib.sha256(token.encode()).hexdigest(), body.username, revision, now + SESSION_SECONDS))
        response = JSONResponse({"authenticated": True, "username": body.username})
        response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, httponly=True,
            secure=request.url.scheme == "https" or request.headers.get("origin") == PUBLIC_ORIGIN,
            samesite="lax")
        return response

    @app.post("/api/logout")
    def logout(request: Request):
        token = request.cookies.get(COOKIE, "")
        with queue.connection() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE)
        return response

    def voices():
        config = configuration(config_path)
        values = [{"id": name, "label": profile["label"],
                   "has_transcript": bool(profile.get("reference_transcript"))}
                  for name, profile in narrator_profiles(config).items()]
        for path in sorted((queue.root / "voices").glob("*.json")):
            row = read(path)
            values.append({"id": path.stem, "label": row["name"], "has_transcript": False})
        return values

    @app.get("/api/settings")
    def settings():
        return {"voices": voices(), "default_duration": 60, "default_subject_graph": False, "language": "Indian English",
                "generation_modes": [{"id": "short", "label": "Short film · 30–180 seconds"}, {"id": "long", "label": "Long film · chapters · 5–30 minutes"}],
                "production_targets": [{'id': 'video', 'label': 'Complete video'}, {'id': 'audio', 'label': 'Audio first'}],
                "long_duration_minutes": {"min": 5, "max": 30, "default": 15},
                "allowed_origin": PUBLIC_ORIGIN, "duration_policy": "advisory",
                "default_mood": "auto", "default_omni_speed": .8, "default_image_review": True,
                **{key: [{"id": k, "label": v} for k, v in values.items()] for key, values in
                   [("moods", MOODS), ("mood_intensities", INTENSITIES), ("visual_balances", BALANCES), ("music_engines", MUSIC)]},
                "tts_engines": [{"id": "vibevoice", "label": "VibeVoice"}, {"id": "omnivoice", "label": "OmniVoice"}]}

    @app.get("/api/readiness")
    def check_readiness():
        if time.time() - health_cache["time"] > 20:
            health_cache["value"] = readiness(configuration(config_path))
            health_cache["time"] = time.time()
        return health_cache["value"]

    @app.post("/api/voices")
    async def upload_voice(file: UploadFile = File(...)):
        content = await file.read(12 * 1024 * 1024 + 1)
        await file.close()
        if len(content) > 12 * 1024 * 1024:
            raise HTTPException(413, "Reference WAV must be smaller than 12 MB")
        try:
            with wave.open(io.BytesIO(content)) as audio:
                seconds = audio.getnframes() / audio.getframerate()
                if not 1 <= seconds <= 120 or audio.getnchannels() not in {1, 2}:
                    raise ValueError("Use a mono or stereo WAV, between 1 and 120 seconds")
                expected = audio.getnframes() * audio.getnchannels() * audio.getsampwidth()
                if len(audio.readframes(audio.getnframes())) != expected:
                    raise ValueError("The WAV is incomplete")
        except (wave.Error, EOFError, ValueError, ZeroDivisionError) as exc:
            raise HTTPException(422, f"Invalid reference audio: {exc}")
        identity = digest(content)
        root = queue.root / "voices"
        root.mkdir(parents=True, exist_ok=True)
        target = root / (identity + ".wav")
        if not target.exists():
            target.write_bytes(content)
        name = Path(file.filename or "Narrator reference.wav").name[:100]
        write(root / (identity + ".json"), {"name": name, "file": str(target), "duration_seconds": seconds})
        return {"id": identity, "label": name}

    @app.get("/api/jobs")
    def jobs():
        queue.import_completed()
        return {"jobs": [queue.public(j["id"]) for j in queue.list()]}

    @app.post("/api/jobs", status_code=202)
    def submit(body: FilmRequest):
        config = configuration(config_path)
        profiles = narrator_profiles(config)
        if body.voice_id in profiles:
            profile = profiles[body.voice_id]
            reference = Path(profile["reference_audio"]).expanduser()
            transcript = profile.get("reference_transcript")
            label = profile["label"]
        elif re.fullmatch(r"[0-9a-f]{64}", body.voice_id):
            reference = queue.root / "voices" / (body.voice_id + ".wav")
            transcript = body.reference_transcript
            label = read(reference.with_suffix(".json"))["name"] if reference.with_suffix(".json").is_file() else "Uploaded reference"
        else:
            raise HTTPException(422, "Choose an available narrator reference")
        if not reference.is_file():
            raise HTTPException(422, "The narrator reference is missing")
        params = body.model_dump()
        params["reference_audio"] = str(reference.resolve())
        params["reference_transcript"] = " ".join((transcript or "").split()) or None
        params["narrator_label"] = label
        if body.music_engine == "acestep":
            if not Path(config.get("acestep_python", "")).is_file() or not (
                Path(config.get("acestep_root", "")) / "checkpoints" / config.get("acestep_model", "acestep-v15-turbo") / "config.json").is_file():
                raise HTTPException(422, "ACE-Step runtime or model is not installed")
        if body.tts_engine == "omnivoice":
            if not Path(config.get("omni_python", "")).is_file() or not (Path(config.get("omni_model", "")) / "config.json").is_file():
                raise HTTPException(422, "OmniVoice runtime or model is not installed")
            if not params["reference_transcript"]:
                raise HTTPException(422, "OmniVoice needs the exact transcript of the narrator reference")
        try:
            job = queue.submit(params, body.submission_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        return queue.public(job["id"], details=True)

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str):
        try:
            return queue.public(job_id, details=True)
        except KeyError:
            raise HTTPException(404, "Run not found")

    @app.post("/api/jobs/{job_id}/resume")
    def resume(job_id: str, body: ResumeRequest | None = None):
        try:
            queue.get(job_id)
            queue.resume(job_id, image_review=body.image_review if body else None)
            return queue.public(job_id, details=True)
        except KeyError:
            raise HTTPException(404, "Run not found")
        except ValueError as exc:
            raise HTTPException(409, str(exc))

    @app.post("/api/jobs/{job_id}/music", status_code=202)
    def revise_music(job_id: str, body: MusicRevision):
        try:
            queue.revise_music(job_id, body.music_engine)
            return queue.public(job_id, details=True)
        except KeyError:
            raise HTTPException(404, "Run not found")
        except ValueError as exc:
            raise HTTPException(409, str(exc))

    @app.post("/api/jobs/{job_id}/narration", status_code=202)
    def revise_narration(job_id: str):
        try:
            queue.revise_narration(job_id)
            return queue.public(job_id, details=True)
        except KeyError:
            raise HTTPException(404, "Run not found")
        except ValueError as exc:
            raise HTTPException(409, str(exc))

    @app.post('/api/jobs/{job_id}/continue-video', status_code=202)
    def continue_video(job_id: str):
        try:
            queue.continue_video(job_id)
            return queue.public(job_id, details=True)
        except KeyError:
            raise HTTPException(404, 'Run not found')
        except ValueError as exc:
            raise HTTPException(409, str(exc))

    @app.api_route("/media/{job_id}/{kind}/{name:path}", methods=["GET", "HEAD"])
    def media(job_id: str, kind: str, name: str):
        try:
            job = queue.get(job_id)
            project = Path(job["project"])
            if kind == "plan" and name == "chapter-plan.json":
                base = project.resolve()
                target = (project / "chapter-plan.json").resolve()
            elif kind == "chapter":
                match = re.fullmatch(r"(C\d{2})/(review/.+|audio\.wav|script\.txt)", name)
                if not match:
                    raise HTTPException(404)
                value = read(project / "longform-status.json")
                row = next((r for r in value["chapters"] if r["chapter"]["id"] == match[1]), None)
                if row is None:
                    raise HTTPException(404)
                chapter_project = Path(row["project"]).resolve()
                if not chapter_project.is_relative_to((project / "chapters").resolve()):
                    raise HTTPException(404)
                part = match[2]
                if part.startswith("review/"):
                    if row.get("status") != "succeeded":
                        raise HTTPException(404)
                    base = (ROOT / "output" / chapter_project.name).resolve()
                    target = (base / part.removeprefix("review/")).resolve()
                    if not target.is_relative_to(base) or target.suffix.lower() not in {".html", ".json", ".txt", ".wav", ".mp4", ".png", ".jpg"}:
                        raise HTTPException(404)
                else:
                    base = chapter_project
                    folder = chapter_project / f"film-{row['duration']}"
                    target = (Path(read(folder / "audio-selection.json")["audio"]) if part == "audio.wav" else folder / "script.txt").resolve()
                    if not target.is_relative_to(base):
                        raise HTTPException(404)
            elif kind == 'listen':
                if not (project / 'audio-delivery.json').is_file():
                    raise HTTPException(404, 'Audio delivery is not ready')
                base = (ROOT / 'output' / project.name / 'audio-review').resolve()
                target = (base / name).resolve()
                if not target.is_relative_to(base) or target.suffix.lower() not in {'.html', '.json', '.txt', '.wav', '.mp3'}:
                    raise HTTPException(404)
            elif kind == "review":
                if job["status"] != "succeeded":
                    raise HTTPException(404, "Delivery is not ready")
                base = (ROOT / "output" / project.name).resolve()
                target = (base / name).resolve()
                if not target.is_relative_to(base) or target.suffix.lower() not in {".html", ".json", ".txt", ".wav", ".mp4", ".png", ".jpg"}:
                    raise HTTPException(404)
            elif kind in {"audio", "script", "score", "master"} and re.fullmatch(r"\d+\.(wav|txt)", name):
                duration = int(name.split(".")[0])
                if duration not in job["request"].get("durations", []):
                    raise HTTPException(404)
                if kind in {"score", "master"}:
                    value = read(project / f"film-{duration}/music-selection.json")
                    if value.get("engine") != "acestep":
                        raise HTTPException(404)
                    target = Path(value["score"]["audio"] if kind == "score" else value["master_audio"]).resolve()
                    if not target.is_relative_to(project.resolve()):
                        raise HTTPException(404)
                elif kind == "audio":
                    value = read(project / f"film-{duration}/audio-selection.json")
                    target = Path(value["audio"]).resolve()
                    if not target.is_relative_to(project.resolve()):
                        raise HTTPException(404)
                else:
                    target = project / f"film-{duration}/script.txt"
            else:
                raise HTTPException(404)
            if not target.is_file():
                raise HTTPException(404)
            return FileResponse(target)
        except (KeyError, FileNotFoundError):
            raise HTTPException(404, "File not found")

    app.add_middleware(CORSMiddleware, allow_origins=[PUBLIC_ORIGIN], allow_credentials=True,
                       allow_methods=["GET", "POST", "HEAD"], allow_headers=["Content-Type"])
    return app


def serve(config_path, directory, host="0.0.0.0", port=8051):
    import uvicorn
    uvicorn.run(create_app(config_path, directory), host=host, port=port,
                proxy_headers=True, forwarded_allow_ips="127.0.0.1,::1")
