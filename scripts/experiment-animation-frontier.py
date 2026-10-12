#!/usr/bin/env python3
"""Run the resumable teaching, narration, speech/MFA and animation chapter pipeline."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from infofilm.animation_frontier import main
if __name__=='__main__':
    sys.exit(main())
