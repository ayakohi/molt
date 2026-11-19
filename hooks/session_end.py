"""
session_end.py - Claude Code SessionEnd hook: hands a closing thread to auto_roll.py.

Claude Code runs this when a thread closes and passes the thread's details as JSON on stdin.
This file only checks the path and starts auto_roll.py as a separate process, then returns at
once, so Claude Code can finish closing. auto_roll.py waits for the file to settle and rolls
the thread only when its newest message from Claude is a roll note.
"""

import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import thread as T     # noqa: E402


def transcript(data: dict):
    """The thread file named in the hook input, when it is a .jsonl inside Claude Code's projects folder."""
    raw = data.get("transcript_path")
    if not isinstance(raw, str) or not raw:
        return None
    path = Path(raw).expanduser().resolve()
    projects = T.PROJECTS_DIR.resolve()
    if path.suffix != ".jsonl" or projects not in path.parents or not path.is_file():
        return None
    return path


def start_detached(args: list) -> None:
    """Start args as a process that keeps running after Claude Code exits."""
    quiet = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        try:
            subprocess.Popen(args, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **quiet)
        except OSError:                                   # the parent's job refuses breakaway
            subprocess.Popen(args, creationflags=flags, **quiet)
    else:
        subprocess.Popen(args, start_new_session=True, **quiet)


def main() -> None:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return
    path = transcript(data) if isinstance(data, dict) else None
    if not path:
        return
    import auto_roll                                      # the lock is taken here, before the worker exists
    if not auto_roll.reserve(path.stem):
        return
    try:
        start_detached([sys.executable, str(HERE / "auto_roll.py"), str(path)])
    except OSError as e:                                  # written down where a roll's outcome always goes
        auto_roll.log(f"{path.stem[:8]}  not rolled: the background roll could not start ({type(e).__name__}: {e})")
        auto_roll.pending(path.stem).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
