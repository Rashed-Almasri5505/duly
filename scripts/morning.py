"""The always-on part: run the morning check on your own data and save the brief.

Schedule it once and it runs every morning by itself, for example on Windows:

    schtasks /Create /SC DAILY /ST 08:00 /TN "Duly morning check" ^
      /TR "\"C:\\path\\to\\duly\\.venv\\Scripts\\python.exe\" \"C:\\path\\to\\duly\\scripts\\morning.py\""

or with cron on Linux and macOS:

    0 8 * * *  /path/to/duly/.venv/bin/python /path/to/duly/scripts/morning.py

The brief is printed, added to the app's Deadlines tab, and saved as a Markdown file in the data folder.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from duly.brief import morning_check  # noqa: E402
from duly.config import default_data_dir, load_config  # noqa: E402
from duly.llm import SpendLedger, TokenFactory  # noqa: E402
from duly.store import Store  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", help="data folder (default: DULY_DATA_DIR or data/user)")
    ap.add_argument("--today", help="run as if today were this date (YYYY-MM-DD)")
    args = ap.parse_args()
    cfg = load_config()
    store = Store(Path(args.data) if args.data else default_data_dir())
    today = date.fromisoformat(args.today) if args.today else date.today()
    llm = TokenFactory(cfg, ledger=SpendLedger.from_config(cfg))
    brief = morning_check(store, llm, today, cfg)
    text = store.vault.restore(brief.text)
    out = store.root / f"brief-{today.isoformat()}.md"
    out.write_text(f"# Duly morning check, {today.isoformat()}\n\n{text}\n", encoding="utf-8")
    print(text)
    print(f"\n({len(brief.alerts)} items; written by {brief.written_by}; saved to {out})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
