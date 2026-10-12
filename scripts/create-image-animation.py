#!/usr/bin/env python3
"""Isolated, resumable image-backed educational film CLI."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from infofilm.image_animation_workflow import main

if __name__ == '__main__':
    sys.exit(main())
