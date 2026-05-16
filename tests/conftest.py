import sys
from pathlib import Path

# Make `model` importable when running pytest from any directory.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
