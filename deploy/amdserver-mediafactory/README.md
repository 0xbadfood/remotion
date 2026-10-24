# amdserver media services (deployed copies)

Source of what runs on the amdserver (10.8.0.8) in image mode, kept here because the amdserver has no repository.

| File | On the amdserver | Port | GPU |
|---|---|---|---|
| `imagefactory-factory.py` | `~/imagefactory/factory.py` (routes jobs by `model`: klein, hidream) | 8075 | klein 0, HiDream 1 |
| `app.py` + infofilm's `mms_align.py` | `~/mediafactory/` (LuxTTS narration, MMS alignment) | 8078 | 1 |
| `mediafactory.service` | `~/.config/systemd/user/` (part of `imagefactory.service`, so it follows image mode) | | |

The media factory's environment is `~/mediafactory/.venv` (Python 3.11, torch 2.14.1+rocm7.2, LuxTTS requirements,
LuxTTS code in `~/mediafactory/LuxTTS`). infofilm calls it through `lux_url` and `aligner_url` in config.json and
falls back to local narration and alignment when it cannot be reached.
