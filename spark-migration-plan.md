# Move infofilm and Lesson Studio to the Spark (plan, 2026-10-12)

Why: the lab power failure; this host needs an overhaul. Target: the Spark (10.8.0.4 / 192.168.0.102) runs both web
apps and their pipelines; LLMs are cloud (Haiku planner, Luna reviewer); the image factory stays on amdserver; music
moves to the Spark; the premium image tier becomes a cloud provider.

## What the Spark has (surveyed 2026-10-12)

- **aarch64 (ARM)**, NVIDIA GB10, 121 GB unified memory (~50 GB in use: arbiter llama-server 41 GB, OmniVoice 2 GB),
  **20 cores** (this host: 64), 216 GB free disk.
- Already running: VibeVoice (`vibevoice-hindi-experiment`), arbiter LLM (`llm-qwen27b`), OmniVoice, ImageStudio web,
  quiz app + queue, `vllm-chat`.
- Remotion works: `~/quiz_generator/quiz/render` has Remotion 4.0.513 (same version as infofilm's renderer) with the
  `compositor-linux-arm64` package; Node 24 at `~/.local/node-v24.4.0`.
- Python 3.11.15 (pyenv) and 3.12 system; `~/quiz_generator` replica (has `amit.wav`, the default narrator reference).

## Blockers and replacements (ARM or this-host-only)

| Dependency (config key) | Today | On the Spark |
|---|---|---|
| Obscura page fetcher (`kbagent/obscura`) | x86-64 binary | ARM build if the project offers one; else replace `fetch` with httpx + readability (or Tavily extract) |
| MFA (`story_backend/mfa_env`, 5.5 GB conda) | x86 conda env | new conda/micromamba env from conda-forge (confirm aarch64 build of montreal-forced-aligner + its models) |
| Chromium for layout checks (`browser`) | Playwright x86 build | Playwright ARM chromium, or Remotion's own chrome-headless-shell |
| ACE-Step music (`acestep_*`), Whisper large-v3 QA (`music_stable_ts_*`, `music_vad_python`) | venvs on the 3090 | fresh ARM venvs (torch aarch64 + CUDA already works for VibeVoice); copy checkpoints |
| OmniVoice (`omni_*`) | local venv | Spark already runs `omnivoice.service`: point `omni_url` at it |
| Flux premium tier (`flux_dev_local`) | ImageStudio on the 3090 | **done:** `cloud_premium` (adapter `openrouter_image`, `infofilm/image_cloud.py`), model chosen under LLM settings → Premium images; now `google/gemini-3.1-flash-image` (~$0.07/image, 3 attempts max) |
| Manim env for Lesson Studio (`manim_python`) | `.venv-manim-pilot` | fresh ARM venv (manim, LaTeX, ffmpeg via apt) |
| `gpu_lock`, `shared_gpu_services` | 3090 lease (music, Flux) | Spark: music only (or none, with enough memory) |
| Python env for both apps | pyenv 3.11.9 | pyenv 3.11.15 venv per app |

## Configuration changes on the Spark

- `tts_url` → the local VibeVoice (directly, or keep the media adapter for the virtual unload/busy semantics);
  `image_url` / `qwen_local` → amdserver factory (10.8.0.8:8075); premium tier → cloud provider.
- LLM settings page: copy `artifacts/interface/llm-settings.json` + `llm-secrets.json` (mode 600) and the queue/auth
  databases; old runs stay here (archive), only `interface/` moves.
- CPU is a third of this host's: `mfa_slots` ≈ 3, `render_slots` 1–2. If renders become the bottleneck, the
  render-server idea on 10.8.0.5 (CPU-rich) still applies.
- Units: `remotion-video` (infofilm web :8051) and `lesson-studio` (:18610) as user units with linger;
  `infofilm-media-adapter` if kept.
- Public URL (`remotion.photovault.live`) and any proxy/frp entries must point at the Spark.

## Order

1. Commit/push both codebases: infofilm `github.com/0xbadfood/remotion` (`967de7b`); Lesson Studio
   `github.com/0xbadfood/lesson-studio` (initial import; runtime data in `scivid-data/` is copied separately).
2. Spark: clone, create venvs, install Node deps for the renderer, MFA env, research env (+ fetcher replacement).
3. Music: ACE-Step + Whisper QA on the Spark; drop the 3090 lease for music.
4. Premium images: done (cloud adapter; `flux_dev_local` kept in the registry but no policy uses it).
5. Copy `interface/` state; start units; run the test suite; one short film, then one long film.
6. Stop the services here. The public URL is already switched: `remotion.photovault.live` → 10.8.0.4:8051 on master's
   Caddy (2026-10-12); 13 sites that lived only in Caddy's admin-API config were persisted into `/etc/caddy/Caddyfile`
   (backup `Caddyfile.bak-20261012-150752-before-api-sites`). `manim.photovault.live` (Lesson Studio) still points
   here (10.8.0.2:18610) and moves at its cutover.

## Decisions

- Digest work (NCERT, kb_research) pauses until this host is back (user, 2026-10-12). Nothing in the move needs the
  Spark arbiter LLM; its 41 GB can be freed if music or other services need room.

