"""
Tests for molt. Run:  python tests/test_molt.py

Standard library only. Every test builds synthetic threads (invented filler text) in a temporary
folder and trims them with roll.trim(...). Nothing here touches ~/.claude/projects or the real archive.
Prints one PASS or FAIL line per test and exits non-zero if any failed.
"""

import contextlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import traceback
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="molt-test-"))
os.environ["ROLL_ARCHIVE"] = str(TMP / "archive")      # set before roll is imported
os.environ.pop("CLAUDE_CODE_SESSION_ID", None)       # tests choose their own search scope
sys.path.insert(0, str(ROOT))

import roll        # noqa: E402
import thread      # noqa: E402
import readable    # noqa: E402
import search      # noqa: E402

REAL_EMBEDDER = search.embedder
search.embedder = lambda: None     # trims in tests index by word only; test_search_meaning brings its own embedder

BASE = datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)
STEP = 2500          # tokens each assistant row adds, so one exchange (two assistant rows) adds 5000
EXCHANGE = 2 * STEP


# ---- building synthetic threads -------------------------------------------------

def when(n: int) -> str:
    return (BASE + timedelta(minutes=n)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def chat(kind, parent, n, content, **extra):
    r = {"parentUuid": parent, "isSidechain": False, "type": kind, "uuid": str(uuid.uuid4()),
         "timestamp": when(n), "message": {"role": kind, "content": content}}
    if "usage" in extra:                          # Claude Code keeps the token counts inside the message
        r["message"]["usage"] = extra.pop("usage")
    r.update(extra)
    return r


def big(label: str, size: int) -> str:
    """Tool output text of about `size` characters, with a recognisable start and end."""
    body = "".join(f"filler line {i} about {label}\n" for i in range(size // 20 + 1))[:size - 20]
    return f"START {label}\n" + body + f"\nEND {label}"


def write_rows(path: Path, rows: list) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def read_rows(path: Path) -> list:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def build(path: Path, segments, result_chars: int = 8000, title: str = "Pizza chat") -> Path:
    """One exchange = the user speaks, Claude thinks and calls a tool, the tool answers, Claude replies.
    segments is [(topic, count)], giving texts like 'pizza talk 1'."""
    rows = [{"type": "ai-title", "aiTitle": title}, {"type": "permission-mode", "mode": "default"}]
    parent, n, usage = None, 0, 1000
    for topic, count in segments:
        for j in range(1, count + 1):
            tid = f"toolu_{topic}{j}_{uuid.uuid4().hex[:6]}"
            u = chat("user", parent, n, f"{topic} talk {j}")
            usage += STEP
            a = chat("assistant", u["uuid"], n + 1,
                     [{"type": "thinking", "thinking": f"musing on {topic} {j}", "signature": "sig"},
                      {"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": f"echo {topic} {j}"}}],
                     usage={"input_tokens": usage})
            out = big(f"{topic} {j}", result_chars)
            res = chat("user", a["uuid"], n + 2,
                       [{"type": "tool_result", "tool_use_id": tid, "content": out, "is_error": False}],
                       toolUseResult={"stdout": out, "stderr": ""})
            usage += STEP
            b = chat("assistant", res["uuid"], n + 3, [{"type": "text", "text": f"{topic} reply {j}"}],
                     usage={"input_tokens": usage})
            rows += [u, a, res, b]
            parent, n = b["uuid"], n + 4
    write_rows(path, rows)
    return path


def new_thread_path(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{uuid.uuid4()}.jsonl"


def fork(path: Path, title=None) -> Path:
    """Copy a thread the way Claude Code forks: new thread id, new uuid on every row, same timestamps and text."""
    rows = read_rows(path)
    ids = {r["uuid"]: str(uuid.uuid4()) for r in rows if r.get("uuid")}
    for r in rows:
        if r.get("uuid"):
            r["uuid"] = ids[r["uuid"]]
        for key in ("parentUuid", "logicalParentUuid"):
            if r.get(key) in ids:
                r[key] = ids[r[key]]
        if title and r.get("type") == "ai-title":
            r["aiTitle"] = title
    new = path.with_name(f"{uuid.uuid4()}.jsonl")
    write_rows(new, rows)
    return new


def speak(path: Path, label: str = "more", times: int = 1) -> None:
    """Imitate the thread being used again: one user row and one assistant row, later and larger, per round."""
    rows = read_rows(path)
    chats = [r for r in rows if r.get("type") in ("user", "assistant")]
    parent = chats[-1]["uuid"]
    last = max(datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00")) for r in rows if r.get("timestamp"))
    usage = [r["message"]["usage"]["input_tokens"] for r in chats if r["type"] == "assistant"][-1]
    for j in range(1, times + 1):
        last += timedelta(minutes=1)
        u = chat("user", parent, 0, f"{label} talk {j}")
        u["timestamp"] = last.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        last += timedelta(minutes=1)
        usage += EXCHANGE
        a = chat("assistant", u["uuid"], 0, [{"type": "text", "text": f"{label} reply {j}"}], usage={"input_tokens": usage})
        a["timestamp"] = last.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        rows += [u, a]
        parent = a["uuid"]
    write_rows(path, rows)


# ---- helpers for looking at results ------------------------------------------------

def fresh() -> Path:
    """A new temporary folder, with the archive pointed inside it."""
    d = Path(tempfile.mkdtemp(dir=TMP))
    roll.ARCHIVE = d / "archive"
    return d


def trim(path: Path, keep_k: int, **kw):
    """Real trim, output captured. Returns (ok, printed text)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ok = roll.trim(thread.Thread(path), keep_k, go=True, **kw)
    return ok, buf.getvalue()


def family_folders() -> list:
    a = roll.ARCHIVE
    return [d for d in a.iterdir() if (d / roll.FAMILY).exists()] if a.exists() else []


def record(folder: Path) -> dict:
    return json.loads((folder / roll.FAMILY).read_text(encoding="utf-8"))


def only_family():
    fams = family_folders()
    assert len(fams) == 1, f"expected one family folder, found {len(fams)}"
    return fams[0], record(fams[0])


def fps(rows) -> list:
    return [fp for r in rows for fp in [thread.fingerprint(r)] if fp]


def raw_rows(folder: Path) -> list:
    out = []
    for f in sorted((folder / ".raw").glob("*.jsonl")):
        if not f.name.endswith(".thinned.jsonl"):
            out += read_rows(f)
    return out


def listing(folder: Path) -> list:
    return sorted(str(p.relative_to(folder)) for p in folder.rglob("*"))


def hidden(path: Path) -> bool:
    return bool(os.stat(path).st_file_attributes & 2)


def spoken_count(path: Path) -> int:
    t = thread.Thread(path)
    return sum(1 for r in t.rows if t.speaker(r))


def page_of(rec: dict, tid: str) -> dict:
    return next(p for p in rec["pages"] if p["thread"] == tid)


def small(topic_segments, path):
    """A thread whose tool results are too short to be shortened, so cut sizes are exact."""
    return build(path, topic_segments, result_chars=300)


# ---- the tests ---------------------------------------------------------------------

def test_plain_trim():
    d = fresh()
    p = build(new_thread_path(d / "proj"), [("pizza", 20), ("fruit", 20), ("tea", 10)], result_chars=3000)
    before = read_rows(p)
    ok, out = trim(p, 100)
    assert ok, out
    assert "Check: OK" in out
    folder, rec = only_family()
    after = read_rows(p)
    assert Counter(fps(before)) == Counter(fps(after)) + Counter(fps(raw_rows(folder))), "a spoken row was lost or doubled"
    pages = [f for f in folder.glob("*.html") if f.name != "index.html"]
    assert len(pages) == 1 and (folder / "index.html").exists()
    assert hidden(folder / roll.FAMILY), ".family.json should be hidden"
    assert len(rec["pages"]) == 1 and rec["pages"][0]["file"] == pages[0].name and rec["pages"][0]["prev"] is None
    assert rec["pages"][0]["cut_fp"] == fps(after)[0]
    assert rec["pages"][0]["fps"] == fps(raw_rows(folder))
    assert rec["threads"][p.stem]["last_trim_leaf"] == thread.Thread(p).rows[thread.Thread(p).leaf()]["uuid"]


def test_thinning():
    d = fresh()
    orig = build(new_thread_path(d / "proj"), [("pizza", 30), ("fruit", 30)], result_chars=8000)
    plain = fork(orig)                       # same content, trimmed without shortening for comparison
    before = {r["uuid"]: r for r in read_rows(orig) if r.get("uuid")}
    before_lines = {json.loads(l)["uuid"]: l for l in orig.read_text(encoding="utf-8").splitlines() if '"uuid"' in l}
    ok, out = trim(orig, 100)
    assert ok, out
    folder, rec = only_family()
    after = read_rows(orig)
    shortened = {r["uuid"] for r in after if r.get("uuid") and "[molt: shortened from" in json.dumps(r)}
    assert shortened, "nothing was shortened"
    # the fresh zone: untouched
    g = thread.Thread(plain).growth_at()
    fresh_start = next(i for i in range(len(g)) if g[-1] - (g[i - 1] if i else 0) <= roll.FRESH_K * 1000)
    plain_rows = read_rows(plain)
    fresh_ts = {plain_rows[i]["timestamp"] for i in range(fresh_start, len(plain_rows)) if plain_rows[i].get("timestamp")}
    checked_fresh = 0
    for r in after:
        if r.get("uuid") and r["timestamp"] in fresh_ts and r["type"] == "user" and thread.is_tool_result(r):
            assert r["uuid"] not in shortened, "a fresh-zone result was shortened"
            assert r["message"] == before[r["uuid"]]["message"]
            checked_fresh += 1
    assert checked_fresh >= 3, "no fresh-zone results were checked"
    # the shortened ones: head, tail, note, full text in .thinned.jsonl
    thinned_file = next((folder / ".raw").glob("*.thinned.jsonl"))
    saved = {json.loads(l)["uuid"]: l for l in thinned_file.read_text(encoding="utf-8").splitlines()}
    assert set(saved) == shortened, "the originals file does not match the shortened rows"
    for uid, line in saved.items():
        assert line == before_lines[uid], "an original line was changed"
    stamp = thinned_file.name[:-len(".thinned.jsonl")]
    for r in after:
        if r.get("uuid") in shortened:
            blk = r["message"]["content"][0]
            old = before[r["uuid"]]["message"]["content"][0]
            assert set(blk) == set(old) and blk["tool_use_id"] == old["tool_use_id"] and blk["is_error"] is False
            text = old["content"]
            assert blk["content"].startswith(text[:roll.THIN_HEAD] + "\n…\n" + text[-roll.THIN_TAIL:] + "\n[molt: shortened from " + str(len(text)) + " characters.")
            assert blk["content"].endswith(f"Full output: Thread Archive, {folder.name}, .raw/{stamp}.thinned.jsonl]")
            assert r["toolUseResult"] == before[r["uuid"]]["toolUseResult"]
    # nothing else changed: thinking and tool_use blocks, top-level keys, other messages
    for r in after:
        if not r.get("uuid"):
            continue
        o = before[r["uuid"]]
        for key in set(r) | set(o):
            if key not in ("parentUuid", "message"):
                assert r.get(key) == o.get(key), f"key {key} changed"
        for a, b in zip(thread.blocks(r), thread.blocks(o)):
            if a.get("type") in ("thinking", "tool_use"):
                assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    # more conversation stays than with shortening off
    kept_thin = spoken_count(orig)
    ok2, out2 = trim(plain, 100, thin=False)
    assert ok2, out2
    kept_plain = spoken_count(plain)
    assert kept_thin > kept_plain, f"{kept_thin} turns kept with shortening, {kept_plain} without"


def test_guard():
    d = fresh()
    p = small([("pizza", 20), ("fruit", 20), ("tea", 10)], new_thread_path(d / "proj"))
    ok, out = trim(p, 100)
    assert ok, out
    folder, _ = only_family()
    snap_thread, snap_fam, snap_list = p.read_bytes(), (folder / roll.FAMILY).read_bytes(), listing(folder)
    ok, out = trim(p, 100)
    assert not ok and "has not been spoken in since" in out and "Nothing was changed." in out, out
    assert p.read_bytes() == snap_thread and (folder / roll.FAMILY).read_bytes() == snap_fam and listing(folder) == snap_list
    speak(p, "guard")
    ok, out = trim(p, 100)
    assert ok, out
    assert len(record(folder)["pages"]) == 2


def test_fork_after_trim():
    d = fresh()
    root = small([("pizza", 16), ("fruit", 16), ("tea", 12)], new_thread_path(d / "proj"))
    ok, out = trim(root, 60)
    assert ok, out
    child = fork(root, "Cheese chat")
    speak(child, "cheese", 3)
    ok, out = trim(child, 60)
    assert ok, out
    folder, rec = only_family()
    root_page, child_page = page_of(rec, root.stem), page_of(rec, child.stem)
    assert child_page["prev"] == root_page["stamp"]
    assert rec["threads"][child.stem]["parent"] == root_page["stamp"]
    assert len(rec["threads"]) == 2
    # the pages link to each other and to the tree
    html_child = (folder / child_page["file"]).read_text(encoding="utf-8")
    assert "family tree" in html_child and "← earlier: Pizza chat" in html_child and 'href="index.html"' in html_child
    assert 'href="' + root_page["file"].replace(" ", "%20") + '"' in html_child
    tree = (folder / "index.html").read_text(encoding="utf-8")
    assert "Cheese chat" in tree and "continues in Claude Code: Cheese chat" in tree and "continues in Claude Code: Pizza chat" in tree


def test_fork_before_trim():
    d = fresh()
    a = small([("pizza", 16), ("fruit", 16), ("tea", 12)], new_thread_path(d / "proj"))
    b = fork(a, "Cheese chat")
    c = fork(a, "Third chat")                # will be trimmed with nothing of its own to write
    ok, out = trim(a, 60)
    assert ok, out
    speak(b, "cheese", 4)
    ok, out = trim(b, 60)
    assert ok, out
    folder, rec = only_family()
    pa, pb = page_of(rec, a.stem), page_of(rec, b.stem)
    assert rec["threads"][b.stem]["parent"] == pa["stamp"] and rec["threads"][b.stem]["joins_at"]
    assert pb["prev"] == pa["stamp"]
    shared = set(pa["fps"])
    assert shared and not (shared & set(pb["fps"])), "shared rows are stored in both pages"
    seen = Counter(fp for p in rec["pages"] for fp in p["fps"])
    assert max(seen.values()) == 1, "a row appears in more than one page"
    raw_texts = Counter(fp for fp in fps(raw_rows(folder)))
    assert max(raw_texts.values()) == 1, "a row appears in more than one raw file"
    assert len(pb["fps"]) == 4 * 2, f"expected 8 rows in the second page, found {len(pb['fps'])}"
    # c: everything it moves out is already archived, so it gets a thread record and no page
    stamps = {p["stamp"] for p in rec["pages"]}
    files_before = listing(folder)
    c_before = spoken_count(c)
    ok, out = trim(c, 60)
    assert ok, out
    rec = record(folder)
    assert {p["stamp"] for p in rec["pages"]} == stamps, "a page was written"
    assert [f for f in listing(folder) if f not in files_before] == [], "a file was written"
    assert rec["threads"][c.stem]["parent"] == pb["stamp"] and spoken_count(c) < c_before   # tea 1-4 are in b's page, the last shared row
    assert "continues in Claude Code: Third chat" in (folder / "index.html").read_text(encoding="utf-8")


def test_tree():
    d = fresh()
    root = small([("pizza", 16), ("fruit", 16), ("tea", 12)], new_thread_path(d / "proj"))
    ok, out = trim(root, 60)
    assert ok, out
    cheese = {}
    for n, rounds in ((1, 3), (2, 5)):
        c = fork(root, f"Cheese chat {n}")
        speak(c, f"cheese{n}", rounds)
        ok, out = trim(c, 60)
        assert ok, out
        cheese[n] = c
    burrito = {}
    for n, (parent, rounds) in {1: (1, 4), 2: (1, 6), 3: (2, 8), 4: (2, 10)}.items():
        b = fork(cheese[parent], f"Burrito chat {n}")
        speak(b, f"burrito{n}", rounds)
        ok, out = trim(b, 60)
        assert ok, out
        burrito[n] = b
    folder, rec = only_family()
    assert len(rec["threads"]) == 7, f"{len(rec['threads'])} threads in the family"
    assert len(list(roll.ARCHIVE.iterdir())) == 1, "more than one folder in the archive"
    by_stamp = {p["stamp"]: p for p in rec["pages"]}
    for n, b in burrito.items():
        chain, p = [], page_of(rec, b.stem)
        while p:
            chain.append(p["thread"])
            p = by_stamp.get(p["prev"])
        parent = cheese[1 if n <= 2 else 2]
        assert chain == [b.stem, parent.stem, root.stem], f"burrito {n}: {chain}"
    tree = (folder / "index.html").read_text(encoding="utf-8")
    for title in ["Pizza chat"] + [f"Cheese chat {n}" for n in (1, 2)] + [f"Burrito chat {n}" for n in range(1, 5)]:
        assert title in tree, f"{title} missing from the tree page"
    assert tree.count("<ul>") >= 2          # nested lists


def test_failed_trim():
    d = fresh()
    p = small([("pizza", 16), ("fruit", 16), ("tea", 12)], new_thread_path(d / "proj"))
    # a first trim that fails leaves no family folder at all
    original = p.read_bytes()
    real = roll.check
    roll.check = lambda *a: ["forced failure"]
    try:
        ok, out = trim(p, 60)
        assert not ok and "forced failure" in out, out
        assert p.read_bytes() == original, "the thread changed"
        assert not roll.ARCHIVE.exists() or not list(roll.ARCHIVE.iterdir()), "a folder was left behind"
    finally:
        roll.check = real
    ok, out = trim(p, 60)
    assert ok, out
    speak(p, "again", 3)
    folder, rec = only_family()
    snap = dict(thread=p.read_bytes(), fam=(folder / roll.FAMILY).read_bytes(),
                idx=(folder / "index.html").read_bytes(), files=listing(folder))
    roll.check = lambda *a: ["forced failure"]
    try:
        ok, out = trim(p, 60)
    finally:
        roll.check = real
    assert not ok and "forced failure" in out, out
    assert p.read_bytes() == snap["thread"], "the thread changed"
    assert listing(folder) == snap["files"], "a page or raw file was left behind"
    assert (folder / roll.FAMILY).read_bytes() == snap["fam"] and (folder / "index.html").read_bytes() == snap["idx"]
    # a failure after the pages and the thread are done, while the family record is being written
    real_index = readable.to_index
    def boom(rec):
        raise OSError("disk full, pretend")
    readable.to_index = boom
    try:
        ok, out = trim(p, 60)
    finally:
        readable.to_index = real_index
    assert not ok and "disk full" in out, out
    assert p.read_bytes() == snap["thread"] and listing(folder) == snap["files"]
    assert (folder / roll.FAMILY).read_bytes() == snap["fam"] and (folder / "index.html").read_bytes() == snap["idx"]
    assert hidden(folder / roll.FAMILY), ".family.json lost its hidden mark"
    ok, out = trim(p, 60)                    # and the same thread still trims fine afterwards
    assert ok, out


def run_search(*argv) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        search.main(list(argv))
    return buf.getvalue()


def test_search():
    d = fresh()
    p = small([("pizza", 16), ("fruit", 16), ("tea", 12)], new_thread_path(d / "proj"))
    ok, out = trim(p, 60)
    assert ok, out
    folder, rec = only_family()
    # a decoy in a .thinned.jsonl file must not be searched
    decoy = {"type": "user", "uuid": "z", "timestamp": when(1), "message": {"role": "user", "content": "needlework decoy"}}
    (folder / ".raw" / "2026-01-01 00.00.thinned.jsonl").write_text(json.dumps(decoy) + "\n", encoding="utf-8")

    out = run_search("pizza", "talk", "--all", "--limit", "50")
    assert "16 matches" in out and "by word only" in out, out
    assert out.index("pizza talk 16") < out.index("pizza talk 15") < out.index("pizza talk 1\n"), "newest first"
    assert "needlework" not in out
    assert "honest empty" in run_search("needlework", "--all")
    assert "honest empty" in run_search("tea", "talk", "--all")          # still in the thread, never moved out
    assert "honest empty" in run_search("musing", "--all")               # thinking is not searched
    assert "honest empty" in run_search("echo", "--all")                 # tool calls are not searched

    # dates: a day, a month, before and after
    day = search.local_day(when(0))
    assert "16 matches" in run_search("pizza", "talk", "--all", "--from", day, "--to", day)
    assert "16 matches" in run_search("pizza", "talk", "--all", "--from", day[:7])
    assert "honest empty" in run_search("pizza", "talk", "--all", "--to", "2000")
    assert "honest empty" in run_search("pizza", "talk", "--all", "--from", "2999")

    # browse: dates without words, oldest first; an id opens the message with its neighbours
    out = run_search("--all", "--from", day, "--to", day)
    assert "oldest first" in out and out.index("pizza talk 1\n") < out.index("pizza talk 2\n"), out
    ident = re.search(r"id: (\S+)\n  pizza talk 3\b", out).group(1)
    opened = run_search("--open", ident)
    assert "▶" in opened and "pizza talk 3" in opened and "pizza reply 2" in opened, opened

    # scope: the family of the thread that searches, read from Claude Code's session id
    os.environ["CLAUDE_CODE_SESSION_ID"] = p.stem
    assert f"Searching {folder.name}." in run_search("pizza", "talk")
    os.environ["CLAUDE_CODE_SESSION_ID"] = "00000000-0000-0000-0000-000000000000"
    assert "nothing moved out yet" in run_search("pizza", "talk")
    del os.environ["CLAUDE_CODE_SESSION_ID"]

    # history: the pages so far, then what a roll now would move
    first = roll.history(thread.Thread(p), 60)
    assert "1 earlier page of this conversation is already saved" in first and "· Page 1 ·" in first, first
    speak(p, "cheese", times=20)
    again = roll.history(thread.Thread(p), 60)
    assert "Page 2 (this roll)" in again and "Search them with" in again, again


def add_row(path: Path, kind: str, content) -> None:
    """Append one chat row after the newest one, a minute later."""
    rows = read_rows(path)
    last = [r for r in rows if r.get("type") in ("user", "assistant")][-1]
    used = [x["message"]["usage"] for x in rows if x.get("type") == "assistant" and x["message"].get("usage")]
    r = chat(kind, last["uuid"], 0, content, **({"usage": dict(used[-1])} if kind == "assistant" and used else {}))
    r["timestamp"] = (datetime.fromisoformat(last["timestamp"].replace("Z", "+00:00")) + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    write_rows(path, rows + [r])


def test_guards():
    import importlib
    sys.path.insert(0, str(ROOT / "hooks"))
    auto = importlib.import_module("auto_roll")
    start = importlib.import_module("session_start")
    auto.SETTLE = 0
    d = fresh()
    note = lambda path: roll.note_line(path.stem) + "\nOn waking: read this note."

    # the veto: the user speaks after the note and closes before any reply
    p = build(new_thread_path(d / "proj"), [("pizza", 60)])
    add_row(p, "assistant", [{"type": "text", "text": note(p)}])
    assert roll.roll_note(thread.Thread(p))
    add_row(p, "user", "wait, one more thing")
    assert roll.roll_note(thread.Thread(p)) is None
    rows = len(read_rows(p))
    auto.roll_closed(p)
    assert len(read_rows(p)) == rows and not family_folders()

    # Claude acting after the note (a tool call) takes away its last word
    v = build(new_thread_path(d / "proj"), [("pizza", 60)])
    add_row(v, "assistant", [{"type": "text", "text": note(v)}])
    add_row(v, "assistant", [{"type": "tool_use", "id": "toolu_after", "name": "Bash", "input": {"command": "ls"}}])
    assert roll.roll_note(thread.Thread(v)) is None

    # a fork carries the note but not its thread id: closing the fork rolls nothing; the original still rolls
    w = build(new_thread_path(d / "proj"), [("pizza", 60)])
    add_row(w, "assistant", [{"type": "text", "text": note(w)}])
    wf = fork(w)
    rows = len(read_rows(wf))
    auto.roll_closed(wf)
    assert len(read_rows(wf)) == rows and not family_folders()
    auto.roll_closed(w)
    assert len(family_folders()) == 1
    shutil.rmtree(family_folders()[0])

    # a note only quoted inside a message is not a note
    q = build(new_thread_path(d / "proj"), [("pizza", 60)])
    add_row(q, "assistant", [{"type": "text", "text": "The note starts like this: [ROLL NOTE · date]"}])
    assert roll.roll_note(thread.Thread(q)) is None

    # a thread written to after it was read is open again: the swap is refused and the new line survives
    r = build(new_thread_path(d / "proj"), [("pizza", 60)])
    t = thread.Thread(r)
    add_row(r, "user", "I reopened it")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert not roll.trim(t, 60, go=True)
    assert "it is open" in buf.getvalue() and "never touched" in buf.getvalue()
    assert read_rows(r)[-1]["message"]["content"] == "I reopened it" and not family_folders()

    # a reopen while the roll is pending: session_start marks it, the roll leaves it alone, no message is left
    s = build(new_thread_path(d / "proj"), [("pizza", 60)])
    add_row(s, "assistant", [{"type": "text", "text": note(s)}])
    assert roll.note_is_for(thread.Thread(s), roll.roll_note(thread.Thread(s)))
    box = roll.ARCHIVE / ".auto-roll"
    box.mkdir(parents=True, exist_ok=True)
    (box / f"{s.stem}.lock").write_text("", encoding="utf-8")
    start.WAIT = 0
    sys.stdin = io.StringIO(json.dumps({"session_id": s.stem, "source": "resume"}))
    with contextlib.redirect_stdout(io.StringIO()):
        start.main()
    sys.stdin = sys.__stdin__
    assert auto.reopened(s.stem)
    rows = len(read_rows(s))
    auto.roll_closed(s)
    assert len(read_rows(s)) == rows and not (box / f"{s.stem}.txt").exists()

    # the close hook reserves the roll before the worker exists: a reopen at once is still seen, a second close steps aside
    u = build(new_thread_path(d / "proj"), [("pizza", 60)])
    assert auto.reserve(u.stem) and not auto.reserve(u.stem)
    sys.stdin = io.StringIO(json.dumps({"session_id": u.stem, "source": "resume"}))
    with contextlib.redirect_stdout(io.StringIO()):
        start.main()
    sys.stdin = sys.__stdin__
    assert auto.reopened(u.stem)
    auto.pending(u.stem).unlink()

    # close, reopen, close again: the second close cannot reserve, and the reopen mark stands
    assert auto.reserve(u.stem)
    auto.reopened_mark(u.stem).write_text("", encoding="utf-8")
    assert not auto.reserve(u.stem) and auto.reopened(u.stem)
    auto.pending(u.stem).unlink()

    # a manual roll asks the same question: a fork carrying its parent's note has no note of its own
    x = build(new_thread_path(d / "proj"), [("pizza", 60)])
    add_row(x, "assistant", [{"type": "text", "text": note(x)}])
    xf = fork(x)
    assert roll.own_note(thread.Thread(x)) and roll.own_note(thread.Thread(xf)) is None

    # a failure after the swap keeps whatever a reopened thread wrote, and points at the backup
    y = build(new_thread_path(d / "proj"), [("pizza", 60)])
    real_check = roll.check

    def check_after_reopen(old, new, cut):
        add_row(y, "user", "written after the swap")
        return ["a forced failure"]
    roll.check = check_after_reopen
    try:
        ok, out = trim(y, 60)
    finally:
        roll.check = real_check
    assert not ok and "left as it is" in out and "archive files were kept" in out, out
    assert read_rows(y)[-1]["message"]["content"] == "written after the swap"
    kept = list(roll.ARCHIVE.glob("*/.raw/*.jsonl"))
    assert kept, "the moved-out part must survive in the archive, not only in the backup"
    shutil.rmtree(kept[0].parent.parent)

    # a note sharing its message with a tool call is not the last word
    z = build(new_thread_path(d / "proj"), [("pizza", 60)])
    add_row(z, "assistant", [{"type": "text", "text": note(z)},
                             {"type": "tool_use", "id": "toolu_same", "name": "Bash", "input": {"command": "ls"}}])
    assert roll.roll_note(thread.Thread(z)) is None

    # locks: a held lock is not taken; one left by a crash long ago is
    lock = box / "test.lock"
    assert auto.claim(lock, 0) and not auto.claim(lock, 0)
    os.utime(lock, (0, 0))
    assert auto.claim(lock, 0)


def test_open_scope():
    d = fresh()
    a = small([("pizza", 16), ("fruit", 16), ("tea", 12)], new_thread_path(d / "proj"))
    b = small([("salad", 16), ("soup", 16), ("bread", 12)], new_thread_path(d / "proj"), )
    assert trim(a, 60)[0] and trim(b, 60)[0]
    fa = next(f for f in family_folders() if a.stem[:8] in f.name)
    fb = next(f for f in family_folders() if b.stem[:8] in f.name)
    ident = re.search(r"id: (\S+)\n  salad talk 3\b", run_search("--all", "--from", "2000", "--limit", "50")).group(1)
    assert "salad talk 3" in run_search("--open", ident, "--family", fb.name)
    assert "No moved-out message" in run_search("--open", ident, "--family", fa.name)
    assert "No family folder" in run_search("pizza", "--family", "..")


def test_close_hook():
    import importlib
    sys.path.insert(0, str(ROOT / "hooks"))
    end = importlib.import_module("session_end")
    auto = importlib.import_module("auto_roll")
    start = importlib.import_module("session_start")
    d = fresh()
    projects = d / "projects"
    real_projects, thread.PROJECTS_DIR = thread.PROJECTS_DIR, projects
    try:
        p = build(new_thread_path(projects / "proj"), [("pizza", 10)])
        # only a .jsonl inside the projects folder is accepted
        assert end.transcript({"transcript_path": str(p)}) == p.resolve()
        assert end.transcript({"transcript_path": str(d / "elsewhere.jsonl")}) is None
        assert end.transcript({"transcript_path": str(projects / ".." / p.name)}) is None
        assert end.transcript({"transcript_path": 42}) is None and end.transcript({}) is None
        # a worker that cannot start is written down, and its reservation is let go
        real_start = end.start_detached

        def refuse(args):
            raise OSError("no process for you")
        end.start_detached = refuse
        sys.stdin = io.StringIO(json.dumps({"transcript_path": str(p)}))
        try:
            end.main()
        finally:
            end.start_detached, sys.stdin = real_start, sys.__stdin__
        assert "could not start" in (roll.ARCHIVE / "molt.log").read_text(encoding="utf-8")
        assert not auto.pending(p.stem).exists()
    finally:
        thread.PROJECTS_DIR = real_projects
    # a session id with anything after its hex is refused before it touches a path
    for bad in ("aaaaaaaa/../../x", "aaaaaaaa\n", "short"):
        sys.stdin = io.StringIO(json.dumps({"session_id": bad}))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            start.main()
        sys.stdin = sys.__stdin__
        assert out.getvalue() == ""
    # a damaged family record is skipped with a message, not a crash
    f = roll.ARCHIVE / "Broken (00000000)"
    f.mkdir(parents=True)
    (f / roll.FAMILY).write_text("{}", encoding="utf-8")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert roll.load_families() == []
    assert "damaged family record" in out.getvalue()


def test_page_is_offline():
    page = readable.to_html("Pizza chat", "sub", [{"who": "user", "name": "You", "when": "", "parts": [("say", "hi")]}])
    assert "DOMPurify" in page and "marked" in page
    for lib in ("marked.min.js", "purify.min.js"):                 # carried exactly as released
        assert (ROOT / "vendor" / lib).read_text(encoding="utf-8") in page, lib
    for fetch in ("<script src", "<link", "@import", "fonts.googleapis", "cdnjs.cloudflare"):
        assert fetch not in page, fetch                          # nothing that loads from elsewhere


def test_index_files():
    try:
        import numpy as np
    except ImportError:
        return
    d = fresh()
    p = small([("pizza", 16), ("fruit", 16), ("tea", 12)], new_thread_path(d / "proj"))
    assert trim(p, 60)[0]
    raw = search.pages_raw(family_folders()[0])[0]
    fake = lambda texts, query=False: np.ones((len(texts), 4), dtype="float32") / 2
    search.index_page(raw, embed=fake)
    count = len(search.chunks(search.messages(raw)))
    assert not list(raw.parent.glob("*.tmp")), "temporary files are swapped in, never left behind"
    assert search.load_vectors(raw, count) is not None
    with open(search.vector_file(raw), "wb") as f:              # a half-written index, as after a crash
        np.save(f, np.ones((count - 1, 4), dtype="float16"))
    assert search.load_vectors(raw, count) is None


def test_search_edges():
    # a search word must start a word
    assert search.cluster("what a party", ["art"])[0] == 0
    assert search.cluster("we rolled it", ["roll"])[0] == 1
    # a model that cannot load leaves search by word only, never a crash
    try:
        import fastembed  # noqa: F401
    except ImportError:
        return
    model, search.MODEL, search._model = search.MODEL, "no-such/model-for-molt-tests", None
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            assert REAL_EMBEDDER() is None
    finally:
        search.MODEL, search._model = model, None


def test_search_meaning():
    try:
        import numpy as np
    except ImportError:
        return                                   # numpy comes with fastembed; without it, search by meaning is off anyway
    import zlib

    def fake(texts, query=False):               # bag of words in 256 slots: same words, close vectors
        m = np.zeros((len(texts), 256), dtype="float32")
        for i, t in enumerate(texts):
            for w in re.findall(r"[a-z]+", t.lower()):
                m[i, zlib.crc32(w.encode()) % 256] += 1
        return m / np.maximum(np.linalg.norm(m, axis=1, keepdims=True), 1e-12)

    d = fresh()
    p = small([("pizza", 16), ("fruit", 16), ("tea", 12)], new_thread_path(d / "proj"))
    ok, out = trim(p, 60)
    assert ok, out
    folder, _ = only_family()
    raw = search.pages_raw(folder)[0]
    assert "Indexed" in search.index_page(raw, embed=fake)
    assert search.vector_file(raw).exists()
    out = search.search("pizza talk", folder, "0000-01-01", "9999-12-31", 8, True, embed=fake)
    assert "· both ·" in out and "closeness" in out, out
    meta = raw.with_name(raw.stem + ".vectors.json")
    meta.write_text(json.dumps({"model": "another-model", "chunks": 1}), encoding="utf-8")
    assert search.load_vectors(raw, 1) is None              # an index from another model is not trusted


def test_thin_only():
    d = fresh()
    p = build(new_thread_path(d / "proj"), [("pizza", 22)], result_chars=8000)
    before = read_rows(p)
    ok, out = trim(p, 100)
    assert ok, out
    assert "Moves out: nothing" in out, out
    folder, rec = only_family()
    assert rec["pages"] == [] and rec["threads"][p.stem]["parent"] is None
    assert not [f for f in folder.glob("*.html") if f.name != "index.html"], "a page was written"
    assert not raw_rows(folder), "rows were moved out"
    assert list((folder / ".raw").glob("*.thinned.jsonl")), "no originals saved"
    assert fps(read_rows(p)) == fps(before) and len(read_rows(p)) == len(before)
    ok, out = trim(p, 100)
    assert not ok and "has not been spoken in since" in out


def test_no_thin_and_practice():
    d = fresh()
    p = build(new_thread_path(d / "proj"), [("pizza", 30), ("fruit", 30)], result_chars=8000)
    original = p.read_bytes()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        roll.trim(thread.Thread(p), 100, go=False)
    out = buf.getvalue()
    for word in ("Shortens:", "tool outputs, saving about", "Keeps  :", "turns", "starts a new family", "Practice run"):
        assert word in out, f"{word!r} missing from the practice run:\n{out}"
    assert p.read_bytes() == original and not roll.ARCHIVE.exists(), "a practice run changed something"
    ok, out = trim(p, 100, thin=False)
    assert ok and not list((family_folders()[0] / ".raw").glob("*.thinned.jsonl")) and "[molt:" not in p.read_text(encoding="utf-8")


def test_whole_copy_and_fingerprint():
    d = fresh()
    p = small([("pizza", 5)], new_thread_path(d / "proj"))
    with contextlib.redirect_stdout(io.StringIO()):
        out = roll.copy_whole(thread.Thread(p))
    assert out.parent == roll.ARCHIVE / "Whole thread copies" and out.name.endswith("Pizza chat.html") and out.exists()
    assert not family_folders()
    q = fork(p)
    a, b = read_rows(p), read_rows(q)
    assert fps(a) == fps(b) and len(fps(a)) == 10
    assert [r["uuid"] for r in a if r.get("uuid")] != [r["uuid"] for r in b if r.get("uuid")]
    row = {"type": "user", "timestamp": "T", "message": {"role": "user", "content": "hi"}}
    import hashlib
    assert thread.fingerprint(row) == hashlib.sha1("T\nhi".encode("utf-8")).hexdigest()[:16]
    assert thread.fingerprint({"type": "assistant", "timestamp": "T", "message": {"content": [{"type": "tool_use", "id": "x"}]}}) is None


def test_speakers():
    say = lambda r: thread.Thread.speaker(None, r)
    queued = lambda p: {"type": "attachment", "attachment": {"type": "queued_command", "prompt": p}}
    typed = lambda t: {"type": "user", "message": {"role": "user", "content": t}}
    assert say(typed("pizza tonight?")) == ("user", "pizza tonight?")
    assert say(queued("and cheese")) == ("user", "and cheese")
    assert say(queued('<cross-session-message from="uds:x" from-name="pizza-foreman">Oven is hot.</cross-session-message>')) \
        == ("session", "pizza-foreman", "Oven is hot.")
    assert say(queued('<agent-message from="a5f962d1555695bf6">[Subagent hand-back] Dough checked.</agent-message>')) \
        == ("session", "subagent a5f962d1", "[Subagent hand-back] Dough checked.")
    assert say(typed('<agent-message from="b1">Report.</agent-message>')) == ("session", "subagent b1", "Report.")
    real, thread.HOME_SLUG = thread.HOME_SLUG, "sam-jones"
    try:
        sent = say(queued('<cross-session-message from="uds:x" from-name="sam-jones-10">hi</cross-session-message>'))
        assert sent == ("session", "home-10", "hi"), sent
        assert thread.sender("pizza-foreman") == "pizza-foreman"
    finally:
        thread.HOME_SLUG = real
    for harness in ("<task-notification>done</task-notification>", "<side-sessions-event>x</side-sessions-event>",
                    "<ci-monitor-event>x</ci-monitor-event>"):
        assert say(queued(harness)) is None and say(typed(harness)) is None


def test_names():
    d = fresh()
    assert readable.load_names(d / "missing.json") == ("You", "Claude")
    f = d / "names.json"
    f.write_text('{"user": "Sam", "claude": "Juniper"}', encoding="utf-8")
    assert readable.load_names(f) == ("Sam", "Juniper")
    f.write_text('{"user": ""}', encoding="utf-8")
    assert readable.load_names(f) == ("You", "Claude")
    f.write_text("not json", encoding="utf-8")
    with contextlib.redirect_stdout(io.StringIO()):
        assert readable.load_names(f) == ("You", "Claude")


def test_auto_roll():
    import importlib
    sys.path.insert(0, str(ROOT / "hooks"))
    auto = importlib.import_module("auto_roll")
    start = importlib.import_module("session_start")
    auto.SETTLE = 0
    d = fresh()

    # an ordinary close: no roll note, so nothing is rolled, logged or left for the next start
    plain = build(new_thread_path(d / "proj"), [("pizza", 60)])
    auto.roll_closed(plain)
    assert not family_folders() and not (roll.ARCHIVE / "molt.log").exists()

    # the roll note is Claude's newest message: the close is the yes
    p = build(new_thread_path(d / "proj"), [("pizza", 60)])
    add_row(p, "assistant", [{"type": "text", "text": roll.note_line(p.stem) + "\nOn waking: read this note."}])
    before = len(read_rows(p))
    auto.roll_closed(p)
    assert len(family_folders()) == 1 and len(read_rows(p)) < before
    assert "rolled" in (roll.ARCHIVE / "molt.log").read_text(encoding="utf-8")
    assert not (roll.ARCHIVE / ".auto-roll" / f"{p.stem}.txt").exists(), "a roll that worked leaves no message"

    # closing it again before anyone speaks rolls nothing more
    rows = len(read_rows(p))
    auto.roll_closed(p)
    assert len(read_rows(p)) == rows

    # a roll that stopped leaves a message: the next start of that thread hears it once, then never again
    auto.leave_message(p.stem, "Molt tried to roll this thread and stopped.")
    for expect in (True, False):
        sys.stdin, out = io.StringIO(json.dumps({"session_id": p.stem, "source": "resume"})), io.StringIO()
        with contextlib.redirect_stdout(out):
            start.main()
        assert ("tried to roll" in out.getvalue()) == expect
    sys.stdin = sys.__stdin__

    # a note followed by more talk is not the last word: no roll
    q = build(new_thread_path(d / "proj"), [("cheese", 60)])
    add_row(q, "assistant", [{"type": "text", "text": roll.note_line(q.stem)}])
    speak(q, label="one more thing")
    rows = len(read_rows(q))
    auto.roll_closed(q)
    assert len(read_rows(q)) == rows


TESTS = [test_plain_trim, test_thinning, test_guard, test_fork_after_trim, test_fork_before_trim, test_tree,
         test_failed_trim, test_search, test_thin_only, test_no_thin_and_practice, test_whole_copy_and_fingerprint,
         test_names, test_auto_roll, test_search_meaning, test_guards, test_open_scope,
         test_search_edges, test_close_hook, test_speakers, test_page_is_offline, test_index_files]


def main() -> int:
    failed = 0
    for fn in TESTS:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except Exception as e:                       # AssertionError and any crash both count as FAIL
            failed += 1
            print(f"FAIL  {fn.__name__}: {type(e).__name__}: {e}")
            print("      " + traceback.format_exc().strip().splitlines()[-3].strip())
    shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n{len(TESTS) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
