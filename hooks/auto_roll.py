"""
auto_roll.py - roll a closed thread when its newest message from Claude is a roll note.

Started by session_end.py, never by hand:
    python auto_roll.py <path to the thread's .jsonl>

In order:
  1. Waits until the thread file has stopped changing (Claude Code may still be writing as it closes).
  2. Leaves the thread alone unless Claude's newest message carries a roll note: the note is the yes.
  3. Leaves it alone when it was already trimmed and not spoken in since, or when another roll of it runs.
  4. Runs the same trim as the menu (backup, check, undo on failure).
  5. Writes one line to molt.log. Only a roll that stopped also leaves a message, which
     session_start.py hands to Claude when the thread is reopened.
"""

import contextlib
import io
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import readable as R   # noqa: E402
import roll            # noqa: E402
import thread as T     # noqa: E402

SETTLE = 5             # seconds the file must stay unchanged before rolling
GIVE_UP = 120          # seconds to wait at most for it to settle
KEEP_K = int(os.environ.get("MOLT_KEEP_K") or roll.KEEP_K)   # override for tests only


def inbox() -> Path:
    """Folder of messages about rolls that stopped, one per thread, read once by session_start.py."""
    return roll.ARCHIVE / ".auto-roll"


def settled(path: Path) -> bool:
    """True once the file's size and time have held still for SETTLE seconds; False after GIVE_UP."""
    start, last, since = time.time(), None, time.time()
    while time.time() - start < GIVE_UP:
        st = path.stat()
        now = (st.st_size, st.st_mtime)
        if now != last:
            last, since = now, time.time()
        elif time.time() - since >= SETTLE:
            return True
        time.sleep(1)
    return False


def log(line: str) -> None:
    roll.ARCHIVE.mkdir(parents=True, exist_ok=True)
    with open(roll.ARCHIVE / "molt.log", "a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {line}\n")


def leave_message(thread_id: str, text: str) -> None:
    box = inbox()
    box.mkdir(parents=True, exist_ok=True)
    (box / f"{thread_id}.txt").write_text(text, encoding="utf-8")


def reopened_mark(thread_id: str) -> Path:
    """Written by session_start.py when this thread reopens while its roll is pending."""
    return inbox() / f"{thread_id}.reopened"


def reopened(thread_id: str):
    return "the thread was reopened, so it was left as it is" if reopened_mark(thread_id).exists() else None


def roll_closed(path: Path) -> None:
    calm = settled(path)
    t = T.Thread(path)
    if not roll.own_note(t):
        return                                            # an ordinary close, or a fork carrying another thread's note
    if not calm or reopened(t.id):
        log(f"{t.id[:8]}  not rolled: the thread kept changing or was reopened")
        return
    if roll.trimmed_notice(t, roll.load_families()):
        return
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        ok = roll.trim(t, KEEP_K, go=True, guard=lambda: reopened(t.id))
    report = out.getvalue().strip().replace(str(Path.home()), "~")    # paths read as ~/..., not the home folder's name
    when = f"{datetime.now():%d %b %Y, %H:%M}"
    if ok:
        log(f"{t.id[:8]}  rolled  {t.title()}")             # a roll that worked leaves nothing in the thread
        return
    log(f"{t.id[:8]}  not rolled  {t.title()}: {report.splitlines()[-1] if report else 'no output'}")
    if not reopened(t.id):                                # a reopen is the user's choice, not a failure to report
        leave_message(t.id, f"Molt tried to roll this thread on {when} and stopped. "
                            f"Tell {R.USER} what it reported:\n\n{report}")


claim = roll.claim     # moved: locks live in roll.py, shared by every roll; the archive lock is taken inside roll.trim


def pending(thread_id: str) -> Path:
    """The thread's roll lock: it exists from the moment the thread closes until its roll is over."""
    return inbox() / f"{thread_id}.lock"


def reserve(thread_id: str) -> bool:
    """Called by session_end.py before it starts this worker, so a reopen can never come before the lock.
    False when a roll of this thread is already pending: a second close steps aside."""
    inbox().mkdir(parents=True, exist_ok=True)
    if not claim(pending(thread_id), 0):
        return False                                      # a roll is pending: its reopen mark, if any, must stand
    reopened_mark(thread_id).unlink(missing_ok=True)      # a new roll of our own: only a reopen from now on counts
    return True


def main() -> None:
    if len(sys.argv) != 2:
        return
    path = Path(sys.argv[1])
    mine = pending(path.stem)
    if not mine.exists():                                 # only a worker that session_end.py reserved runs
        return
    try:
        roll_closed(path)
    except Exception as e:                                # a crash is written down, never silent
        log(f"{path.stem[:8]}  auto-roll crashed: {type(e).__name__}: {e}")
    finally:
        mine.unlink(missing_ok=True)
        reopened_mark(path.stem).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
