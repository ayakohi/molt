"""
search.py - search what molt moved out of a thread: by word, by meaning, and by date.

    python roll.py search "words" [--from 2026-11] [--to 2026-11-19] [--all] [--family "<folder>"] [--limit 8] [--debug]
    python roll.py search --from 2026-11-11 --to 2026-11-11       no words: browse that stretch in order
    python roll.py search --open <id>                              one message in full, with two on each side
    python roll.py index                                           make the meaning index for every page that lacks one

What is searched: the conversation on each moved-out page, as chunks of about 800 characters.
The user's words, other sessions' messages and Claude's words go in. Thinking and tool output stay out.

How a search runs:
  1. Two passes at once: chunks that CONTAIN the words, and chunks that MEAN them.
  2. The query is judged before any chunk: it has a home when a chunk holds 2+ of its words
     (1 for a one-word query) or sits at least ANCHOR close in meaning. No home: an honest empty.
  3. With a home, chunks that clear their floor are shown: strong matches first, then the rest,
     newest first inside each group, each as a ~500-character
     window aimed where the most search words cluster.
  4. The scope is the family of the thread that runs the search (CLAUDE_CODE_SESSION_ID), unless
     --all or --family says otherwise.

Meaning needs the optional package fastembed (pip install fastembed). Without it, search runs by
word only and says so. The model is downloaded once into ~/.cache/molt/models.
"""

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import readable as R   # noqa: E402
import roll            # noqa: E402
import thread as T     # noqa: E402

MODEL = "BAAI/bge-base-en-v1.5"          # the floors below are starting values for this model
MODEL_DIR = Path.home() / ".cache" / "molt" / "models"
CHUNK = 800              # characters per chunk
OVERLAP = 100            # characters shared by neighbouring chunks of one long message
WINDOW = 500             # characters shown per result
BROWSE_CLIP = 300        # characters shown per message when browsing by date
ANCHOR = 0.60            # closeness that proves the query has a home here
KEYWORD_FLOOR = 0.50     # once there is a home: a chunk holding a search word needs this closeness
SEMANTIC_FLOOR = 0.56    # once there is a home: a meaning-only chunk needs this closeness
STOP = set("the and for with that this from into your you are was but not her his she him they them our out "
           "has had have will would than then what when which who how why of to in on is it as at by or be".split())


# ---- chunks -----------------------------------------------------------------------

def messages(raw: Path) -> list:
    """The conversation in one moved-out raw file: [{n, ts, who, text}], Claude's consecutive text rows
    joined into one message. Thinking, tool calls, tool output and side threads are left out."""
    out = []
    for line in raw.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("isSidechain"):
            continue
        s = T.Thread.speaker(None, r)
        if s:
            who = R.USER if s[0] == "user" else s[1]
            out.append({"timestamp": r.get("timestamp") or "", "who": who, "text": s[-1]})
        elif r.get("type") == "assistant" and T.text_of(r).strip():
            if out and out[-1]["who"] == R.CLAUDE:
                out[-1]["text"] += "\n\n" + T.text_of(r)
            else:
                out.append({"timestamp": r.get("timestamp") or "", "who": R.CLAUDE, "text": T.text_of(r)})
    for n, m in enumerate(out):
        m["n"] = n
    return out


def chunks(msgs: list) -> list:
    """[{n, part, text}]: each message whole, or cut into overlapping CHUNK-sized pieces."""
    out = []
    for m in msgs:
        text, start, part = m["text"], 0, 0
        while True:
            out.append({"n": m["n"], "part": part, "text": text[start:start + CHUNK]})
            if start + CHUNK >= len(text):
                break
            start, part = start + CHUNK - OVERLAP, part + 1
    return out


# ---- the meaning index --------------------------------------------------------------

_model = None


def embedder():
    """A function texts -> unit vectors, or None when fastembed is not installed or its model cannot
    load (eg. offline before the first download). None means search runs by word only."""
    global _model
    try:
        from fastembed import TextEmbedding
        import numpy as np
    except ImportError:
        return None
    if _model is None:
        try:
            MODEL_DIR.mkdir(parents=True, exist_ok=True)
            _model = TextEmbedding(MODEL, cache_dir=str(MODEL_DIR))
        except Exception as e:                            # a missing or unreachable model costs meaning, never search
            print(f"The search model could not load ({type(e).__name__}); searching by word only.")
            return None

    def embed(texts: list, query: bool = False):
        vecs = list(_model.query_embed(texts) if query else _model.passage_embed(texts))
        m = np.array(vecs, dtype="float32")
        return m / np.maximum(np.linalg.norm(m, axis=1, keepdims=True), 1e-12)
    return embed


def vector_file(raw: Path) -> Path:
    return raw.with_name(raw.stem + ".vectors.npy")


def index_page(raw: Path, embed=None) -> str:
    """Write the meaning index beside one raw page. Returns a one-line result for the caller to print."""
    embed = embed or embedder()
    if embed is None:
        return "Search by meaning is off (pip install fastembed); this page is searchable by word."
    import numpy as np
    cs = chunks(messages(raw))
    vecs = embed([c["text"] for c in cs]) if cs else np.zeros((0, 768), dtype="float32")
    with open(vector_file(raw), "wb") as f:
        np.save(f, vecs.astype("float16"))
    meta = raw.with_name(raw.stem + ".vectors.json")
    meta.write_text(json.dumps({"model": MODEL, "chunks": len(cs)}), encoding="utf-8")
    return f"Indexed {len(cs)} chunks for search by meaning."


def index_all(embed=None) -> None:
    """Index every page that has no meaning index yet, or one made by another model."""
    todo = []
    for raw in pages_raw(None):
        meta = raw.with_name(raw.stem + ".vectors.json")
        try:
            ok = json.loads(meta.read_text(encoding="utf-8")).get("model") == MODEL and vector_file(raw).exists()
        except (OSError, ValueError):
            ok = False
        if not ok:
            todo.append(raw)
    if not todo:
        print("Every page is already indexed.")
        return
    for raw in todo:
        print(f"{raw.parent.parent.name} · {raw.stem}: {index_page(raw, embed)}")


def load_vectors(raw: Path, count: int):
    """The page's vectors when they exist, match this model and match the chunk count; else None."""
    try:
        meta = json.loads(raw.with_name(raw.stem + ".vectors.json").read_text(encoding="utf-8"))
        if meta.get("model") != MODEL or meta.get("chunks") != count:
            return None
        import numpy as np
        return np.load(vector_file(raw)).astype("float32")
    except (OSError, ValueError, ImportError):
        return None


# ---- scope -----------------------------------------------------------------------

def family_of(thread_id: str):
    """The family folder whose record lists this thread, or None."""
    for folder, rec in roll.load_families():
        if thread_id in rec["threads"]:
            return folder
    return None


def pages_raw(folder) -> list:
    """Raw page files, of one family folder or of the whole archive. Shortened tool output is not a page."""
    pattern = "*.jsonl" if folder else "*/.raw/*.jsonl"
    base = (folder / ".raw") if folder else roll.ARCHIVE
    if not base.exists():
        return []
    return sorted(p for p in base.glob(pattern) if not p.name.endswith(".thinned.jsonl"))


def page_label(raw: Path) -> str:
    """'pizza night (2af145ca) · page 2': the family's name and the page's place among that family's pages."""
    family = raw.parent.parent
    ordered = [p.stem for p in pages_raw(family)]
    return f"{family.name} · page {ordered.index(raw.stem) + 1}"


# ---- text helpers ----------------------------------------------------------------

def words_of(query: str) -> list:
    return list(dict.fromkeys(w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) >= 2 and w not in STOP))


def cluster(text: str, words: list):
    """(distinct search words in the densest WINDOW-sized stretch, centre of that stretch)."""
    low = text.lower()
    # a search word must start a word: "art" is not in "party", while "roll" is in "rolled"
    occ = sorted((m.start(), w) for w in words for m in re.finditer(r"(?<![a-z0-9])" + re.escape(w), low))
    best = (0, occ[0][0] if occ else 0)
    for s in range(len(occ)):
        seen, last = set(), occ[s][0]
        for e in range(s, len(occ)):
            if occ[e][0] - occ[s][0] > WINDOW:
                break
            seen.add(occ[e][1])
            last = occ[e][0]
        if len(seen) > best[0]:
            best = (len(seen), (occ[s][0] + last) // 2)
    return best


def window(text: str, centre: int) -> str:
    a = max(0, min(centre - WINDOW // 2, len(text) - WINDOW))
    piece = text[a:a + WINDOW].strip()
    return ("…" if a > 0 else "") + piece + ("…" if a + WINDOW < len(text) else "")


def local_day(ts: str) -> str:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d")
    except ValueError:
        return ""


def bound(raw, end: bool) -> str:
    """'2026', '2026-11' or '2026-11-19' as the first or last day it covers; blank is open."""
    m = re.match(r"^(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?", (raw or "").strip())
    if not m:
        return "9999-12-31" if end else "0000-01-01"
    return f"{m[1]}-{m[2] or ('12' if end else '01')}-{m[3] or ('31' if end else '01')}"


def short_id(raw: Path, n: int) -> str:
    return f"{raw.parent.parent.name[-9:-1]}.{raw.stem.replace('-', '').replace(' ', '').replace('.', '')}.{n}"


# ---- search ----------------------------------------------------------------------

def search(query: str, folder, frm: str, to: str, limit: int, debug: bool, embed=None) -> str:
    words = words_of(query)
    embed = embed if embed is not None else embedder()
    qvec = embed([query], query=True)[0] if embed else None
    cands = []
    for raw in pages_raw(folder):
        msgs = messages(raw)
        cs = chunks(msgs)
        vecs = load_vectors(raw, len(cs)) if qvec is not None else None
        for i, c in enumerate(cs):
            m = msgs[c["n"]]
            day = local_day(m["timestamp"])
            if not (frm <= day <= to):
                continue
            kw, centre = cluster(c["text"], words)
            close = float(vecs[i] @ qvec) if vecs is not None else 0.0
            if kw or close:
                cands.append(dict(raw=raw, m=m, text=c["text"], kw=kw, centre=centre, close=close))
    need = min(2, max(1, len(words)))
    strong = lambda c: c["kw"] >= need or c["close"] >= ANCHOR
    anchored = any(strong(c) for c in cands)
    shown = [c for c in cands if anchored and (strong(c) or
                                               (c["close"] >= KEYWORD_FLOOR if c["kw"] else c["close"] >= SEMANTIC_FLOOR))]
    best = {}                                             # one result per message: its best chunk
    for c in shown:
        key = (c["raw"], c["m"]["n"])
        if key not in best or (c["kw"], c["close"]) > (best[key]["kw"], best[key]["close"]):
            best[key] = c
    # strong matches first, then the ones that rode along; newest first inside each group
    hits = sorted(best.values(), key=lambda c: (strong(c), c["m"]["timestamp"]), reverse=True)
    mode = "" if qvec is not None else "\n_Search by meaning is off (pip install fastembed); this is by word only._"
    if not hits:
        out = f"Nothing moved out matches “{query}”. An honest empty: searched by word and by meaning.{mode}"
        if debug and cands:
            faint = sorted(cands, key=lambda c: -c["close"])[:3]
            out += "\n\nFaintest nearby:\n" + "\n".join(
                f"· {page_label(c['raw'])} · {R.clock(c['m'])} · closeness {c['close']:.2f} · words {c['kw']}" for c in faint)
        return out
    lines = [f"{len(hits)} matches for “{query}”, newest first." +
             (f" Showing {limit}; add words or dates to narrow it." if len(hits) > limit else "") + mode]
    for c in hits[:limit]:
        how = "both" if c["kw"] and c["close"] >= SEMANTIC_FLOOR else "words" if c["kw"] else "meaning"
        extra = f" · closeness {c['close']:.2f} · words {c['kw']}" if debug else ""
        text = window(c["text"], c["centre"]) if c["kw"] else window(c["text"], 0)
        lines.append(f"\n{page_label(c['raw'])} · {R.clock(c['m'])} · {c['m']['who']} · {how}{extra}"
                     f" · id: {short_id(c['raw'], c['m']['n'])}\n  {text}")
    return "\n".join(lines)


def browse(folder, frm: str, to: str, limit: int) -> str:
    rows = []
    for raw in pages_raw(folder):
        for m in messages(raw):
            if frm <= local_day(m["timestamp"]) <= to:
                rows.append((m["timestamp"], raw, m))
    if not rows:
        return f"Nothing moved out between {frm} and {to}."
    rows.sort(key=lambda x: x[0])
    head = f"{len(rows)} messages between {frm} and {to}, oldest first." + (
        f" Showing the first {limit}; narrow the dates to see the rest." if len(rows) > limit else "")
    body = [f"\n{page_label(raw)} · {R.clock(m)} · {m['who']} · id: {short_id(raw, m['n'])}\n  "
            f"{m['text'][:BROWSE_CLIP].strip()}{'…' if len(m['text']) > BROWSE_CLIP else ''}" for _, raw, m in rows[:limit]]
    return head + "\n".join(body)


def open_message(ident: str, folder) -> str:
    """One message by its id, with two on each side, from inside the search scope only."""
    for raw in pages_raw(folder):
        msgs = messages(raw)
        for m in msgs:
            if short_id(raw, m["n"]) == ident:
                around = msgs[max(0, m["n"] - 2):m["n"] + 3]
                return f"{page_label(raw)}\n" + "\n".join(
                    f"\n{'▶ ' if x is m else ''}{R.clock(x)} · {x['who']}\n{x['text']}" for x in around)
    return f"No moved-out message has the id {ident}."


# ---- the command line ----------------------------------------------------------------

def scope(args: dict):
    """(folder or None for everything, a sentence saying which, or an error sentence)."""
    if args.get("all"):
        return None, "the whole archive", None
    if args.get("family"):
        name = args["family"]
        f = roll.ARCHIVE / name
        ok = Path(name).name == name and name not in (".", "..") and (f / roll.FAMILY).exists()
        return (f, name, None) if ok else (None, "", f"No family folder called {name!r} in the archive.")
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not sid:
        return None, "the whole archive", None
    f = family_of(sid)
    if f is None:
        return None, "", "This thread has nothing moved out yet. Add --all to search the whole archive."
    return f, f.name, None


def parse(argv: list) -> dict:
    out, words, i = {"limit": 8}, [], 0
    while i < len(argv):
        a = argv[i]
        if a in ("--from", "--to", "--family", "--open", "--limit") and i + 1 < len(argv):
            out[a[2:]] = argv[i + 1]
            i += 2
            continue
        if a in ("--all", "--debug"):
            out[a[2:]] = True
        else:
            words.append(a)
        i += 1
    out["query"] = " ".join(words).strip()
    try:
        out["limit"] = max(1, min(int(out["limit"]), 50))
    except ValueError:
        out["limit"] = 8
    return out


def main(argv: list) -> None:
    args = parse(argv)
    folder, where, err = scope(args)
    if err:
        print(err)
        return
    if args.get("open"):
        print(open_message(args["open"], folder))
        return
    frm, to = bound(args.get("from"), False), bound(args.get("to"), True)
    if not args["query"]:
        if args.get("from") or args.get("to"):
            print(f"Searching {where}.\n" + browse(folder, frm, to, max(args["limit"], 20)))
        else:
            print('Give words to search, or --from / --to to browse. eg. roll.py search "pizza toppings" --from 2026-11')
        return
    print(f"Searching {where}.\n" + search(args["query"], folder, frm, to, args["limit"], bool(args.get("debug"))))
