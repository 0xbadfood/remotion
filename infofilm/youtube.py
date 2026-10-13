"""Upload a finished film to the test YouTube channel (the Quiz Video Studio's connection, youtube.upload scope).

Credentials live in artifacts/interface/youtube-secrets.json (0600, imported once by
scripts/import_youtube_credentials.py); they are never returned by the API or logged. Each upload keeps its state next
to the portable video as <video>.youtube.json, which the portal reads for progress and the watch link.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import httpx

from .core import read, write

SECRETS_FILE = "youtube-secrets.json"
TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
PRIVACY = ("unlisted", "private")
_lock = threading.Lock()


class YouTubeError(RuntimeError):
    pass


def credentials(root):
    path = Path(root) / SECRETS_FILE
    if not path.is_file():
        return None
    value = read(path)
    return value if all(value.get(k) for k in ("client_id", "client_secret", "refresh_token")) else None


def state_path(video):
    return Path(str(video) + ".youtube.json")


def state(video):
    path = state_path(video)
    return read(path) if path.is_file() else None


def access_token(client, secret):
    response = client.post(TOKEN_URL, data={"client_id": secret["client_id"], "client_secret": secret["client_secret"],
                                            "refresh_token": secret["refresh_token"], "grant_type": "refresh_token"})
    if response.status_code != 200 or not response.json().get("access_token"):
        raise YouTubeError(f"could not refresh YouTube authorization (HTTP {response.status_code})")
    return response.json()["access_token"]


def upload(secret, video, title, description, privacy, report, *, chunk_bytes=8 * 1024 * 1024, retries=4, transport=None):
    """Resumable upload; returns {youtube_video_id, youtube_url, privacy_status}."""
    size = Path(video).stat().st_size
    if size <= 0:
        raise YouTubeError("video file is empty")
    metadata = {"snippet": {"title": title[:100], "description": description[:4900].replace("<", "(").replace(">", ")"),
                            "categoryId": "27", "tags": ["documentary", "history", "infofilm test"]},
                "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}}
    with httpx.Client(timeout=180, transport=transport) as client:
        report("Refreshing YouTube authorization", 0.02)
        token = access_token(client, secret)
        start = client.post(UPLOAD_URL, params={"uploadType": "resumable", "part": "snippet,status"}, json=metadata,
                            headers={"Authorization": f"Bearer {token}", "X-Upload-Content-Length": str(size),
                                     "X-Upload-Content-Type": "video/mp4"})
        if start.status_code != 200 or not start.headers.get("Location"):
            raise YouTubeError(f"could not start the upload (HTTP {start.status_code}): {start.text[:500]}")
        location, offset = start.headers["Location"], 0
        with open(video, "rb") as source:
            while offset < size:
                source.seek(offset)
                chunk = source.read(min(chunk_bytes, size - offset))
                last = offset + len(chunk) - 1
                for attempt in range(retries + 1):
                    try:
                        response = client.put(location, content=chunk, headers={
                            "Authorization": f"Bearer {token}", "Content-Type": "video/mp4",
                            "Content-Range": f"bytes {offset}-{last}/{size}"})
                    except httpx.HTTPError as exc:
                        if attempt >= retries:
                            raise YouTubeError(f"upload failed at byte {offset}: {exc}") from exc
                        time.sleep(min(2 ** attempt, 8))
                        continue
                    if response.status_code >= 500 and attempt < retries:
                        time.sleep(min(2 ** attempt, 8))
                        continue
                    break
                if response.status_code == 308:
                    acknowledged = response.headers.get("Range", "")
                    offset = int(acknowledged.rsplit("-", 1)[1]) + 1 if "-" in acknowledged else last + 1
                    report(f"Uploading {offset / 1e6:.0f} of {size / 1e6:.0f} MB", 0.05 + 0.9 * offset / size)
                    continue
                if response.status_code not in (200, 201) or not response.json().get("id"):
                    raise YouTubeError(f"upload failed (HTTP {response.status_code}): {response.text[:500]}")
                ident = response.json()["id"]
                return {"youtube_video_id": ident, "youtube_url": f"https://youtu.be/{ident}", "privacy_status": privacy}
    raise YouTubeError("upload did not complete")


def describe(project, subject):
    """Title and description from the film's plan and research sources."""
    project = Path(project)
    plan = read(project / "chapter-plan.json") if (project / "chapter-plan.json").is_file() else {}
    title = (plan.get("film") or {}).get("title") or subject
    lines = [f"Test render of an infofilm documentary: {subject}.", ""]
    chapters = plan.get("chapters") or []
    if chapters:
        lines += ["Chapters:"] + [f"{c.get('id')} {c.get('title')}" for c in chapters] + [""]
    sources = read(project / "sources.json").get("results", []) if (project / "sources.json").is_file() else []
    if sources:
        lines += ["Sources:"] + [s["url"] for s in sources[:12] if s.get("url")] + [""]
    lines.append("Illustrations are generated reconstructions, not archival footage.")
    return title, "\n".join(lines)


def start(root, project, subject, video, privacy="unlisted"):
    """Begin a background upload unless one is running or done. Returns the current state."""
    if privacy not in PRIVACY:
        raise ValueError("privacy must be unlisted or private")
    secret = credentials(root)
    if secret is None:
        raise ValueError("YouTube is not connected: run scripts/import_youtube_credentials.py")
    with _lock:
        current = state(video)
        if current and current.get("status") in {"uploading", "complete"}:
            return current
        write(state_path(video), {"status": "uploading", "progress": 0, "message": "Queued for upload",
                                  "privacy_status": privacy, "updated": time.time()})

    def report(message, progress):
        write(state_path(video), {"status": "uploading", "progress": round(progress, 3), "message": message,
                                  "privacy_status": privacy, "updated": time.time()})

    def run():
        try:
            title, description = describe(project, subject)
            result = upload(secret, video, title, description, privacy, report)
            write(state_path(video), {"status": "complete", "progress": 1, "message": "Uploaded", **result,
                                      "updated": time.time()})
        except Exception as exc:  # the portal shows the reason; the user can retry
            write(state_path(video), {"status": "failed", "progress": 1, "message": "Upload failed",
                                      "error": str(exc)[:600], "privacy_status": privacy, "updated": time.time()})

    threading.Thread(target=run, name="youtube-upload", daemon=True).start()
    return state(video)
