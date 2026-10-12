#!/usr/bin/env python3
"""Generate Luna narration and optionally prepare VibeVoice/MFA in one command."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from infofilm.animation_narration import main
if __name__=='__main__':sys.exit(main())
