#!/usr/bin/env python3
"""Report failed assets without altering or restarting any generation."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from infofilm.failure_tracking import main
if __name__ == '__main__':
    main()
