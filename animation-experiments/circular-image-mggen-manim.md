# Circular image → MG-Gen → Manim pilot

The isolated pilot lives in `artifacts/experiments/circular-image-mggen-manim`.
Its web report is `/experiments/circular-image-mggen-manim/report.html` and requires the normal webapp login. Production remains stopped.

The source diagram was generated with built-in imagegen. The unmodified official MG-Gen extractor uses YOLO, SAM2 and LaMa on CPU, with runtime adapters that omit OCR and text segmentation for this verified text-free input. Its original extracted layers and receipts are retained. The extracted ball is assigned by its red foreground pixels, a deliberate pilot rule. The visible blue circle is fitted from source pixels; its occluded arc is reconstructed. No model writes coordinates or motion keyframes.

Manim `MoveAlongPath` moves the extracted ball along the measured circle. A second clip adds a centre-to-ball string, inward-force arrow and tangent-velocity arrow using `always_redraw`, so these objects follow the same moving ball. Both clips are 12 seconds, 1280×720 at 30 fps, with two revolutions and no audio.

On this machine extraction took 15.3 seconds; rendering took 6.0 seconds for the ball and 8.9 seconds with the related objects. These times exclude source image generation, environment setup and model downloads. This validates one simple diagram, not general scientific image parsing or automated teaching/narration planning.

## Repeat using the installed pilot environments

Run from `/home/nitin/remotion`. Source image generation is a separate built-in imagegen step; the following reuses the saved source.

```bash
artifacts/experiments/circular-image-mggen-manim/.venv/bin/python \
  artifacts/experiments/circular-image-mggen-manim/scripts/extract_mggen.py \
  --verified-text-free

python scripts/experiment-image-motion-pilot.py geometry \
  --image artifacts/experiments/circular-image-mggen-manim/input.png \
  --ball artifacts/experiments/circular-image-mggen-manim/layers/ball.png \
  --output artifacts/experiments/circular-image-mggen-manim

.venv-manim-pilot/bin/python scripts/experiment-circular-image-manim.py \
  --scene artifacts/experiments/circular-image-mggen-manim/scene.json \
  --background artifacts/experiments/circular-image-mggen-manim/layers/background.png \
  --ball artifacts/experiments/circular-image-mggen-manim/layers/ball.png \
  --output-dir artifacts/experiments/circular-image-mggen-manim/manim \
  --relations

python scripts/experiment-image-motion-pilot.py report \
  --output artifacts/experiments/circular-image-mggen-manim
```

The report embeds both MP4s because the webapp's existing experiment route serves only approved HTML, JSON and image files. `public-report.json` explicitly lists published files. Dependencies, upstream commit and extraction provenance are recorded in the artifact receipts; the extraction adapter remains inside this experiment. MG-Gen's upstream license is AGPL-3.0.
