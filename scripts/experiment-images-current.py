#!/usr/bin/env python3
"""Matched baseline: existing production image repair/provider loop."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from infofilm.image_experiment import main
if __name__ == "__main__":
    main("current")
