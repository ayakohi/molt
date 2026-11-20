"""
readable.py - turn part of a thread into a page a person can read, as .html.

Only the conversation: the user's words, other sessions' messages, Claude's words, its thinking
where it was saved, and one quiet line per tool use. No tool output, no bookkeeping.
"""

import html
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import thread as T


# ---- speaker names ---------------------------------------------------------------

def load_names(path: Path) -> tuple[str, str]:
    """(user, claude) labels for the pages and search results. names.json beside this file
    overrides the defaults, eg. {"user": "Sam", "claude": "Juniper"}. It stays out of git."""
    user, claude = "You", "Claude"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return user, claude
    except (OSError, ValueError) as e:
        print(f"names.json could not be read ({e}); using {user!r} and {claude!r}.")
        return user, claude
    if isinstance(data, dict):
        user = str(data.get("user") or user).strip() or user
        claude = str(data.get("claude") or claude).strip() or claude
    return user, claude


USER, CLAUDE = load_names(Path(__file__).resolve().parent / "names.json")

# One quiet line per tool use. Tools not listed show their short name.
VERBS = {
    "Read": ("read", "file_path"), "Write": ("wrote", "file_path"), "Edit": ("edited", "file_path"),
    "NotebookEdit": ("edited", "notebook_path"), "Bash": ("ran", "description"),
    "PowerShell": ("ran", "description"), "Grep": ("searched for", "pattern"),
    "Glob": ("looked for", "pattern"), "Agent": ("sent a helper", "description"),
    "Skill": ("opened skill", "skill"), "WebSearch": ("searched the web for", "query"),
    "WebFetch": ("opened", "url"), "SendMessage": ("messaged", "to"),
}


def tool_line(b: dict) -> str:
    name = b.get("name") or "tool"
    args = b.get("input") or {}
    if name in VERBS:
        verb, key = VERBS[name]
        target = str(args.get(key) or "").replace("\\", "/")
        if key.endswith("_path"):
            target = target.rsplit("/", 1)[-1]
        return f"{verb} {target}".strip()
    return name.split("__")[-1].replace("_", " ")


def clock(r: dict) -> str:
    ts = r.get("timestamp")
    if not ts:
        return ""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%d %b %H:%M")
    except ValueError:
        return ""


def turns(t: "T.Thread", rows: list[int]) -> list[dict]:
    """Group rows into speaking turns: {who, name, when, parts:[(kind, text)]}.
    kind: 'say' | 'think' | 'tool' | 'image'. Consecutive Claude rows merge into one turn."""
    out = []
    for i in rows:
        r = t.rows[i]
        s = t.speaker(r)
        if s:
            who = s[0]
            name = USER if who == "user" else s[1]
            text = s[1] if who == "user" else s[2]
            parts = [("say", text)]
            parts += [("image", "")] * sum(1 for b in T.blocks(r) if b.get("type") == "image")
            out.append({"who": who, "name": name, "when": clock(r), "parts": parts})
            continue
        if r.get("type") != "assistant":
            continue
        parts = []
        for b in T.blocks(r):
            if b.get("type") == "text" and b.get("text", "").strip():
                parts.append(("say", b["text"]))
            elif b.get("type") == "thinking" and (b.get("thinking") or "").strip():
                parts.append(("think", b["thinking"]))
            elif b.get("type") == "tool_use":
                parts.append(("tool", tool_line(b)))
        if not parts:
            continue
        if out and out[-1]["who"] == "claude":
            out[-1]["parts"] += parts
        else:
            out.append({"who": "claude", "name": CLAUDE, "when": clock(r), "parts": parts})
    return out


# ---- HTML ----------------------------------------------------------------------

CSS = """:root{--bg:#EDE6E5;--card:#f6f1f0;--ink:#3d3837;--soft:#6E6968;--line:#d8cfcd;--user:#c46f5a;--claude:#5f7fb8;--session:#8a7a9e}
@media (prefers-color-scheme:dark){:root{--bg:#17161a;--card:#201f24;--ink:#e6e1e0;--soft:#9a9497;--line:#2f2d34;--user:#f0a387;--claude:#8aa9de;--session:#b9a6d4}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 "Instrument Sans",system-ui,sans-serif}
main{max-width:760px;margin:0 auto;padding:40px 16px 80px}
header{margin-bottom:28px}
h1{font:600 26px/1.25 "Instrument Sans",sans-serif;margin:0 0 6px}
.sub{color:var(--soft);font-size:13px}
.turn{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px 18px;margin:12px 0}
.who{font-size:12px;letter-spacing:.06em;text-transform:uppercase;font-weight:600;margin-bottom:6px;display:flex;gap:10px;align-items:baseline}
.who time{font-weight:400;letter-spacing:0;text-transform:none;color:var(--soft)}
.user .who{color:var(--user)} .claude .who{color:var(--claude)} .session .who{color:var(--session)}
.user .say{font-family:"SUSE",sans-serif}
.claude .say,.session .say{font-family:"Spectral",Georgia,serif;font-size:16.5px;line-height:1.7}
.user{border-left:3px solid var(--user)} .claude{border-left:3px solid var(--claude)} .session{border-left:3px solid var(--session)}
.say p{margin:.45em 0} .say :first-child{margin-top:0} .say :last-child{margin-bottom:0}
.say pre,.say code{font-family:"JetBrains Mono",monospace;font-size:12.5px}
.say pre{background:var(--bg);padding:10px 12px;border-radius:8px;overflow-x:auto}
.say table{border-collapse:collapse;font-family:"Instrument Sans",sans-serif;font-size:13.5px;display:block;overflow-x:auto}
.say td,.say th{border:1px solid var(--line);padding:4px 8px;text-align:left}
.say.raw{white-space:pre-wrap}
.tool{color:var(--soft);font-size:12.5px;font-style:italic;margin:4px 0}
.tool::before{content:"· "}
details.think{margin:6px 0;color:var(--soft);font-size:13.5px}
details.think summary{cursor:pointer;font-size:12px;letter-spacing:.05em}
details.think div{white-space:pre-wrap;border-left:2px solid var(--line);padding-left:10px;margin-top:6px}
.links{margin-top:4px} .links a{color:var(--claude)}
ul.tree,ul.tree ul{list-style:none;margin:0;padding-left:20px;border-left:1px solid var(--line)} ul.tree{padding-left:0;border:0}
ul.tree li{margin:8px 0} ul.tree a{color:var(--ink);text-decoration:none} ul.tree a:hover{text-decoration:underline}
.cont{color:var(--soft);font-size:12.5px;font-style:italic}
h2{font:600 15px/1.3 "Instrument Sans",sans-serif;margin:28px 0 8px;color:var(--soft)}
"""

HEAD = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
__CSS__</style></head>
"""

PAGE = HEAD + """<body><main>
<header><h1>__TITLE__</h1><div class="sub">__SUBTITLE__</div>__LINKS__</header>
<div id="log"></div>
</main>
<script id="data" type="application/json">__DATA__</script>
__LIBS__
<script>
const turns = JSON.parse(document.getElementById('data').textContent);
const md = (s) => (window.marked && window.DOMPurify) ? DOMPurify.sanitize(marked.parse(s)) : null;
const log = document.getElementById('log');
for (const t of turns) {
  const box = document.createElement('section'); box.className = 'turn ' + t.who;
  const who = document.createElement('div'); who.className = 'who';
  who.textContent = t.name;
  const tm = document.createElement('time'); tm.textContent = t.when; who.append(tm); box.append(who);
  for (const [kind, text] of t.parts) {
    if (kind === 'say') {
      const d = document.createElement('div'); d.className = 'say';
      const h = md(text); if (h !== null) d.innerHTML = h; else { d.classList.add('raw'); d.textContent = text; }
      box.append(d);
    } else if (kind === 'think') {
      const d = document.createElement('details'); d.className = 'think';
      const s = document.createElement('summary'); s.textContent = 'thinking';
      const b = document.createElement('div'); b.textContent = text; d.append(s, b); box.append(d);
    } else {
      const d = document.createElement('div'); d.className = 'tool';
      d.textContent = kind === 'image' ? '[image]' : text; box.append(d);
    }
  }
  log.append(box);
}
</script>
</body></html>
"""


def link_line(prev, tree) -> str:
    """The line under the subtitle: a link to the earlier page and one to the family tree.
    prev is a page record from .family.json (file, title, first, last); tree is a file name."""
    bits = []
    if prev:
        bits.append(f'<a href="{html.escape(quote(prev["file"]), quote=True)}">'
                    f'← earlier: {html.escape(prev["title"])} · {html.escape(prev["first"])} to {html.escape(prev["last"])}</a>')
    if tree:
        bits.append(f'<a href="{html.escape(quote(tree), quote=True)}">family tree</a>')
    return '<div class="sub links">' + " · ".join(bits) + "</div>" if bits else ""


# The page's two helpers travel inside every page, so a page needs nothing from the internet and still
# reads the same in ten years: marked turns Markdown into HTML, DOMPurify cleans that HTML before it is
# shown. Pinned copies with their license headers live in vendor/ (see vendor/README.md).
VENDOR = Path(__file__).resolve().parent / "vendor"


def libs() -> str:
    """Both helpers as inline scripts, or "" when vendor/ is missing; the page then shows plain text."""
    try:
        code = [(VENDOR / name).read_text(encoding="utf-8") for name in ("marked.min.js", "purify.min.js")]
    except OSError:
        return ""
    # only "</script" can end the tag early; neither library holds it, and its every "</" stays as written
    safe = [re.sub(r"</(script)", r"<\\/\1", c, flags=re.I) for c in code]
    return "\n".join("<script>\n" + c + "\n</script>" for c in safe)


def to_html(title: str, subtitle: str, ts: list[dict], prev=None, tree=None) -> str:
    data = json.dumps(ts, ensure_ascii=False).replace("</", "<\\/")
    return (PAGE.replace("__LIBS__", libs()).replace("__CSS__", CSS)
                .replace("__TITLE__", html.escape(title))
                .replace("__SUBTITLE__", html.escape(subtitle))
                .replace("__LINKS__", link_line(prev, tree))
                .replace("__DATA__", data))


# ---- the family tree page ------------------------------------------------------

INDEX = HEAD + """<body><main>
<header><h1>__TITLE__</h1><div class="sub">__SUBTITLE__</div></header>
__LIST__
</main></body></html>
"""


def family_tree(rec: dict):
    """(kids, under, loose) for one family, shared by the HTML and Markdown tree pages.
    kids: {page stamp or None for the roots: [pages continuing from it]}; under: {page stamp:
    [titles of Claude Code threads carrying on from it]}; loose: thread titles with nothing moved out."""
    pages, threads = rec["pages"], rec["threads"]
    stamps = {p["stamp"] for p in pages}
    kids = {}
    for p in pages:
        kids.setdefault(p["prev"] if p["prev"] in stamps else None, []).append(p)
    newest = {p["thread"]: p["stamp"] for p in pages}
    under, loose = {}, []
    for tid, th in threads.items():
        at = newest.get(tid) or th.get("parent")
        if at in stamps:
            under.setdefault(at, []).append(th.get("title") or "Untitled")
        else:
            loose.append(th.get("title") or "Untitled")
    return kids, under, loose


def to_index(rec: dict) -> str:
    """index.html for one family: pages nested under the page they continue from, and under each
    page the Claude Code threads that carry on from it. rec is the family's .family.json."""
    pages, threads = rec["pages"], rec["threads"]
    kids, under, loose = family_tree(rec)

    def node(p: dict) -> str:
        label = f'{p["title"]} · {p["first"]} to {p["last"]} · {p["turns"]} turns'
        out = f'<li><a href="{html.escape(quote(p["file"]), quote=True)}">{html.escape(label)}</a>'
        out += "".join(f'<div class="cont">continues in Claude Code: {html.escape(x)}</div>' for x in under.get(p["stamp"], []))
        if kids.get(p["stamp"]):
            out += "<ul>" + "".join(node(c) for c in kids[p["stamp"]]) + "</ul>"
        return out + "</li>"

    body = '<ul class="tree">' + "".join(node(p) for p in kids.get(None, [])) + "</ul>"
    if loose:
        body += '<h2>Threads with nothing moved out yet</h2><ul class="tree">' + "".join(
            f'<li><div class="cont">continues in Claude Code: {html.escape(x)}</div></li>' for x in loose) + "</ul>"
    sub = f"Family tree · {len(pages)} pages · {len(threads)} threads"
    return (INDEX.replace("__CSS__", CSS).replace("__TITLE__", html.escape(rec.get("name") or "Untitled"))
                 .replace("__SUBTITLE__", html.escape(sub)).replace("__LIST__", body))


# ---- Markdown pages, for notes apps such as Obsidian ---------------------------
# Written when MOLT_PAGES is markdown or both. Each page opens with YAML front matter, so a notes app can
# sort and filter pages, and links use plain relative Markdown links, so they resolve inside one family
# folder however many families the vault holds.

def md_name(html_file: str) -> str:
    """The Markdown page's file name: the page record keeps the .html name, and both share one stem."""
    return html_file[:-5] + ".md" if html_file.endswith(".html") else html_file + ".md"


def md_link(text: str, target: str) -> str:
    return f"[{text.replace('[', '(').replace(']', ')')}]({quote(target)})"


def md_value(text) -> str:
    """A front-matter value that YAML reads back as the same text."""
    return json.dumps(str(text), ensure_ascii=False)


def md_quote(text: str) -> str:
    return "\n".join("> " + line if line else ">" for line in text.splitlines())


def to_md(title: str, subtitle: str, ts: list[dict], prev=None, tree=None, page=None) -> str:
    """One Markdown page. page is the record from .family.json (stamp, first, last, turns) when the
    page belongs to a family; a whole-thread copy has none."""
    front = ["---", f"title: {md_value(title)}", "molt: page"]
    if page:
        m = re.match(r"(\d{4}-\d{2}-\d{2}) (\d{2})\.(\d{2})", page["stamp"])
        if m:
            front.append(f"moved_out: {m.group(1)}T{m.group(2)}:{m.group(3)}")
        front += [f"conversation: {md_value(page['first'] + ' → ' + page['last'])}", f"turns: {page['turns']}"]
    front += ["tags: [molt]", "---", ""]
    lines = front + [f"# {title}", "", f"*{subtitle}*", ""]
    links = []
    if prev:
        links.append(md_link(f"← earlier: {prev['title']} · {prev['first']} to {prev['last']}", md_name(prev["file"])))
    if tree:
        links.append(md_link("family tree", tree))
    if links:
        lines += [" · ".join(links), ""]
    for turn in ts:
        lines += [f"### {turn['name']} · {turn['when']}".rstrip(" ·"), ""]
        for kind, text in turn["parts"]:
            if kind == "say":
                lines += [text, ""]
            elif kind == "think":
                lines += ["> [!quote]- thinking", md_quote(text), ""]
            elif kind == "tool":
                lines += ["· `" + text.replace("`", "'") + "`", ""]
            elif kind == "image":
                lines += ["*[image]*", ""]
    return "\n".join(lines)


def to_index_md(rec: dict) -> str:
    """index.md for one family: the same tree as index.html, as a nested Markdown list."""
    pages, threads = rec["pages"], rec["threads"]
    kids, under, loose = family_tree(rec)
    name = rec.get("name") or "Untitled"
    lines = ["---", f"title: {md_value(name)}", "molt: family", f"pages: {len(pages)}", f"threads: {len(threads)}",
             "tags: [molt]", "---", "", f"# {name}", "", f"*Family tree · {len(pages)} pages · {len(threads)} threads*", ""]

    def node(p: dict, depth: int) -> None:
        pad = "  " * depth
        lines.append(pad + "- " + md_link(f"{p['title']} · {p['first']} to {p['last']} · {p['turns']} turns", md_name(p["file"])))
        lines.extend(f"{pad}  - *continues in Claude Code: {x}*" for x in under.get(p["stamp"], []))
        for c in kids.get(p["stamp"], []):
            node(c, depth + 1)

    for p in kids.get(None, []):
        node(p, 0)
    if loose:
        lines += ["", "## Threads with nothing moved out yet", ""] + [f"- *continues in Claude Code: {x}*" for x in loose]
    return "\n".join(lines) + "\n"
