#!/usr/bin/env python3
"""Render a validated image-animation bundle using CPU Manim/Cairo."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from infofilm.image_animation_renderer import main

if __name__ == "__main__":
    raise SystemExit(main())
