# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 CueCarry contributors
"""
make_code_tour.py - builds docs/code-tour.html from the section headers in the code.

Each source file is split at its numbered banners:

    # ======================
    # 3. Title
    # What:  ...
    # Why:   ...
    # If it goes wrong: ...
    # ======================

and the page shows each section's explanation beside the code it explains,
so the explanation can be checked against the code line by line.

Run:  python tools/make_code_tour.py      (no packages needed)
"""
import html
import io
import os
import re
import tokenize

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
FILES = [
    ("app/__main__.py", "Entry point"),
    ("app/core.py", "Engine: XML, plan, cue shift"),
    ("app/encoder.py", "ffmpeg: find, probe, encode"),
    ("app/gui.py", "The window"),
    ("app/cli.py", "Command line"),
    ("packaging/cuecarry.spec", "Packaging recipe"),
]

BANNER = re.compile(r"^\s*# =+\s*$")
TAGS = {"What": "what", "Why": "why", "If it goes wrong": "wrong"}
FIELD = re.compile(r"^(What|Why|If it goes wrong):\s*(.*)$")


def parse_sections(lines):
    """Returns (intro_lines, [(number_title, fields, start_line, code_lines)])."""
    sections, i, intro_end = [], 0, None
    while i < len(lines):
        if BANNER.match(lines[i]) and i + 1 < len(lines) and re.match(r"^\s*# \d+\.", lines[i + 1]):
            if intro_end is None:
                intro_end = i
            title = lines[i + 1].strip()[2:]
            j, body = i + 2, []
            while j < len(lines) and not BANNER.match(lines[j]):
                body.append(re.sub(r"^\s*# ?", "", lines[j]))
                j += 1
            fields, key = {}, None
            for b in body:
                m = FIELD.match(b)
                if m:
                    key = m.group(1)
                    fields[key] = m.group(2)
                elif key and b.strip():
                    fields[key] += " " + b.strip()
            sections.append([title, fields, j + 2, []])
            i = j + 1
            continue
        if sections:
            sections[-1][3].append(lines[i])
        i += 1
    if intro_end is None:
        intro_end = len(lines)
    return lines[:intro_end], sections


CLASS = {tokenize.COMMENT: "c", tokenize.STRING: "s", tokenize.NUMBER: "n"}
KEYWORDS = set("""def class return if elif else for while in not and or is None True False
import from as with try except finally raise lambda yield pass break continue global
nonlocal assert del async await""".split())


def highlight(code: str) -> str:
    """Python syntax colors, using Python's own tokenizer."""
    out, pos = [], 0
    lines = code.splitlines(keepends=True)
    offsets = [0]
    for ln in lines:
        offsets.append(offsets[-1] + len(ln))
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(code).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return html.escape(code)
    for t in toks:
        if t.type == tokenize.ENDMARKER:
            break
        start = offsets[t.start[0] - 1] + t.start[1]
        end = offsets[t.end[0] - 1] + t.end[1]
        if start < pos:
            continue
        out.append(html.escape(code[pos:start]))
        text = html.escape(code[start:end])
        cls = CLASS.get(t.type)
        if t.type == tokenize.NAME and t.string in KEYWORDS:
            cls = "k"
        if t.type == tokenize.STRING and code[start:end].startswith(('"""', "'''")):
            cls = "d"
        out.append(f'<span class="{cls}">{text}</span>' if cls else text)
        pos = end
    out.append(html.escape(code[pos:]))
    return "".join(out)


def code_block(code_lines, first_line):
    code = "\n".join(code_lines).rstrip("\n")
    hl = highlight(code + "\n").rstrip("\n").split("\n")
    rows = "".join(f'<span class="row"><span class="ln">{first_line + k}</span>{line}\n</span>'
                   for k, line in enumerate(hl))
    return f"<pre>{rows}</pre>"


def main():
    nav, body = [], []
    for rel, label in FILES:
        path = os.path.join(ROOT, rel)
        with open(path, encoding="utf-8") as f:
            lines = f.read().split("\n")
        intro, sections = parse_sections(lines)
        fid = re.sub(r"\W", "-", rel)
        nav.append(f'<li><a href="#{fid}"><b>{html.escape(rel)}</b></a> <span class="dim">{label}</span><ul>')
        intro_text = "\n".join(intro).strip()
        doc = re.search(r'"""(.*?)"""', intro_text, re.S)
        doc_text = doc.group(1).strip() if doc else ""
        body.append(f'<section class="file" id="{fid}"><h2>{html.escape(rel)}</h2>'
                    f'<p class="dim">{label}</p>')
        if doc_text and sections:
            body.append(f'<div class="intro"><pre class="doc">{html.escape(doc_text)}</pre></div>')
        if not sections:
            body.append(f'<div class="whole">{code_block(lines, 1)}</div>')
        for title, fields, start, code in sections:
            sid = fid + "-" + re.sub(r"\W", "-", title.split(".")[0])
            nav.append(f'<li><a href="#{sid}">{html.escape(title)}</a></li>')
            expl = "".join(
                f'<p><span class="tag {TAGS[k]}">{k}</span> {html.escape(v)}</p>'
                for k, v in fields.items())
            body.append(f'<div class="sec" id="{sid}"><div class="expl"><h3>{html.escape(title)}</h3>'
                        f'{expl}</div><div class="code">{code_block(code, start)}</div></div>')
        nav.append("</ul></li>")
        body.append("</section>")

    page = TEMPLATE.replace("{{NAV}}", "".join(nav)).replace("{{BODY}}", "".join(body))
    out = os.path.join(ROOT, "docs", "code-tour.html")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(page)
    print("wrote", out)


TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Code Tour</title>
<style>
:root { --bg:#0f1115; --panel:#171a21; --line:#262b36; --text:#e6e8ee; --dim:#8b93a7;
        --what:#4cc38a; --why:#5b9cf5; --wrong:#f0a04b; --k:#c792ea; --s:#c3e88d;
        --c:#7f8aa3; --n:#f78c6c; --d:#a3b8a0; }
@media (prefers-color-scheme: light) { :root:not([data-theme="dark"]) {
  --bg:#f7f7f9; --panel:#fff; --line:#e3e5ea; --text:#1b1e24; --dim:#5f6677;
  --k:#7c3aed; --s:#2f7d32; --c:#6b7280; --n:#c2410c; --d:#4b6b48; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--text);
       font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
.wrap { display:grid; grid-template-columns: 300px 1fr; }
nav { position:sticky; top:0; height:100vh; overflow:auto; padding:16px;
      border-right:1px solid var(--line); background:var(--panel); }
nav ul { list-style:none; padding-left:12px; margin:4px 0 12px; }
nav > ul { padding-left:0; }
nav a { color:var(--text); text-decoration:none; } nav a:hover { text-decoration:underline; }
main { padding:16px 24px; min-width:0; }
h1 { margin:0 0 4px; } .dim { color:var(--dim); }
.file { margin-bottom:40px; }
.sec { display:grid; grid-template-columns: minmax(260px, 34%) 1fr; gap:16px;
       border-top:1px solid var(--line); padding:14px 0; }
.expl { position:sticky; top:12px; align-self:start; }
.expl h3 { margin:0 0 8px; font-size:16px; }
.expl p { margin:0 0 8px; }
.tag { display:inline-block; font-size:12px; font-weight:600; padding:1px 7px;
       border-radius:10px; color:#0f1115; margin-right:4px; }
.tag.what { background:var(--what); } .tag.why { background:var(--why); }
.tag.wrong { background:var(--wrong); }
.code, .intro { min-width:0; }
pre { margin:0; padding:10px 12px; background:var(--panel); border:1px solid var(--line);
      border-radius:8px; white-space:pre-wrap; word-break:break-word;
      font: 13px/1.45 ui-monospace, "Cascadia Code", Consolas, monospace; }
pre.doc { font-family: inherit; font-size:14px; }
.row { display:block; padding-left:4.6em; text-indent:-4.6em; }
.ln { display:inline-block; width:3.6em; margin-right:1em; text-indent:0; color:var(--dim);
      user-select:none; text-align:right; }
.whole { border-top:1px solid var(--line); padding-top:14px; }
.k { color:var(--k); } .s { color:var(--s); } .c { color:var(--c); font-style:italic; }
.n { color:var(--n); } .d { color:var(--d); }
@media (max-width: 900px) { .wrap { grid-template-columns: 1fr; } nav { position:static; height:auto; }
  .sec { grid-template-columns: 1fr; } }
</style></head><body><div class="wrap">
<nav><h3 style="margin-top:0">Code tour</h3>
<p class="dim">Each section: <span class="tag what">What</span>
<span class="tag why">Why</span> <span class="tag wrong">If it goes wrong</span></p><ul>{{NAV}}</ul></nav>
<main><h1>Code tour</h1>
<p class="dim">Generated from the section headers in the code by <code>tools/make_code_tour.py</code>.
Line numbers match the files.</p>
{{BODY}}</main></div></body></html>
"""

if __name__ == "__main__":
    main()
