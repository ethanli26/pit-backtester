"""Pytest bootstrap: make the repo root importable so tests can `import config`,
`from factors.evaluate import ...`, etc., regardless of where pytest is invoked from.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
