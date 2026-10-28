"""Shared test configuration.

Puts ``tests/support`` on ``sys.path`` so both ``tests/core`` and ``tests/backend`` can
import the same test doubles (notably ``fake_adapter.FakeAdapter``) regardless of
collection order.
"""

import sys
from pathlib import Path

_SUPPORT_DIR = Path(__file__).resolve().parent / "support"
if str(_SUPPORT_DIR) not in sys.path:
    sys.path.insert(0, str(_SUPPORT_DIR))
