import json
import tempfile
import unittest
from pathlib import Path

import httpx

from infofilm.youtube import describe, upload


class YouTubeUploadTests(unittest.TestCase):
    def test_resumable_upload_sends_film_metadata_and_resumes_from_the_acknowledged_byte(self):
        seen = []

        def respond(request):
            seen.append(request)
            if request.url.host == "oauth2.googleapis.com":
                return httpx.Response(200, json={"access_token": "token"})
            if request.method == "POST":
                return httpx.Response(200, headers={"Location": "https://upload.example/session"})
            if request.headers["Content-Range"].startswith("bytes 0-"):
                return httpx.Response(308, headers={"Range": "bytes=0-3"})
            return httpx.Response(200, json={"id": "abc123"})

        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "film.mp4"
            video.write_bytes(b"0123456789")
            progress = []
            result = upload({"client_id": "id", "client_secret": "secret", "refresh_token": "refresh"}, video,
                            "Cuba <1962>", "Description", "unlisted", lambda m, p: progress.append(p), chunk_bytes=6,
                            transport=httpx.MockTransport(respond))
        self.assertEqual(result, {"youtube_video_id": "abc123", "youtube_url": "https://youtu.be/abc123", "privacy_status": "unlisted"})
        metadata = json.loads(seen[1].content)
        self.assertEqual(metadata["status"], {"privacyStatus": "unlisted", "selfDeclaredMadeForKids": False})
        self.assertEqual(metadata["snippet"]["categoryId"], "27")
        self.assertEqual(seen[3].headers["Content-Range"], "bytes 4-9/10")       # resumed after the acknowledged byte
        self.assertTrue(progress)

    def test_description_lists_chapters_and_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "chapter-plan.json").write_text(json.dumps({"film": {"title": "Thirteen Days"},
                "chapters": [{"id": "C01", "title": "Roots"}]}))
            (project / "sources.json").write_text(json.dumps({"results": [{"url": "https://history.state.gov/x"}]}))
            title, text = describe(project, "The Cuban Missile Crisis")
        self.assertEqual(title, "Thirteen Days")
        self.assertIn("C01 Roots", text)
        self.assertIn("https://history.state.gov/x", text)
