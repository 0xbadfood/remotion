"""Publish the four imagegen canvas interpretations beside their saved Luna plans."""
from pathlib import Path
import html
import json


ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'artifacts/experiments/circular-motion-luna-canvas-previews'


def build():
    chapters = json.loads((OUT / 'source-plans.json').read_text())
    notes_path = OUT / 'inspection-notes.json'
    notes = json.loads(notes_path.read_text()) if notes_path.exists() else {}
    esc = html.escape
    cards = []
    for chapter in chapters:
        cid = chapter['id'].lower()
        canvas = chapter['canvas']
        image = cid + '-canvas.png'
        if not (OUT / image).is_file():
            raise FileNotFoundError(image)
        beats = ''.join('<li><strong>' + esc(beat['id'].replace('_', ' ').replace('-', ' ')) +
            '</strong><p>' + esc(beat['message']) + '</p><p class="muted">' +
            esc(beat['demonstration']) + '</p></li>' for beat in canvas['teaching_sequence'])
        zones = ''.join('<tr><td>' + esc(zone['id']) + '</td><td>' +
            ', '.join(f'{n * 100:g}%' for n in zone['box']) + '</td><td>' + esc(zone['role']) +
            '</td></tr>' for zone in canvas['zones'])
        review = ''.join('<li>' + esc(note) + '</li>' for note in notes.get(cid, []))
        cards.append(f'''<article id="{cid}">
<div class="chapter-top"><span class="eyebrow">{esc(chapter['id'])} · Luna canvas interpretation</span>
<a class="download" href="{image}" download>Download image ↗</a></div>
<h2>{esc(chapter['title'])}</h2><p class="objective">{esc(canvas['learning_objective'])}</p>
<button class="image-button" type="button" data-image="{image}" data-title="{esc(chapter['title'], quote=True)}" aria-label="Enlarge {esc(chapter['title'], quote=True)}">
<img src="{image}" alt="Image-generated illustration of Luna's representative canvas for {esc(chapter['title'], quote=True)}" loading="lazy"></button>
<div class="stats"><span>{len(canvas['teaching_sequence'])} teaching beats</span><span>{chapter['object_instances']} planned object instances across the chapter</span><span>1280 × 720 logical canvas</span></div>
<p class="muted">The stage is reused over time. These object counts include later diagrams, labels and alternate states; they are not all visible together.</p>
{('<details><summary>Visual inspection notes</summary><ul>' + review + '</ul></details>') if review else ''}
<details><summary>Luna's original layout strategy</summary><p>{esc(canvas['layout_strategy'])}</p>
<p><a href="{cid}-canvas.json">Download original canvas JSON</a></p>
<div class="table-scroll"><table><thead><tr><th>Zone</th><th>x, y, width, height</th><th>Role</th></tr></thead><tbody>{zones}</tbody></table></div></details>
<details><summary>Full teaching sequence · {len(canvas['teaching_sequence'])} beats</summary><ol>{beats}</ol></details>
</article>''')
    links = ''.join(f'<a href="#{ch["id"].lower()}">{esc(ch["id"])} · {esc(ch["title"])}</a>' for ch in chapters)
    document = '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Luna's four circular-motion canvases</title><style>
:root{color-scheme:dark;--bg:#080f1d;--card:#101b2e;--line:#26364f;--ink:#edf3fc;--muted:#b0bed3;--accent:#81d8ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.6 system-ui,sans-serif}main{max-width:1380px;margin:auto;padding:38px 24px 70px}h1{font-size:clamp(28px,4vw,46px);line-height:1.15;margin:10px 0 18px}h2{margin:10px 0;font-size:28px}a{color:var(--accent);text-underline-offset:4px}.eyebrow{text-transform:uppercase;letter-spacing:.1em;font-size:12px;font-weight:700;color:var(--accent)}.intro{max-width:940px;color:var(--muted)}nav{display:flex;flex-wrap:wrap;gap:10px;margin:26px 0}nav a{border:1px solid var(--line);border-radius:10px;padding:8px 13px;text-decoration:none}.notice{padding:16px 20px;background:#16223b;border-left:3px solid var(--accent);border-radius:5px;color:var(--muted)}article{scroll-margin-top:20px;background:var(--card);border:1px solid var(--line);border-radius:18px;padding:24px;margin:30px 0}.chapter-top{display:flex;justify-content:space-between;align-items:center;gap:12px}.download{font-size:14px}.objective{max-width:1040px;color:var(--muted)}.image-button{border:0;padding:0;background:transparent;width:100%;display:block;cursor:zoom-in;margin:24px 0 16px;border-radius:12px;overflow:hidden}.image-button img{width:100%;height:auto;display:block}.stats{display:flex;flex-wrap:wrap;gap:10px}.stats span{background:#1c2c43;border-radius:6px;padding:5px 10px;font-size:13px}.muted{color:var(--muted)}details{border-top:1px solid var(--line);margin-top:16px;padding-top:14px}summary{cursor:pointer;font-weight:600}li p{margin:6px 0 12px}.table-scroll{overflow:auto}table{border-collapse:collapse;font-size:14px;width:100%;margin:20px 0}th,td{padding:10px;text-align:left;border:1px solid var(--line);vertical-align:top}th{background:#19273d}dialog{max-width:96vw;width:1600px;padding:16px;background:#0b1424;color:var(--ink);border:1px solid var(--line);border-radius:12px}dialog::backdrop{background:#000c}dialog img{max-width:100%;max-height:80vh;display:block;margin:auto}dialog form{display:flex;justify-content:space-between;gap:20px;margin-bottom:12px}dialog button{background:#25364f;color:white;border:1px solid #476080;padding:7px 13px;border-radius:6px;cursor:pointer}footer{color:var(--muted);margin-top:28px}@media(max-width:600px){main{padding:24px 12px}article{padding:15px}.chapter-top{align-items:flex-start;flex-direction:column}}
</style></head><body><main><span class="eyebrow">Circular motion · planner intent study</span>
<h1>What Luna was trying to put on the canvas</h1>
<p class="intro">Four image-generated interpretations of the saved chapter canvas plans. Each illustration shows one representative teaching state, with the chapter's full sequence and original layout available underneath.</p>
<div class="notice">These are imagegen illustrations, not Remotion screenshots or proof that the animation executes correctly. Diagram positions and text are illustrative. Luna's JSON remains the source for exact canvas zones. Its bottom 20% is reserved for captions, and the same stage hosts different diagrams over time.</div>
<nav aria-label="Chapters">''' + links + '</nav>' + ''.join(cards) + '''
<footer>Generated with the built-in imagegen tool from the existing Luna plans. <a href="imagegen-prompts.json">Exact prompt set</a> · <a href="source-plans.json">Saved source plans and chapter counts</a>. Production plans, audio, timings and asset registrations were not changed.</footer>
</main><dialog id="viewer"><form method="dialog"><strong id="viewer-title"></strong><button>Close ✕</button></form><img id="viewer-image" alt="Enlarged chapter canvas illustration"></dialog>
<script>const viewer=document.getElementById('viewer');for(const button of document.querySelectorAll('[data-image]'))button.addEventListener('click',()=>{document.getElementById('viewer-title').textContent=button.dataset.title;document.getElementById('viewer-image').src=button.dataset.image;viewer.showModal();});viewer.addEventListener('click',event=>{if(event.target===viewer)viewer.close();});</script></body></html>'''
    (OUT / 'report.html').write_text(document)
    files = ['report.html', 'imagegen-prompts.json', 'source-plans.json']
    files += [ch['id'].lower() + suffix for ch in chapters for suffix in ('-canvas.png', '-canvas.json')]
    if notes_path.exists():
        files.append(notes_path.name)
    (OUT / 'public-report.json').write_text(json.dumps({
        'schema_version':'infofilm.public-experiment-report.v1', 'files':files}, indent=2))
    print(OUT / 'report.html')


if __name__ == '__main__':
    build()
