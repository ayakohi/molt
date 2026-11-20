"""
thread.py - read one Claude Code thread (a .jsonl transcript) and understand it.

Shared by roll.py (the trim) and readable.py (the readable copy).
Read-only: nothing in this file writes to disk.

Words used here:
  row        one line of the .jsonl file
  chat row   a row with a uuid (a message, tool call, attachment...). Chained by parentUuid.
  bookkeeping row   a row without a uuid (titles, request logs, modes...). Never removed.
  branch     the chain of chat rows from the newest message back to the start.
             Rewinds and edits leave dead branches in the same file; the branch is the live one.
  entry      a point where the conversation can be cut cleanly: the user typing, or another
             session's message arriving, with no tool call left waiting for its answer.
"""

import hashlib
import json
import re
from pathlib import Path

PROJECTS_DIR = Path.home() / ".claude" / "projects"

# Text the harness injects as if typed. Not the user, not another session.
SYSTEM_PREFIXES = (
    "<task-notification", "<local-command-", "<command-name", "<command-message",
    "<command-args", "<system-reminder", "[Request interrupted", "Caveat:",
    "<side-sessions-event", "<ci-monitor-event",
)
# Messages from someone other than the user, wrapped in an envelope that names the sender.
SESSION_RE = re.compile(r'<cross-session-message[^>]*from-name="([^"]*)"[^>]*>\s*(.*?)\s*(?:</cross-session-message>|$)', re.S)
AGENT_RE = re.compile(r'^\s*<agent-message[^>]*from="([^"]*)"[^>]*>\s*(.*?)\s*(?:</agent-message>|$)', re.S)


# Claude Code names a session started in the home folder after that folder, which is often a person's
# full name. Such names read as "home-…" instead, so the name never travels with a quoted search result.
HOME_SLUG = re.sub(r"[^a-z0-9]+", "-", Path.home().name.lower()).strip("-")


def sender(name: str) -> str:
    if HOME_SLUG and (name.lower() == HOME_SLUG or name.lower().startswith(HOME_SLUG + "-")):
        return "home" + name[len(HOME_SLUG):]
    return name


def envelope(text: str):
    """('session', sender, text) when text is a message another session or a subagent sent in; else None."""
    m = SESSION_RE.search(text)
    if m:
        return ("session", sender(m.group(1)), m.group(2))
    m = AGENT_RE.match(text)
    if m:
        return ("session", f"subagent {m.group(1)[:8]}", m.group(2))
    return None


def find(session_id: str) -> Path:
    """The thread's file, by full id or by its first 8 characters."""
    hits = [p for p in PROJECTS_DIR.glob(f"*/{session_id}*.jsonl") if not p.name.endswith("_archive.jsonl")]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise SystemExit(f"No thread found for {session_id!r}.")
    raise SystemExit(f"{session_id!r} matches {len(hits)} threads. Use more of the id.")


def file_state(path: Path) -> tuple:
    """(size, modified time) of a file: changes whenever anything writes to it."""
    st = path.stat()
    return st.st_size, st.st_mtime_ns


class Thread:
    def __init__(self, path: Path):
        self.path = path
        self.id = path.stem
        self.state = file_state(path)         # taken before reading, so a write during the read still shows as a change
        self.lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        self.rows = []
        for l in self.lines:
            try:
                self.rows.append(json.loads(l))
            except ValueError:
                self.rows.append({})          # unreadable line: its text stays in self.lines and is written back
                                              # as it was; as a row it is empty, so nothing counts or moves it
        self.by_uuid = {r["uuid"]: i for i, r in enumerate(self.rows) if r.get("uuid")}

    # ---- naming --------------------------------------------------------------

    def title(self) -> str:
        """The user's own name for the thread if they gave one, else Claude Code's."""
        custom = ai = None
        for r in self.rows:
            if r.get("type") == "custom-title":
                custom = r.get("customTitle")
            elif r.get("type") == "ai-title":
                ai = r.get("aiTitle")
        return (custom or ai or "Untitled").strip()

    # ---- the live branch -----------------------------------------------------

    def leaf(self):
        """Row index of the newest user or assistant row that is not a side thread, or None."""
        return next((i for i in range(len(self.rows) - 1, -1, -1)
                     if self.rows[i].get("type") in ("user", "assistant") and not self.rows[i].get("isSidechain")), None)

    def branch(self) -> list[int]:
        """Row indices of the live conversation, in file order.

        1. Walk parentUuid back from the newest message (through a compaction's
           logicalParentUuid where that row exists).
        2. Add side links: when several tools run at once, their answers hang off
           the chain rather than sitting on it. Claude Code gathers them back on
           resume, so we do too.
        3. Everything before where the walk ended (history a compaction cut loose)
           is read in file order.
        """
        chain, i = set(), self.leaf()
        while i is not None and i not in chain:
            chain.add(i)
            r = self.rows[i]
            i = self.by_uuid.get(r.get("parentUuid") or r.get("logicalParentUuid"))
        start = min(chain) if chain else len(self.rows)
        chain_uuids = {self.rows[i]["uuid"] for i in chain}
        live = set(chain)
        for i, r in enumerate(self.rows):
            if i > start and r.get("parentUuid") in chain_uuids and is_tool_result(r):
                live.add(i)
        for i in range(start):
            if self.rows[i].get("uuid") and not self.rows[i].get("isSidechain"):
                live.add(i)
        return sorted(live)

    # ---- what a row is -------------------------------------------------------

    def speaker(self, r: dict):
        """('user', text) | ('session', sender, text) | None for anything that is not a message to Claude.
        A sender is another session by its name, or a subagent as 'subagent <first 8 of its id>'."""
        if r.get("type") == "user" and not r.get("isMeta") and not r.get("isCompactSummary"):
            text = text_of(r)
            if text and not text.lstrip().startswith(SYSTEM_PREFIXES) and not is_tool_result(r):
                return envelope(text) or ("user", text)
        if r.get("type") == "attachment":
            a = r.get("attachment") or {}
            if a.get("type") == "queued_command":
                p = a.get("prompt") or ""
                if isinstance(p, list):
                    p = "\n".join(b.get("text", "") for b in p if isinstance(b, dict) and b.get("type") == "text")
                sent = envelope(p)
                if sent:
                    return sent
                p = p.strip()
                if p and not p.startswith(SYSTEM_PREFIXES):
                    return ("user", p)         # a message typed while Claude was busy
        return None

    # ---- size ----------------------------------------------------------------

    def context_at(self) -> list[int]:
        """For every row: the context size (tokens) of the most recent model call at or before it.
        Measured, not estimated: each assistant row records what the call actually read."""
        out, last = [], 0
        for r in self.rows:
            u = (r.get("message") or {}).get("usage") if r.get("type") == "assistant" else None
            if u:
                last = (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) \
                     + (u.get("cache_creation_input_tokens") or 0)
            out.append(last)
        return out

    def growth_at(self) -> list[int]:
        """For every row: tokens the conversation has GROWN by up to that row's most recent call.

        Built from the step between consecutive calls. A step down (a trim or a compaction made
        the context smaller) counts as 0, so the sum keeps meaning "what was added", however
        many trims came before. Tokens added after row e = growth[-1] - growth[e - 1]."""
        out, total, prev = [], 0, None
        for c in self.context_at():
            if prev is not None and c != prev:
                total += max(0, c - prev)
            prev = c
            out.append(total)
        return out

    # ---- clean cut points ----------------------------------------------------

    def entries(self) -> list[int]:
        """Branch rows where the conversation may be cut: a message from the user or another session,
        with every earlier tool call already answered."""
        branch = self.branch()
        # A call never answered anywhere (interrupted) cannot be split from its answer; ignore it.
        answered = {b.get("tool_use_id") for i in branch for b in blocks(self.rows[i]) if b.get("type") == "tool_result"}
        pending, out = set(), []
        for i in branch:
            r = self.rows[i]
            if not pending and self.speaker(r):
                out.append(i)
            for b in blocks(r):
                if b.get("type") == "tool_use" and b.get("id") in answered:
                    pending.add(b.get("id"))
                elif b.get("type") == "tool_result":
                    pending.discard(b.get("tool_use_id"))
        return out


# ---- row helpers -------------------------------------------------------------

def blocks(r: dict) -> list:
    c = (r.get("message") or {}).get("content")
    return [b for b in c if isinstance(b, dict)] if isinstance(c, list) else []


def text_of(r: dict) -> str:
    c = (r.get("message") or {}).get("content")
    if isinstance(c, str):
        return c
    return "\n".join(b["text"] for b in blocks(r) if b.get("type") == "text" and isinstance(b.get("text"), str))


def is_tool_result(r: dict) -> bool:
    return any(b.get("type") == "tool_result" for b in blocks(r))


def fingerprint(r: dict):
    """A short name for a row that survives a fork: Claude Code copies a thread with new uuids
    but keeps each row's timestamp and text, so the same row gets the same fingerprint in both.
    The user's and other sessions' messages and Claude's text rows have one. Every other row has None."""
    s = Thread.speaker(None, r)
    if s:
        text = s[-1]
    elif r.get("type") == "assistant" and text_of(r):
        text = text_of(r)
    else:
        return None
    return hashlib.sha1(((r.get("timestamp") or "") + "\n" + text).encode("utf-8", "surrogatepass")).hexdigest()[:16]
