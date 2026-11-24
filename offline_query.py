#!/usr/bin/env python3
"""Named entry point for the offline mDNS inventory query."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if SRC.exists() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from zeroconf.inventory import main

if __name__ == "__main__":
    raise SystemExit(main())
