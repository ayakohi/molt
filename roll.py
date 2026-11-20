"""
roll.py - the rolling window for Claude Code threads.

Run with no arguments for the menu (that is what "Roll a thread.bat" does):
    python roll.py

Or directly:
    python roll.py trim <thread-id> [--keep 200] [--go] [--no-thin]   practice run unless --go; keep is in thousands of tokens
    python roll.py copy <thread-id>                       readable copy of the whole thread, changes nothing
    python roll.py history [<thread-id>]                  this thread's pages so far and what a roll now moves (the roll note copies it)
    python roll.py search "<words>" [--from ..] [--to ..] search this thread's moved-out pages by word, meaning and date (see search.py)
    python roll.py index                                  make the meaning index for pages that lack one

What a trim does, in order:
  1. Shortens old tool output (the middle of long results) in the part that stays, so the kept
     budget holds more conversation. The originals are saved. --no-thin skips this step.
  2. Picks the cut: the earliest clean point (a message from the user or another session, no tool call waiting)
     that leaves at most KEEP tokens of conversation after it, counting the shortening. Measured
     from the token counts Claude Code saved, not estimated. Only points after the last compaction count.
  3. Everything before the cut is moved out: a readable page and a raw copy go to the archive,
     into the folder of this thread's family (the thread plus every fork descended from it).
  4. Every bookkeeping line stays. Every pointer into the moved-out part is repaired.
  5. A backup is written, the new thread is built in a temporary file, and swapped in last,
     so the thread is never half-written.
  6. The new file is checked. Only then the family record and tree page are written.
     Any failure puts the thread back and removes what this run wrote.
  7. A thread that was just trimmed is not trimmed again until it has been spoken in.
"""

import copy
import time
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import thread as T     # noqa: E402
import readable as R   # noqa: E402

if hasattr(sys.stdout, "reconfigure"):      # in place, so loading this file twice never closes the first stream
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

__version__ = "0.4.0"

# ---- settings, read from environment variables (see "Settings" in README.md) ----------------
# MOLT_ARCHIVE  where the archive lives, eg. a folder inside an Obsidian vault. ROLL_ARCHIVE is the old name.
# MOLT_PAGES    html (the default), markdown, or both: the format of the readable pages.
ARCHIVE = Path(os.environ.get("MOLT_ARCHIVE") or os.environ.get("ROLL_ARCHIVE")
               or Path.home() / "Thread Archive").expanduser()
PAGE_FORMATS = {"html": ("html",), "markdown": ("md",), "md": ("md",), "both": ("html", "md")}


def page_formats() -> tuple:
    """('html',), ('md',) or ('html', 'md'), from MOLT_PAGES; anything unknown means html."""
    return PAGE_FORMATS.get((os.environ.get("MOLT_PAGES") or "html").strip().lower(), ("html",))


KEEP_K = 200                 # thousands of tokens of conversation left after a trim
FRESH_K = 40                 # thousands of tokens at the newest end that stay fully intact
THIN_OVER = 600              # a tool result longer than this many characters can be shortened
THIN_HEAD = 300              # characters kept from the start of a shortened result
THIN_TAIL = 200              # characters kept from the end
IMAGE_CHARS = 6000           # what one image in a tool result counts as when estimating the saving (about 1,500 tokens)
                             # shortcut: one fixed cost for every image; read real image sizes if the estimate misleads a cut
STAMP = "%Y-%m-%d %H.%M"     # page and raw file names start with this
FAMILY = ".family.json"      # hidden record in every family folder
WHOLE = "Whole thread copies"
THIN_MARK = "[molt: shortened from "
STAMP_SHAPE = "0000-00-00 00.00"   # stands in for the real stamp while a practice run estimates sizes


# ---- the archive -----------------------------------------------------------------

def safe(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return (name[:60].rstrip(" .") or "Untitled")


def unique_stamp(folder: Path) -> str:
    """'2026-10-28 23.40', or '2026-10-28 23.40 (2)' if a page or raw file from that minute already
    exists in this folder. Never overwrites a copy."""
    base = datetime.now().strftime(STAMP)
    stamp, n = base, 1
    while (list(folder.glob(f"{stamp}*.html")) or list(folder.glob(f"{stamp}*.md"))
           or list((folder / ".raw").glob(f"{stamp}*.jsonl"))):
        n += 1
        stamp = f"{base} ({n})"
    return stamp


def hide(path: Path) -> None:
    try:
        subprocess.run(["attrib", "+h", str(path)], capture_output=True)
    except OSError:
        pass


def write_hidden(path: Path, data) -> None:
    """Write a hidden file whole or not at all: the new text goes to a temporary file that replaces the
    old one in one step, so a crash mid-write never leaves half a record. Windows refuses to replace a
    hidden file, so the old one is shown first and the new one hidden after."""
    tmp = path.with_name(path.name + ".tmp")
    if isinstance(data, bytes):
        tmp.write_bytes(data)
    else:
        tmp.write_text(data, encoding="utf-8")
    if path.exists():
        try:
            subprocess.run(["attrib", "-h", str(path)], capture_output=True)
        except OSError:
            pass
    os.replace(tmp, path)
    hide(path)


# ---- families ----------------------------------------------------------------------

def load_families() -> list:
    """[(folder, record)] for every family folder in the archive."""
    out = []
    if ARCHIVE.exists():
        for d in sorted(ARCHIVE.iterdir()):
            f = d / FAMILY
            if d.is_dir() and f.exists():
                try:
                    rec = json.loads(f.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    rec = None
                if (isinstance(rec, dict) and isinstance(rec.get("threads"), dict) and isinstance(rec.get("pages"), list)
                        and all(isinstance(p, dict) and {"stamp", "thread", "fps", "prev"} <= p.keys() for p in rec["pages"])):
                    out.append((d, rec))
                else:
                    print(f"Skipped a damaged family record: {f}. Its pages are still there; restore the record from a copy.")
    return out


def page_by_stamp(rec: dict, stamp):
    return next((p for p in rec["pages"] if p["stamp"] == stamp), None)


def locate(t: "T.Thread", families: list, live_fps: list) -> dict:
    """Which family this thread belongs to and where it hangs in it. live_fps is [(row, fingerprint)]
    for the live branch. Returns folder, rec (a copy to edit), new (a family to be created), own
    (the thread already has a page here), prev (page the next page continues from), parent, joins_at."""
    for folder, rec in families:
        th = rec["threads"].get(t.id)
        if th is not None:
            mine = [p for p in rec["pages"] if p["thread"] == t.id]
            return dict(folder=folder, rec=copy.deepcopy(rec), new=False, own=bool(mine),
                        prev=mine[-1]["stamp"] if mine else th.get("parent"),
                        parent=th.get("parent"), joins_at=th.get("joins_at"))
    F = [fp for _, fp in live_fps]
    if F:
        best = None                                   # a page whose cut is where this thread begins: a fork made after a trim
        for folder, rec in families:
            for p in rec["pages"]:
                if p.get("cut_fp") == F[0] and (best is None or p["stamp"] >= best[1]["stamp"]):
                    best = (folder, p, rec)
        if best:
            folder, p, rec = best
            return dict(folder=folder, rec=copy.deepcopy(rec), new=False, own=False,
                        prev=p["stamp"], parent=p["stamp"], joins_at=None)
        for folder, rec in families:                  # a fork made before any trim: history shared with a moved-out page
            owner = {}
            for p in rec["pages"]:
                for fp in p["fps"]:
                    owner.setdefault(fp, p["stamp"])
            if F[0] in owner:
                j = 0
                while j + 1 < len(F) and F[j + 1] in owner:
                    j += 1
                return dict(folder=folder, rec=copy.deepcopy(rec), new=False, own=False,
                            prev=owner[F[j]], parent=owner[F[j]], joins_at=R.clock(t.rows[live_fps[j][0]]))
    rec = {"root": t.id, "name": t.title(), "threads": {}, "pages": []}
    return dict(folder=ARCHIVE / f"{safe(t.title())} ({t.id[:8]})", rec=rec, new=True, own=False,
                prev=None, parent=None, joins_at=None)


def trimmed_notice(t: "T.Thread", families: list):
    """The refusal text when this thread was trimmed and has not been spoken in since, else None."""
    leaf = t.leaf()
    if leaf is None:
        return None
    for _, rec in families:
        th = rec["threads"].get(t.id)
        if th and th.get("last_trim_leaf") and th["last_trim_leaf"] == t.rows[leaf].get("uuid"):
            when = datetime.fromisoformat(th["last_trim_when"]).strftime("%d %b %Y, %H:%M")
            return (f"This thread was trimmed on {when} and has not been spoken in since. "
                    "Reopen it in Claude Code and say something first. Nothing was changed.")
    return None


def shared_run(t: "T.Thread", live_list: list, cut: int, known: set) -> set:
    """Rows to leave out of a thread's first page: the leading run of moved-out live rows whose
    fingerprints are already stored in the family. Rows with no fingerprint inside the run go with it."""
    run, pending = set(), []
    for i in live_list:
        if i >= cut:
            break
        fp = T.fingerprint(t.rows[i])
        if fp is None:
            pending.append(i)
        elif fp in known:
            run.update(pending)
            run.add(i)
            pending = []
        else:
            break
    return run


# ---- shortening tool output --------------------------------------------------------

def flat(content):
    """(text, images) of a tool result's content: a string as is; a list as its text items joined
    by newlines, with every other item (an image) counted."""
    if isinstance(content, str):
        return content, 0
    if isinstance(content, list):
        texts = [b["text"] for b in content if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)]
        return "\n".join(texts), sum(1 for b in content if not (isinstance(b, dict) and b.get("type") == "text"))
    return "", 0


def thin_content(content, where: str):
    """The shortened content for one tool result, or None when it is short with no image, already
    shortened, or would not get smaller. Long text keeps its start and end; images (old screenshots)
    are taken out whatever the text length."""
    text, images = flat(content)
    long = len(text) > THIN_OVER
    if THIN_MARK in text or not (long or images):
        return None
    gone = f", {images} images removed" if images else ""
    body = text[:THIN_HEAD] + "\n…\n" + text[-THIN_TAIL:] if long else text
    new = body + f"\n{THIN_MARK}{len(text)} characters{gone}. Full output: Thread Archive, {where}]"
    return new if len(json.dumps(new)) < len(json.dumps(content)) else None


def thin_row(r: dict, where: str):
    """(new row, estimated tokens saved) when the row holds a tool result worth shortening, else None.
    Only the content of tool_result blocks changes; every other key and block is carried over as loaded."""
    msg = r.get("message") or {}
    content = msg.get("content")
    if not isinstance(content, list):
        return None
    blocks, before, after = [], 0, 0
    for b in content:
        if isinstance(b, dict) and b.get("type") == "tool_result":
            new = thin_content(b.get("content"), where)
            if new is not None:
                text, images = flat(b.get("content"))
                before += len(text) + images * IMAGE_CHARS     # an image is counted by its token cost, not its file size
                after += len(new)
                b = dict(b, content=new)
        blocks.append(b)
    if not after:
        return None
    return dict(r, message=dict(msg, content=blocks)), (before - after) // 4


def thin_savings(t: "T.Thread", grown: list, where: str) -> dict:
    """{row: estimated tokens saved} for every live row before the fresh zone that can be shortened.
    The fresh zone starts at the earliest row with at most FRESH_K thousand tokens of growth after it."""
    n = len(t.rows)
    fresh = next((i for i in range(n) if grown[-1] - (grown[i - 1] if i else 0) <= FRESH_K * 1000), n)
    out = {}
    for i in t.branch():
        if i >= fresh:
            break
        got = thin_row(t.rows[i], where)
        if got:
            out[i] = got[1]
    return out


# ---- choosing the cut --------------------------------------------------------------

def plan(t: "T.Thread", keep_k: int, thin: bool = True, where: str = "") -> dict:
    """What a trim would do. Keys: cut (row index, None means nothing to do), now, kept (estimated
    tokens left), note, thin ({row: estimated tokens saved} for rows that stay), thin_only (nothing
    moves out, old tool output is only shortened)."""
    now = t.context_at()[-1]
    keep = keep_k * 1000
    out = dict(cut=None, now=now, kept=now, note="", thin={}, thin_only=False)
    boundaries = [i for i, r in enumerate(t.rows) if r.get("subtype") == "compact_boundary"]
    after = boundaries[-1] if boundaries else -1
    entries = [e for e in t.entries() if e > after]
    if now <= keep:
        out["note"] = f"The thread is at {k(now)}, already under {keep_k}k."
        return out
    if not entries:
        out["note"] = "No clean cut point since the last compaction."
        return out
    grown = t.growth_at()
    after_cut = lambda e: grown[-1] - (grown[e - 1] if e > 0 else 0)   # tokens added from the cut on
    saves = thin_savings(t, grown, where) if thin else {}
    saving = [0] * (len(t.rows) + 1)                                   # saving[e]: tokens saved in rows e and later
    for i in range(len(t.rows) - 1, -1, -1):
        saving[i] = saving[i + 1] + saves.get(i, 0)
    fits = [e for e in entries if after_cut(e) - saving[e] <= keep]
    if fits:
        cut = fits[0]
    else:
        cut = entries[-1]
        out["note"] = f"The newest exchange alone is bigger than {keep_k}k, so only it stays."
    out["cut"], out["kept"] = cut, after_cut(cut) - saving[cut]
    out["thin"] = {i: s for i, s in saves.items() if i >= cut}
    before_cut = any(T.fingerprint(t.rows[i]) for i in t.branch() if i < cut)
    if cut == entries[0] and not before_cut:
        out["thin_only"] = True
        if not out["thin"]:
            out["cut"], out["note"] = None, "Nothing to move out and nothing to shorten."
    return out


NOTE_MARK = "[ROLL NOTE"


def roll_note(t: "T.Thread"):
    """The roll note's first line when the note is the last word of the thread: Claude's newest message
    opens with it, and nothing was said or done after it (see the /molt skill, skill/SKILL.md). Else None."""
    for i in reversed(t.branch()):
        r = t.rows[i]
        if t.speaker(r):
            return None                                   # the user or another session spoke after it: the veto
        if r.get("type") == "assistant":
            text = T.text_of(r).strip()
            if not text or any(b.get("type") == "tool_use" for b in T.blocks(r)):
                return None                               # Claude acted after the note (a tool call): no longer the last word
            first = text.splitlines()[0].strip()
            return first if first.startswith(NOTE_MARK) else None
    return None


def note_line(thread_id: str) -> str:
    """The roll note's first line for this thread, now. The id ties the note to the thread it was written in."""
    return f"{NOTE_MARK} · {datetime.now():%d %b %Y · %H:%M} · {thread_id[:8]}]"


def note_is_for(t: "T.Thread", line: str) -> bool:
    """True when the note's first line names this thread. A fork copies the note but not the id, so a
    fork never rolls on a note written in the thread it was copied from."""
    m = re.search(r"·\s*([0-9a-fA-F]{8})\s*\]\s*$", line or "")
    return bool(m) and m.group(1).lower() == t.id[:8].lower()


def own_note(t: "T.Thread"):
    """The roll note's first line when it is the last word AND names this thread; else None.
    Every roll, by hand or on close, asks this one question."""
    line = roll_note(t)
    return line if note_is_for(t, line) else None


def k(n: int) -> str:
    return f"{round(n / 1000)}k"


def prepare(t: "T.Thread", keep_k: int, thin: bool, families: list) -> dict:
    """Everything a trim needs, worked out without touching any file."""
    live_list = t.branch()
    live = set(live_list)
    live_fps = [(i, fp) for i in live_list for fp in [T.fingerprint(t.rows[i])] if fp]
    loc = locate(t, families, live_fps)
    p = plan(t, keep_k, thin, f"{loc['folder'].name}, .raw/{STAMP_SHAPE}.thinned.jsonl")
    job = dict(p, loc=loc, live=live, moved=[], dropped=set(), write=[], turns=[], first="", last="",
               fps=[], cut_fp=None)
    cut = p["cut"]
    if cut is None:
        return job
    job["cut_fp"] = next((fp for i, fp in live_fps if i >= cut), None)
    if p["thin_only"]:
        return job                                        # nothing moves out; old tool output is only shortened

    # Moved out: every chat row before the cut, then rows after the cut that hang off the moved-out
    # part without being live conversation (dead branches from a rewind, helper side threads).
    moved = [i for i in range(cut) if t.rows[i].get("uuid")]
    uuids = {t.rows[i]["uuid"] for i in moved}
    for i in range(cut, len(t.rows)):
        r = t.rows[i]
        if r.get("uuid") and i not in live and r.get("parentUuid") in uuids:
            moved.append(i)
            uuids.add(r["uuid"])

    # Shared history is stored once: a thread with no page of its own leaves out what the family has.
    dropped = set()
    if not loc["own"]:
        known = {fp for pg in loc["rec"]["pages"] for fp in pg["fps"]}
        dropped = shared_run(t, live_list, cut, known)
        if dropped:
            end = max(dropped)
            dropped_uuids = {t.rows[i]["uuid"] for i in dropped}
            for i in moved:
                r = t.rows[i]
                if i not in live and (i < end or r.get("parentUuid") in dropped_uuids):
                    dropped.add(i)
                    dropped_uuids.add(r["uuid"])
    write = [i for i in sorted(moved) if i not in dropped]
    readable_rows = [i for i in live_list if i < cut and i not in dropped]
    job.update(moved=moved, dropped=dropped, write=write)
    if write:
        job["turns"] = R.turns(t, readable_rows)
        job["first"] = R.clock(t.rows[readable_rows[0]]) if readable_rows else ""
        job["last"] = R.clock(t.rows[readable_rows[-1]]) if readable_rows else ""
        job["fps"] = [fp for i in readable_rows for fp in [T.fingerprint(t.rows[i])] if fp]
    return job


# ---- the check -------------------------------------------------------------------

def check(old: "T.Thread", new: "T.Thread", cut: int) -> list:
    """Problems found in the new thread, as plain sentences. An empty list means it is sound."""
    problems = []
    present = set(new.by_uuid)
    dangling = sum(1 for r in new.rows if r.get("uuid") and r.get("parentUuid") and r["parentUuid"] not in present)
    if dangling:
        problems.append(f"{dangling} broken pointers")
    live = new.branch()
    if not live or new.rows[live[0]].get("parentUuid") is not None:
        problems.append("the live conversation does not start cleanly")
    seen, orphans = set(), 0
    for i in live:
        for b in T.blocks(new.rows[i]):
            if b.get("type") == "tool_use":
                seen.add(b.get("id"))
            elif b.get("type") == "tool_result" and b.get("tool_use_id") not in seen:
                orphans += 1
    if orphans:
        problems.append(f"{orphans} tool results without their tool call")
    before = sum(1 for i in old.branch() if i >= cut and T.fingerprint(old.rows[i]))
    after = sum(1 for i in live if T.fingerprint(new.rows[i]))
    if before != after:
        problems.append(f"{before} spoken rows before, {after} after")
    return problems


# ---- the trim ---------------------------------------------------------------------

LOCK_WAIT = 300          # seconds a real trim waits for another roll to finish
LOCK_STALE = 3600        # seconds after which a lock is treated as left behind by a crash
                         # shortcut: age alone marks a lock abandoned; a roll is seconds of file work, so an
                         # hour is far past any live one. Record the owner's process id if rolls ever grow long.


def claim(lock: Path, wait: float) -> bool:
    """Create lock; when it exists, wait up to `wait` seconds for it to go. A lock older than
    LOCK_STALE seconds is left from a crash and is taken over."""
    lock.parent.mkdir(parents=True, exist_ok=True)
    end = time.time() + wait
    while True:
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return True
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > LOCK_STALE:
                    lock.unlink(missing_ok=True)
                    continue
            except OSError:
                continue
            if time.time() >= end:
                return False
            time.sleep(1)


def trim(t: "T.Thread", keep_k: int, go: bool, thin: bool = True, guard=None) -> bool:
    """guard: an optional function called just before the new thread is swapped in; a sentence it
    returns stops the trim with nothing changed. The automatic roll uses it to notice a reopen.
    A real trim holds the archive lock from planning to the end: forks share family folders, and
    one roll at a time keeps every family record whole."""
    if not go:
        return plan_and_trim(t, keep_k, False, thin, guard, {})
    lock = ARCHIVE / ".rolling.lock"
    if not claim(lock, LOCK_WAIT):
        print(f"Another roll held the archive for {LOCK_WAIT}s. Nothing was changed; try again.")
        return False
    done = {}
    try:
        ok = plan_and_trim(t, keep_k, True, thin, guard, done)
    finally:
        lock.unlink(missing_ok=True)
    if ok and done.get("new_raw"):                        # after the lock: a first model download may take minutes
        try:
            import search as S
            print(S.index_page(done["new_raw"]))
        except Exception as e:                            # the roll is done; this only costs search by meaning
            print(f"Search by meaning skipped ({type(e).__name__}: {e}). Run: python roll.py index")
    return ok


def plan_and_trim(t: "T.Thread", keep_k: int, go: bool, thin: bool, guard, done: dict) -> bool:
    """Print the plan; with go, carry it out. done receives new_raw, the page to index afterwards."""
    families = load_families()
    print(f"Thread : {t.title()}  ({t.id[:8]})")
    notice = trimmed_notice(t, families)
    if go and notice:
        print(notice)
        return False
    job = prepare(t, keep_k, thin, families)
    print(f"Size   : {k(job['now'])} tokens right now")
    if job["cut"] is None:
        print(job["note"])
        return False
    cut, loc = job["cut"], job["loc"]
    note = own_note(t)
    print(f"Note   : {note or 'NO ROLL NOTE — Claude has not written one (type /molt in the thread first)'}")
    if thin:
        saved = sum(job["thin"].values())
        print(f"Shortens: {len(job['thin'])} tool outputs, saving about {saved:,} tokens")
    print(f"Keeps  : about {k(job['kept'])} of conversation, from {R.clock(t.rows[cut]) or 'the cut'} onward")
    if job["thin_only"]:
        print("Moves out: nothing. The thread already fits once old tool output is shortened.")
    else:
        extra = f"; {len(job['dropped'])} rows already archived by this family are not saved again" if job["dropped"] else ""
        print(f"Moves out: {len(job['turns'])} turns ({len(job['write'])} saved lines){extra}")
    where = f"Family : {loc['folder']}"
    prev = page_by_stamp(loc["rec"], loc["prev"])
    if loc["new"]:
        print(f"{where}  (starts a new family)")
    elif prev:
        print(f"{where}  (continues from: {prev['title']} · {prev['first']} to {prev['last']})")
    else:
        print(f"{where}  (first page of this thread's line)")
    print("Keeps every bookkeeping line. Nothing is deleted.")
    if job["note"]:
        print(job["note"])
    if not go:
        if notice:
            print(f"(A real trim would be refused: {notice})")
        print("\nPractice run. Nothing was changed.")
        return False
    ok = execute(t, job, thin, guard)
    done["new_raw"] = job.get("new_raw")
    return ok


def execute(t: "T.Thread", job: dict, thin: bool, guard=None) -> bool:
    """The real trim. On any failure the thread is put back and everything this run wrote is removed."""
    cut, loc, live = job["cut"], job["loc"], job["live"]
    folder, rec = loc["folder"], loc["rec"]
    moved_set = set(job["moved"])
    backup = t.path.with_suffix(".jsonl.bak")
    shutil.copy2(t.path, backup)
    tmp = t.path.with_suffix(".jsonl.tmp")
    folder_existed = folder.exists()
    raw = folder / ".raw"
    raw_existed = raw.exists()
    before = {name: (folder / name).read_bytes() if (folder / name).exists() else None
              for name in (FAMILY, "index.html", "index.md")}
    formats = page_formats()
    made = []                                             # files this run wrote, for cleanup
    swapped = False                                       # until the swap, the thread file is untouched
    try:
        folder.mkdir(parents=True, exist_ok=True)
        raw.mkdir(exist_ok=True)
        if not raw_existed:
            hide(raw)
        stamp = unique_stamp(folder)
        where = f"{folder.name}, .raw/{stamp}.thinned.jsonl"

        # 1. archive: originals of shortened rows, then readable page + raw copy of what moves out
        if job["thin"]:
            made.append(raw / f"{stamp}.thinned.jsonl")
            with open(made[-1], "w", encoding="utf-8", newline="\n") as f:
                for i in sorted(job["thin"]):
                    f.write(t.lines[i] + "\n")
        page = None
        if job["write"]:
            file = f"{stamp} {safe(t.title())}.html"
            page = dict(stamp=stamp, file=file, thread=t.id, title=t.title(), prev=loc["prev"],
                        first=job["first"], last=job["last"], turns=len(job["turns"]),
                        cut_fp=job["cut_fp"], fps=job["fps"])
            made.append(raw / f"{stamp}.jsonl")
            with open(made[-1], "w", encoding="utf-8", newline="\n") as f:
                for i in job["write"]:
                    f.write(t.lines[i] + "\n")
            sub = (f"Moved out of the window on {datetime.now():%d %b %Y, %H:%M} · "
                   f"conversation from {job['first']} to {job['last']}")
            prev_page = page_by_stamp(rec, loc["prev"])
            if "html" in formats:                         # the record keeps the .html name; a .md page shares its stem
                made.append(folder / file)
                made[-1].write_text(R.to_html(t.title(), sub, job["turns"], prev_page, "index.html"), encoding="utf-8")
            if "md" in formats:
                made.append(folder / R.md_name(file))
                made[-1].write_text(R.to_md(t.title(), sub, job["turns"], prev_page, "index.md", page), encoding="utf-8")

        # 2. build the new thread: bookkeeping lines everywhere + chat lines not moved out.
        #    A live row whose parent moved out is joined to the previous kept live row, or starts the thread.
        moved_uuids = {t.rows[i]["uuid"] for i in moved_set}
        out, last_live, repaired, shortened = [], None, 0, 0
        for i, (line, r) in enumerate(zip(t.lines, t.rows)):
            if i in moved_set:
                continue
            if r.get("uuid"):
                fix = {}
                if r.get("parentUuid") in moved_uuids:
                    fix["parentUuid"] = last_live
                if r.get("logicalParentUuid") in moved_uuids:
                    fix["logicalParentUuid"] = None
                row = dict(r, **fix) if fix else r
                repaired += 1 if fix else 0
                if i in job["thin"]:
                    got = thin_row(row, where)
                    if got:
                        row, shortened = got[0], shortened + 1
                if row is not r:
                    line = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
                if i in live:
                    last_live = r["uuid"]
            out.append(line)

        # 3. swap in, only when the thread still holds exactly what was read: anything written since
        #    means it is open again, and swapping would cut a live thread
        tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
        if T.file_state(t.path) != t.state:
            raise RuntimeError("the thread changed while it was being rolled, so it is open. Close it and roll again")
        stop = guard() if guard else None
        if stop:
            raise RuntimeError(stop)
        os.replace(tmp, t.path)
        swapped = T.file_state(t.path)                    # what the thread looks like right after the swap

        # 4. check
        new = T.Thread(t.path)
        problems = check(t, new, cut)
        if problems:
            raise RuntimeError("the check found: " + "; ".join(problems))

        # 5. only now the family record and its tree page
        if page:
            rec["pages"].append(page)
        leaf = new.leaf()
        rec["threads"][t.id] = dict(title=t.title(), parent=loc["parent"], joins_at=loc["joins_at"],
                                    last_trim_leaf=new.rows[leaf].get("uuid") if leaf is not None else None,
                                    last_trim_when=datetime.now().isoformat(timespec="seconds"))
        write_hidden(folder / FAMILY, json.dumps(rec, indent=2, ensure_ascii=False))
        if "html" in formats:
            (folder / "index.html").write_text(R.to_index(rec), encoding="utf-8")
        if "md" in formats:
            (folder / "index.md").write_text(R.to_index_md(rec), encoding="utf-8")
    except Exception as e:
        print(f"\nThe trim failed: {e}")
        undo(t, backup, tmp, made, folder, folder_existed, raw, raw_existed, before, swapped)
        print(("The thread was put back from the backup" if swapped else "The thread was never touched") +
              " and everything this run wrote was removed.")
        return False

    print(f"\nDone. {repaired} pointers repaired. Check: OK")
    if shortened:
        print(f"Shortened {shortened} tool outputs; the originals are in {raw / (stamp + '.thinned.jsonl')}")
    tree = "index.html" if "html" in formats else "index.md"
    if page:
        print(f"Readable copy: {folder / (page['file'] if 'html' in formats else R.md_name(page['file']))}")
        job["new_raw"] = raw / f"{stamp}.jsonl"            # indexed by trim() once the archive lock is let go
    else:
        print("Nothing needed a new page.")
    print(f"Family tree: {folder / tree}")
    return True


def undo(t, backup, tmp, made, folder, folder_existed, raw, raw_existed, before, swapped=None) -> None:
    """Put the thread and the archive folder back the way they were before the run. swapped is the
    thread's file state right after the swap, or None when the swap never happened. The thread file
    is restored only when it still is exactly what was swapped in: anything newer was written by a
    reopened thread and is kept, with the backup beside it. The archive files of this run are then
    kept too, because the shortened thread no longer holds what they hold."""
    if swapped:
        try:
            if T.file_state(t.path) != swapped:
                print(f"The thread was written to after the swap, so it was left as it is, and this run's "
                      f"archive files were kept. The backup is {backup}")
                tmp.unlink(missing_ok=True)
                return
            shutil.copy2(backup, t.path)
        except OSError as e:
            print(f"Could not restore the thread: {e}. The backup is {backup}. This run's archive files were kept.")
            return
    for p in [tmp] + made:
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass
    for name, data in before.items():
        f = folder / name
        try:
            if data is None:
                f.unlink(missing_ok=True)
            elif not f.exists() or f.read_bytes() != data:
                write_hidden(f, data) if name == FAMILY else f.write_bytes(data)
        except OSError:
            pass
    try:
        if not raw_existed:
            raw.rmdir()
        if not folder_existed:
            folder.rmdir()
    except OSError:
        pass


def copy_whole(t: "T.Thread") -> Path:
    """Readable copy of the whole thread. Changes nothing in the thread and takes no part in families."""
    folder = ARCHIVE / WHOLE
    folder.mkdir(parents=True, exist_ok=True)
    rows = t.branch()
    first = R.clock(t.rows[rows[0]]) if rows else ""
    last = R.clock(t.rows[rows[-1]]) if rows else ""
    sub, ts = f"The whole thread · from {first} to {last}", R.turns(t, rows)
    stem = f"{unique_stamp(folder)} {safe(t.title())}"
    paths = []
    for fmt in page_formats():
        paths.append(folder / f"{stem}.{fmt}")
        paths[-1].write_text(R.to_html(t.title(), sub, ts) if fmt == "html" else R.to_md(t.title(), sub, ts),
                             encoding="utf-8")
        print(f"Readable copy: {paths[-1]}")
    return paths[0]


# moved: searching the moved-out pages now lives in search.py (by word, by meaning, by date).


# ---- history: what the roll note's PAST ROLLS section copies ------------------------------

def history(t: "T.Thread", keep_k: int) -> str:
    """Every page in this thread's line, oldest first, then what a roll now would move out."""
    families = load_families()
    job = prepare(t, keep_k, True, families)
    loc = job["loc"]
    line, p = [], page_by_stamp(loc["rec"], loc["prev"])
    while p:
        line.insert(0, p)
        p = page_by_stamp(loc["rec"], p["prev"])
    out = [f"Roll note first line: {note_line(t.id)}", "",
           f"{len(line)} earlier page{'' if len(line) == 1 else 's'} of this conversation {'is' if len(line) == 1 else 'are'} "
           f"already saved, oldest first." if line else "Nothing of this conversation is saved yet; this is its first roll."]
    out += [f"· Page {n} · {p['first']} → {p['last']} · {p['turns']} turns" for n, p in enumerate(line, 1)]
    if job["cut"] is None:
        out.append(f"· A roll now moves nothing out: {job['note']}")
    elif job["thin_only"] or not job["write"]:
        out.append("· This roll moves nothing new out; it only shortens old tool output and cuts history already saved.")
    else:
        out.append(f"· Page {len(line) + 1} (this roll) · {job['first']} → {job['last']} · {len(job['turns'])} turns")
    try:
        home = "~/" + loc["folder"].relative_to(Path.home()).as_posix()
    except ValueError:
        home = loc["folder"].as_posix()
    out.append(f"Folder: {home} · open its index page there for the tree.")
    out.append('Search them with: python ~/.claude/molt/roll.py search "words"   (add --from / --to for dates)')
    return "\n".join(out)


# ---- the menu -----------------------------------------------------------------------

def recent(limit: int = 15) -> list[Path]:
    files = [p for p in T.PROJECTS_DIR.glob("*/*.jsonl") if not p.name.endswith("_archive.jsonl")]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[:limit]


def menu() -> None:
    print(f"Molt {__version__} - the rolling window\n")
    paths = recent()
    families = load_families()
    threads = []
    for n, p in enumerate(paths, 1):
        t = T.Thread(p)
        threads.append(t)
        used = datetime.fromtimestamp(p.stat().st_mtime).strftime("%d %b %H:%M")
        size = "trimmed, reopen first" if trimmed_notice(t, families) else k(t.context_at()[-1])
        print(f"{n:>3}. {t.title()[:48]:<48} {size:>21}   last used {used}")
    pick = input("\nWhich thread? (number, or Enter to quit) ").strip()
    if not pick.isdigit() or not 1 <= int(pick) <= len(threads):
        return
    t = threads[int(pick) - 1]
    what = input("[t] trim it   [c] readable copy of the whole thread   (Enter to quit) ").strip().lower()
    if what == "c":
        copy_whole(t)
    elif what == "t":
        print()
        if trimmed_notice(t, families):
            print(trimmed_notice(t, families))
            return
        trim(t, KEEP_K, go=False)
        if plan(t, KEEP_K)["cut"] is None:
            return
        if not own_note(t) and input("\nNo roll note. Roll anyway? (y/n) ").strip().lower() != "y":
            print("Open the thread, type /molt, then run this again. Nothing was changed.")
            return
        if input("\nIs this thread's tab CLOSED? (y/n) ").strip().lower() != "y":
            print("Close it first, then run this again. Nothing was changed.")
            return
        if input("Trim it for real? (y/n) ").strip().lower() == "y":
            print()
            if trim(T.Thread(t.path), KEEP_K, go=True):
                print("\nNow reopen the thread from Claude Code's history.")


def main() -> None:
    a = sys.argv[1:]
    if not a:
        return menu()
    cmd = a[0]
    if cmd == "trim" and len(a) >= 2:
        keep = KEEP_K
        if "--keep" in a:
            given = a[a.index("--keep") + 1] if a.index("--keep") + 1 < len(a) else ""
            if not given.isdigit() or int(given) < 1:
                print("--keep takes a whole number of thousands of tokens, eg. --keep 200. Nothing was changed.")
                return
            keep = int(given)
        t = T.Thread(T.find(a[1]))
        if "--go" in a and not own_note(t) and "--no-note" not in a:
            print("No roll note in Claude's newest message. Type /molt in the thread first, "
                  "or add --no-note to roll anyway. Nothing was changed.")
            return
        trim(t, keep, go="--go" in a, thin="--no-thin" not in a)
    elif cmd == "copy" and len(a) >= 2:
        copy_whole(T.Thread(T.find(a[1])))
    elif cmd == "history":
        sid = a[1] if len(a) >= 2 else os.environ.get("CLAUDE_CODE_SESSION_ID")
        if not sid:
            print("Give a thread id: python roll.py history <id>")
            return
        print(history(T.Thread(T.find(sid)), KEEP_K))
    elif cmd in ("search", "index"):
        import search as S
        S.index_all() if cmd == "index" else S.main(a[1:])
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
