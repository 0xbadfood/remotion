from __future__ import annotations

import html
import re
import shutil
from pathlib import Path

from .core import ROOT, digest, read, write


def preview(store, destination=None):
    """Export a portable review folder after technical and layout checks pass."""
    from .evaluation import evaluate
    evaluate(store)
    delivery = read(store.root / "delivery.json")
    destination = Path(destination or ROOT / "output" / store.root.name).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    prefix = re.sub(r"[^a-z0-9]+", "-", store.root.name.lower()).strip("-") or "infofilm"
    cards, manifest = [], []
    for output in delivery["outputs"]:
        source = Path(output["video"])
        root = source.parent
        qa = read(root / "qa.json")
        layout = read(root / "layout/layout-results.json")
        if not qa["passed"] or not layout["passed"]:
            raise ValueError(f"Delivery checks have not passed: {source}")
        name = f"{prefix}-{output['requested_seconds']}s-{output['orientation']}"
        # A changed render gets a separate portable revision rather than silently
        # replacing an earlier review movie.
        target = destination / (name + ".mp4")
        if target.exists() and digest(target.read_bytes()) != qa["video_sha256"]:
            target = destination / (name + "-" + qa["video_sha256"][:8] + ".mp4")
        shutil.copyfile(source, target)
        assert digest(target.read_bytes()) == qa["video_sha256"]
        detail = destination / name
        detail.mkdir(exist_ok=True)
        for filename in ["qa.json", "compile-report.json", "timeline.json", "render-result.json"]:
            shutil.copyfile(root / filename, detail / filename)
        if (root / "master.wav").exists():
            shutil.copyfile(root / "master.wav", detail / "master.wav")
        if (root / "music.json").exists():
            shutil.copyfile(root / "music.json", detail / "music.json")
        shutil.copyfile(root / "layout/layout-results.json", detail / "layout-results.json")
        poster = root / "layout" / layout["samples"][0]["file"]
        shutil.copyfile(poster, detail / "poster.png")
        row = {**output, "portable_video": str(target), "sha256": qa["video_sha256"],
               "layout_samples": len(layout["samples"]), "layout_passed": True, "media_qa_passed": True}
        manifest.append(row)
        cards.append(f'<article><h2>{output["requested_seconds"]}-second brief · {html.escape(output["orientation"])}</h2>'
            f'<p>{output["actual_seconds"]:.1f} seconds · {output["point_count"]} points</p>'
            + (f'<p>AI generated, needs expert review: {html.escape(", ".join(output["needs_expert_review_assets"]))}</p>' if output.get('needs_expert_review_assets') else '')
            +
            f'<video controls preload="metadata" poster="{name}/poster.png" src="{target.name}"></video>'
            f'<p><a href="{target.name}">Open MP4</a> · <a href="{name}/timeline.json">Timing and scenes</a> · '
            f'<a href="{name}/qa.json">Media checks</a> · <a href="{name}/layout-results.json">Layout checks</a></p></article>')
    narration = []
    for duration in sorted({o["requested_seconds"] for o in delivery["outputs"]}):
        film = read(store.root / f"film-{duration}/audio-selection.json")
        shutil.copyfile(film["audio"], destination / f"narration-{duration}.wav")
        shutil.copyfile(store.root / f"film-{duration}/script.txt", destination / f"script-{duration}.txt")
        narration.append(f'<p>{duration}-second brief · <a href="script-{duration}.txt">Narration script</a></p>'
                         f'<audio controls src="narration-{duration}.wav"></audio>')
    sources = read(store.root / "sources.json")["results"]
    links = "".join(f'<li>{html.escape(s.get("id", f"S{i+1}"))}: <a href="{html.escape(s["url"],quote=True)}">{html.escape(s.get("title") or s["url"])}</a></li>' for i,s in enumerate(sources))
    for filename in ["evaluation.json","audio-user-review.json","scene-image-prompts.json","image_manifest.json", "creative-brief.json"]:
        if (store.root / filename).exists():
            shutil.copyfile(store.root / filename, destination / filename)
    references = store.root / 'person-references' / 'manifest.json'
    reference_link = ''
    if references.exists():
        shutil.copyfile(references, destination / 'person-reference-manifest.json')
        reference_link = '<a href="person-reference-manifest.json">Person reference sources and contact cards</a> · '
    def gap_names(value):
        if isinstance(value, list):
            return [item['person']['name'] for item in value if isinstance(item, dict) and 'person' in item]
        if isinstance(value, dict):
            return [name for item in value.values() for name in gap_names(item)]
        return []
    missing_names = sorted(set(gap_names(delivery.get('person_reference_gaps'))))
    reference_note = ('<p class="note">Appearance references unavailable: ' + html.escape(', '.join(missing_names)) +
                      '. Those depictions need appearance review.</p>') if missing_names else ''
    write(destination / "manifest.json", {**delivery, "outputs":manifest})
    report_link = '<a href="pilot-report.html">Pilot findings and reviewer-loop assessment</a> · ' if (destination / "pilot-report.html").exists() else ''
    document = '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Informational film review</title><style>
body{margin:0;background:#082d34;color:#f7f3e9;font:17px/1.5 system-ui,sans-serif}main{max-width:1320px;margin:auto;padding:36px 24px}h1{font-size:42px;line-height:1.1}h2{font-size:22px}a{color:#f3b85e}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(330px,1fr));gap:24px}article{padding:20px;background:#103e46;border-radius:16px}video{width:100%;max-height:660px;background:#031317}audio{width:min(600px,100%)}.note{max-width:920px;color:#c5dadd}details{margin:28px 0}pre{white-space:pre-wrap}
</style><main><p>INFOFILM PILOT</p><h1>''' + html.escape(delivery["subject"]) + '''</h1>
<p class="note">Duration guides scope; the explanation and natural delivery determine the finished length. Both aspect ratios preserve the reviewed narration. Banners identify relevant processes, places and seasons.</p>
''' + reference_note + '''<div class="grid">''' + "".join(cards) + '''</div>
<details><summary>Narration audio and scripts</summary>''' + "".join(narration) + '''</details>
<details><summary>Sources</summary><ul>''' + links + '''</ul></details>
<p class="note">Checks cover full media decoding, frames, dimensions and delivery-master waveform synchronization. Narration remains archived separately; scored films also audit speech in the mixed master. Browser measurements sample each scene at two motion positions. Editorial inspection is sampled; this is not a claim of full audiovisual playback review.</p>
<p>''' + report_link + reference_link + '''<a href="manifest.json">Delivery manifest</a> · <a href="evaluation.json">Measured model calls</a></p></main></html>'''
    (destination / "index.html").write_text(document)
    return destination
