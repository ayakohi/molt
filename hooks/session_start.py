"""
session_start.py - Claude Code SessionStart hook: guards a reopened thread against its own pending roll,
and tells Claude, once, when a roll of its thread stopped.

1. When the thread reopens while auto_roll.py still holds its lock, this marks it reopened, so the roll
   leaves the file alone, and waits up to WAIT seconds for that roll to let go before the thread loads.
2. When auto_roll.py left a message for the thread (only a roll that stopped does), this prints it,
   which Claude Code adds to the conversation, and removes it so it is said only once.
Every other start prints nothing.
"""

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import roll            # noqa: E402  (also sets stdout to UTF-8)

ID = re.compile(r"[0-9a-fA-F-]{8,64}")
WAIT = 30              # seconds to wait for a pending roll of this thread to finish


def main() -> None:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return
    sid = data.get("session_id") if isinstance(data, dict) else None
    if not isinstance(sid, str) or not ID.fullmatch(sid):
        return
    box = roll.ARCHIVE / ".auto-roll"
    lock = box / f"{sid}.lock"
    if lock.exists():
        (box / f"{sid}.reopened").write_text("", encoding="utf-8")
        end = time.time() + WAIT
        while lock.exists() and time.time() < end:
            time.sleep(0.5)
    note = box / f"{sid}.txt"
    if note.is_file():
        print(note.read_text(encoding="utf-8"))
        note.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
