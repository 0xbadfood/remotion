# Colourful image → extracted layers → gravity motion

Report: `/experiments/gravity-image-mggen-manim/report.html` (normal webapp login).
Artifacts: `artifacts/experiments/gravity-image-mggen-manim/`.

The built-in imagegen source depicts a horizontal cannon on a raised stone pedestal and two contrasting balls at approximately equal height. The dropped ball sits left of the pedestal; the fired ball sits outside the right-facing muzzle. Separate lanes avoid ball/obstacle collisions. Both source prompts are retained in `image-generation.json`.

Actual MG-Gen YOLO/SAM2 extraction found the orange ball. An automatically colour-derived box supplied SAM2 with the missed blue ball; this scoped fallback is disclosed. Actual LaMa fills only the two ball regions, with local context refinement, while preserving all scenery outside those masks pixel for pixel. The previous circular pilot's checkout, CPU environment and model weights are reused.

Manim moves transparent sprites over the colourful static background. One shared linear flight clock drives both vertical positions quadratically (`y = y0 + fall_height * progress²`), while the fired ball's horizontal position grows linearly. Path arclength is not used as a physics clock. Tiny source height/size differences are normalized. Ground level and horizontal range are visually calibrated once from this image; no metre scale is asserted. No narration or general motion planner is included.

Actual result: eight seconds, 1280×720, 30 fps, 240 decoded frames. Both land at frame 180 (six seconds into the clip). CPU extraction/selective filling took about 29.3 seconds; Manim rendering took 9.2 seconds. These times exclude image generation, dependency setup and downloads. Computed transforms and independently decoded video properties pass all nine checks; decoded midpoint and landing frames were inspected.

## Repeat on this machine

Run from `/home/nitin/remotion`, reusing the saved source image and installed environments:

```bash
artifacts/experiments/circular-image-mggen-manim/.venv/bin/python \
  artifacts/experiments/gravity-image-mggen-manim/scripts/extract_mggen.py \
  --verified-text-free

python scripts/experiment-gravity-image-pilot.py geometry \
  --output artifacts/experiments/gravity-image-mggen-manim \
  --ground-y 760 --landing-x 1450

.venv-manim-pilot/bin/python scripts/experiment-gravity-image-manim.py \
  --scene artifacts/experiments/gravity-image-mggen-manim/scene.json \
  --background artifacts/experiments/gravity-image-mggen-manim/layers/background.png \
  --fired-ball artifacts/experiments/gravity-image-mggen-manim/layers/fired-ball.png \
  --dropped-ball artifacts/experiments/gravity-image-mggen-manim/layers/dropped-ball.png \
  --output-dir artifacts/experiments/gravity-image-mggen-manim/manim

python scripts/experiment-gravity-image-pilot.py report \
  --output artifacts/experiments/gravity-image-mggen-manim
```

The HTML embeds the actual MP4 and publishes only an explicit file allowlist through the existing experiment route. The production pipeline remains stopped and unchanged. Upstream MG-Gen is AGPL-3.0; this experiment is isolated.
