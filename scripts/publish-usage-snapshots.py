#!/usr/bin/env python3
"""Repository entrypoint; the container uses python -m app.business_usage."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps.gateway.app.business_usage import main

if __name__ == "__main__":
    raise SystemExit(main())
