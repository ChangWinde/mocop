"""Test package bootstrap for the src/ layout.

Running ``python -m unittest discover -s tests`` from a source checkout must
keep working without an installation step. Importing this package puts the
``src`` directory first on ``sys.path`` so the tests exercise the checkout,
not an unrelated installed release.
"""

import sys
from pathlib import Path

_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
