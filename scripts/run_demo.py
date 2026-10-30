"""Start the app the way the hosted demo runs it: each visitor gets a temporary copy of the sample documents.

    python scripts/run_demo.py --server.port 8601
    python scripts/run_demo.py --offline --server.port 8602     # as if the API keys were missing
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)                                   # so .streamlit/config.toml (the theme) is found
os.environ.setdefault("DULY_DEMO_MODE", "1")
args = sys.argv[1:]
if "--offline" in args:
    args.remove("--offline")
    os.environ["DULY_OFFLINE"] = "1"

from streamlit.web import cli  # noqa: E402

sys.argv = ["streamlit", "run", str(ROOT / "app.py"), "--server.headless", "true", *args]
sys.exit(cli.main())
