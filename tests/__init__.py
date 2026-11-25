"""Unit test suite for charon_runtime."""

from pathlib import Path
import sys

_REPO_ROOT = Path(__file__).resolve().parent.parent
if (_REPO_ROOT / "charon_runtime").is_dir() and str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
