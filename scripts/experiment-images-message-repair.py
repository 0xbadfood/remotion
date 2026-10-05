#!/usr/bin/env python3
"""Prepare or run the full-resolution, message-preserving repair experiment."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from infofilm.image_message_experiment import main

if __name__ == "__main__":
    main()
