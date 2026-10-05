#!/usr/bin/env python3
"""Matched ladder: low-res/low-steps, low-res/normal-steps, final render/review."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from infofilm.image_experiment import main
if __name__ == "__main__":
    main("ladder")
