#!/usr/bin/env python3
"""Render a test-run Markdown report as a self-contained HTML dashboard.

Usage:
    python3 render_report.py docs/test-runs/2026-08-11-1216-feature.md
    python3 render_report.py <report.md> -o <output.html> [--light]

Why this exists: the Markdown report is the git-friendly artifact, but reading a
12-case run in plain Markdown means scrolling past hundreds of lines of JSON to
answer "did it pass, and what did it touch?". This builds a different view of the
same facts — charts, an endpoint coverage table, and per-case cards — so the
answer is visible before you scroll at all.

It is deliberately NOT a Markdown-to-HTML converter. It reads the report, pulls
structure out of it (method, path, status code, verdict, whether the DB was
checked), and renders a dashboard from that. The prose sections come along for
the ride, but they are not the point.

Design constraints worth preserving if you edit this:
  * Python 3 stdlib only. A pip install standing between a finished test run and
    a readable report is a report nobody generates.
  * The output is ONE file — CSS, JS, and every chart as inline SVG. No CDN, no
    sibling assets. Reports get committed, moved, and emailed; one that only
    renders with network access stops rendering.
  * Everything shown is derived from the report, never invented. If a case has no
    curl to parse, its card says so rather than guessing an endpoint.
  * Degrade gracefully on old reports written before this script existed. They
    still parse; they just show fewer chips.
"""

from __future__ import annotations

import argparse
import html
import json
import math
import re
import sys
from collections import Counter, OrderedDict
from pathlib import Path

# ==========================================================================
# inline markdown
# ==========================================================================

def esc(text: str) -> str:
    """Escape only the three characters that can break out of a text node.

    Quotes stay literal on purpose: the JSON highlighter downstream matches on
    real `"` characters, and unescaped quotes in a text node are valid HTML.
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


SENTINEL = "\x00c{}\x00"


def inline(text: str) -> str:
    """Render inline Markdown: `code`, **bold**, *italic*, [text](url).

    Code spans are swapped for sentinels before the emphasis pass rather than
    rendered in place. Report headings routinely look like
    ``**`GET /api/me` no cookie — 401**``; splitting on the backticks first
    would strand the ``**`` markers in separate fragments where the bold regex
    can never see them.
    """
    spans: list[str] = []

    def stash(m: re.Match) -> str:
        spans.append(m.group(1))
        return SENTINEL.format(len(spans) - 1)

    staged = re.sub(r"`([^`]+)`", stash, text)
    s = esc(staged)
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<![*\w])\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", s)
    for i, code in enumerate(spans):
        s = s.replace(SENTINEL.format(i), f"<code>{esc(code)}</code>")
    return s


# ==========================================================================
# code blocks
# ==========================================================================

JSON_TOKENS = re.compile(
    r'("(?:\\u[0-9a-fA-F]{4}|\\[^u]|[^\\"])*"\s*:)'   # key
    r'|("(?:\\u[0-9a-fA-F]{4}|\\[^u]|[^\\"])*")'      # string
    r'|(\b(?:true|false|null)\b)'                     # literal
    r'|(-?\d+(?:\.\d*)?(?:[eE][+\-]?\d+)?)'           # number
)

SQL_KEYWORDS = re.compile(
    r"\b(SELECT|FROM|WHERE|INSERT|INTO|VALUES|UPDATE|SET|DELETE|JOIN|LEFT|RIGHT|INNER|"
    r"OUTER|ON|AND|OR|NOT|NULL|ORDER|GROUP|BY|LIMIT|OFFSET|AS|COUNT|DISTINCT|IS|IN|"
    r"CREATE|TABLE|ALTER|DROP|RETURNING|DESC|ASC)\b",
    re.I,
)

SHELL_TOKENS = re.compile(
    r"(^|\s)(curl|docker|psql|jq|grep|export|echo)\b"
    r"|(\s-{1,2}[A-Za-z][\w-]*)"
    r"|(\$[A-Za-z_][A-Za-z0-9_]*)"
)


def highlight_json(escaped: str) -> str:
    def repl(m: re.Match) -> str:
        if m.group(1):
            return f'<span class="j-key">{m.group(1)}</span>'
        if m.group(2):
            return f'<span class="j-str">{m.group(2)}</span>'
        if m.group(3):
            return f'<span class="j-lit">{m.group(3)}</span>'
        return f'<span class="j-num">{m.group(4)}</span>'
    return JSON_TOKENS.sub(repl, escaped)


def highlight_sql(escaped: str) -> str:
    return SQL_KEYWORDS.sub(lambda m: f'<span class="k-sql">{m.group(0)}</span>', escaped)


def highlight_shell(escaped: str) -> str:
    def repl(m: re.Match) -> str:
        if m.group(2):
            return f'{m.group(1)}<span class="k-cmd">{m.group(2)}</span>'
        if m.group(3):
            return f'<span class="k-flag">{m.group(3)}</span>'
        return f'<span class="k-var">{m.group(4)}</span>'
    return SHELL_TOKENS.sub(repl, escaped)


def pretty_json(raw: str) -> str | None:
    """Re-indent a JSON document. Returns None if it isn't JSON."""
    raw = raw.strip()
    if not raw or raw[0] not in "{[":
        return None
    try:
        return json.dumps(json.loads(raw), indent=2, ensure_ascii=False)
    except (ValueError, RecursionError):
        return None


STATUS_CLASS = {"1": "s-info", "2": "s-ok", "3": "s-info", "4": "s-warn", "5": "s-err"}

STATUS_TEXT = {
    200: "OK", 201: "Created", 202: "Accepted", 204: "No Content",
    301: "Moved Permanently", 302: "Found", 304: "Not Modified",
    400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found",
    405: "Method Not Allowed", 409: "Conflict", 415: "Unsupported Media Type",
    422: "Unprocessable Entity", 429: "Too Many Requests",
    500: "Internal Server Error", 502: "Bad Gateway", 503: "Service Unavailable",
}


def split_http(body: str) -> tuple[list[str], str]:
    """An `http` block is a status line + headers, a blank line, then a body."""
    lines = body.split("\n")
    head: list[str] = []
    idx = len(lines)
    for i, line in enumerate(lines):
        if line.strip() == "":
            idx = i
            break
        head.append(line)
    return head, "\n".join(lines[idx + 1:]).strip()


def render_http(body: str) -> str:
    head, rest = split_http(body)
    parts: list[str] = []
    for i, line in enumerate(head):
        if i == 0:
            code = re.search(r"\b([1-5]\d\d)\b", line)
            cls = STATUS_CLASS.get(code.group(1)[0], "s-ok") if code else "s-ok"
            parts.append(f'<span class="status {cls}">{esc(line)}</span>')
        else:
            name, sep, value = line.partition(":")
            if sep:
                parts.append(f'<span class="h-name">{esc(name)}</span>:<span class="h-val">{esc(value)}</span>')
            else:
                parts.append(esc(line))
    out = "\n".join(parts)
    if rest:
        pretty = pretty_json(rest)
        out += "\n\n" + (highlight_json(esc(pretty)) if pretty else esc(rest))
    return out


COPYABLE = {"bash", "sh", "shell", "console", "zsh"}


def render_code(lang: str, body: str, block_id: int) -> str:
    lang = (lang or "").lower()
    raw_for_copy = body

    if lang == "http":
        rendered = render_http(body)
    elif lang in ("json", "jsonc"):
        pretty = pretty_json(body)
        if pretty:
            raw_for_copy = pretty
        rendered = highlight_json(esc(pretty or body))
    elif lang == "sql":
        rendered = highlight_sql(esc(body))
    elif lang in COPYABLE:
        rendered = highlight_shell(esc(body))
    else:
        rendered = esc(body)

    label = f'<span class="lang">{esc(lang) or "output"}</span>'
    copy = f'<button class="copy" data-target="cb{block_id}">copy</button>' if lang in COPYABLE else ""
    # Long blocks start folded. Nothing is truncated — the bytes are all there,
    # the reader just isn't forced to scroll past 400 lines of i18n bundle.
    tall = " tall" if body.count("\n") > 28 else ""
    return (
        f'<div class="block{tall}">'
        f'<div class="block-bar">{label}{copy}</div>'
        f'<pre><code id="cb{block_id}" data-raw="{html.escape(raw_for_copy, quote=True)}">'
        f"{rendered}</code></pre>"
        f'{"<button class=unfold>show all lines</button>" if tall else ""}</div>'
    )


# ==========================================================================
# document model
# ==========================================================================

RESULT_RE = re.compile(r"\*\*Result:?\*\*:?\s*(PASS|FAIL|SKIP)", re.I)
LISTED_RESULT_RE = re.compile(r"(?:—|--|-|:)\s*\*{0,2}(PASS|FAIL|SKIP)\*{0,2}\s*(?:\(|$)", re.I)
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")


class Section:
    def __init__(self, level: int, title: str):
        self.level = level
        self.title = title
        self.html: list[str] = []
        self.verdict: str | None = None
        self.blocks: list[tuple[str, str]] = []   # (lang, raw text)
        self.raw: list[str] = []                  # prose lines, for label sniffing

    # --- facts pulled out of the raw blocks -------------------------------
    # Everything here is observed, never assumed. A case that doesn't show its
    # curl simply has no method/path, and the card says "no request captured"
    # rather than inventing one.

    @property
    def shell(self) -> str:
        return "\n".join(t for l, t in self.blocks if l in COPYABLE)

    def request(self) -> tuple[str | None, str | None, str | None]:
        """(method, path, host) from the first curl in the section.

        Scoped to the block that actually holds the curl. A case often ships a
        `docker exec … psql` block in the same section, and parsing flags across
        both blobs is how you end up reporting the wrong method.
        """
        blob = next((t for l, t in self.blocks if l in COPYABLE and "curl" in t), None)
        if blob is None:
            return None, None, None
        # A case's shell block often opens with setup calls — fetching a CSRF
        # token, grabbing a cookie — before the request being tested. Slice the
        # blob into curl invocations and read the last one; the setup comes
        # first, the request under test comes last.
        starts = [m.start() for m in re.finditer(r"\bcurl\b", blob)]
        sh = blob[starts[-1]:] if starts else blob
        m = re.search(r"-X\s+([A-Z]+)", sh)
        method = m.group(1) if m else None
        u = re.search(r"https?://([^/\s'\"]+)(/[^\s'\"\\]*)?", sh)
        path = host = None
        if u:
            host = u.group(1)
            path = u.group(2) or "/"
        h = re.search(r"-H\s+['\"]?Host:\s*([^'\"\s]+)", sh, re.I)
        if h:
            host = h.group(1)
        if method is None and path:
            # curl defaults to GET; a body flag means it isn't one.
            method = "POST" if re.search(r"\s(-d|--data|-F)\b", sh) else "GET"
        return method, path, host

    def status(self) -> int | None:
        """Status code of the last http block — the response under test."""
        for lang, text in reversed(self.blocks):
            if lang == "http":
                m = re.search(r"\b([1-5]\d\d)\b", text.split("\n")[0])
                if m:
                    return int(m.group(1))
        return None

    def error_code(self) -> str | None:
        """A `{MODULE}-{TYPE}-{NNNN}` style application error code, if present."""
        for lang, text in self.blocks:
            if lang in ("http", "json"):
                m = re.search(r'"(?:code|errorCode)"\s*:\s*"([^"]+)"', text)
                if m:
                    return m.group(1)
        return None

    def db_queries(self) -> int:
        """Count DB checks however the report happened to express them.

        A `sql` fence is the tidy form, but in practice the query is just as
        often a `docker exec … psql -c "SELECT …"` line inside a bash block.
        Both are a database check; only counting the tidy form under-reports the
        very evidence this skill exists to capture.
        """
        shown = sum(1 for l, _ in self.blocks if l == "sql")
        for lang, text in self.blocks:
            if lang in COPYABLE:
                shown += len(re.findall(r"\b(?:psql|mysql|mongosh|sqlite3|mongo)\b", text))
        # Some reports label the evidence ("**DB before:**") and paste the psql
        # output without repeating the command. That's still a DB check, so
        # count the labels too and take whichever count is higher rather than
        # adding them, which would double-count a well-formed case.
        labelled = len(re.findall(r"\*\*DB\s+(?:before|after)", "\n".join(self.raw), re.I))
        return max(shown, labelled)

    def num(self) -> str | None:
        m = re.match(r"\s*test\s*(\d+)", self.title, re.I)
        return m.group(1) if m else None

    def short_title(self) -> str:
        return re.sub(r"^\s*test\s*\d+\s*[:.\-—]\s*", "", self.title, flags=re.I)


def parse(md: str) -> tuple[str, list[Section]]:
    lines = md.split("\n")
    doc_title = "Test Run"
    sections: list[Section] = []
    current = Section(0, "")
    sections.append(current)

    i = block_id = 0
    para: list[str] = []
    list_items: list[str] = []
    list_kind: str | None = None
    table: list[str] = []

    def flush_para():
        nonlocal para
        if para:
            current.html.append("<p>" + inline(" ".join(para)) + "</p>")
            para = []

    def flush_list():
        nonlocal list_items, list_kind
        if list_items:
            tag = "ol" if list_kind == "ol" else "ul"
            body = "".join(f"<li>{inline(x)}</li>" for x in list_items)
            current.html.append(f"<{tag}>{body}</{tag}>")
            list_items = []
            list_kind = None

    def flush_table():
        nonlocal table
        if table:
            rows = [r.strip().strip("|").split("|") for r in table
                    if not re.match(r"^\s*\|?[\s:\-|]+\|?\s*$", r)]
            if rows:
                head = "".join(f"<th>{inline(c.strip())}</th>" for c in rows[0])
                body = "".join(
                    "<tr>" + "".join(f"<td>{inline(c.strip())}</td>" for c in r) + "</tr>"
                    for r in rows[1:]
                )
                current.html.append(f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>")
            table = []

    def flush_all():
        flush_para(); flush_list(); flush_table()

    while i < len(lines):
        line = lines[i]

        fence = re.match(r"^\s*```+\s*(\S*)\s*$", line)
        if fence:
            flush_all()
            lang = fence.group(1)
            i += 1
            buf: list[str] = []
            while i < len(lines) and not re.match(r"^\s*```+\s*$", lines[i]):
                buf.append(lines[i])
                i += 1
            i += 1
            block_id += 1
            raw = "\n".join(buf).rstrip()
            current.blocks.append((lang.lower(), raw))
            current.html.append(render_code(lang, raw, block_id))
            continue

        current.raw.append(line)

        heading = re.match(r"^(#{1,6})\s+(.*)$", line)
        if heading:
            flush_all()
            level = len(heading.group(1))
            title = heading.group(2).strip()
            if level == 1:
                doc_title = title
                i += 1
                continue
            current = Section(level, title)
            sections.append(current)
            i += 1
            continue

        if re.match(r"^\s*(---|\*\*\*|___)\s*$", line):
            flush_all()
            i += 1
            continue

        if line.lstrip().startswith("|") and line.count("|") >= 2:
            flush_para(); flush_list()
            table.append(line)
            i += 1
            continue
        flush_table()

        ul = re.match(r"^\s*[-*+]\s+(.*)$", line)
        ol = re.match(r"^\s*\d+[.)]\s+(.*)$", line)
        if ul or ol:
            flush_para()
            kind = "ol" if ol else "ul"
            if list_kind and list_kind != kind:
                flush_list()
            list_kind = kind
            list_items.append((ol or ul).group(1))
            i += 1
            continue

        if line.strip() == "":
            flush_para(); flush_list()
            i += 1
            continue

        m = RESULT_RE.search(line)
        if m:
            current.verdict = m.group(1).upper()
            # The verdict line is promoted into the card header, so drop the
            # duplicate from the body rather than saying it twice.
            rest = RESULT_RE.sub("", line).lstrip(" —-:")
            if rest.strip():
                current.html.append(f'<p class="verdict-note">{inline(rest.strip())}</p>')
            i += 1
            continue

        para.append(line.strip())
        i += 1

    flush_all()
    return doc_title, sections


# ==========================================================================
# charts (inline SVG, computed here so the page needs no JS to draw)
# ==========================================================================

VERDICT_COLOR = {"PASS": "var(--ok)", "FAIL": "var(--err)", "SKIP": "var(--warn)"}


def donut(counts: "OrderedDict[str, int]") -> str:
    total = sum(counts.values()) or 1
    r, cx, cy, w = 52.0, 70.0, 70.0, 15.0
    circ = 2 * math.pi * r
    segs: list[str] = []
    offset = 0.0
    for label, n in counts.items():
        if not n:
            continue
        length = circ * n / total
        segs.append(
            f'<circle class="seg" cx="{cx}" cy="{cy}" r="{r}" fill="none" '
            f'stroke="{VERDICT_COLOR.get(label, "var(--muted)")}" stroke-width="{w}" '
            f'stroke-dasharray="{length:.2f} {circ - length:.2f}" '
            f'stroke-dashoffset="{-offset:.2f}" stroke-linecap="butt">'
            f"<title>{label}: {n}</title></circle>"
        )
        offset += length

    passed = counts.get("PASS", 0)
    pct = round(100 * passed / total)
    ring_cls = "good" if pct == 100 else ("bad" if counts.get("FAIL") else "mid")
    return (
        f'<svg class="donut {ring_cls}" viewBox="0 0 140 140" role="img" '
        f'aria-label="{passed} of {total} cases passed">'
        f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="var(--line)" stroke-width="{w}"/>'
        f'<g transform="rotate(-90 {cx} {cy})">{"".join(segs)}</g>'
        f'<text class="d-num" x="70" y="66">{pct}%</text>'
        f'<text class="d-lab" x="70" y="88">{passed}/{total} passed</text>'
        "</svg>"
    )


def status_chart(codes: Counter) -> str:
    if not codes:
        return ""
    top = sum(codes.values())
    rows: list[str] = []
    for code, n in sorted(codes.items()):
        cls = STATUS_CLASS.get(str(code)[0], "s-ok")
        pct = 100 * n / top
        name = STATUS_TEXT.get(code, "")
        rows.append(
            f'<div class="bar-row">'
            f'<span class="bar-key {cls}">{code}</span>'
            f'<span class="bar-track"><span class="bar-fill {cls}" style="width:{pct:.1f}%"></span></span>'
            f'<span class="bar-val">{n}</span>'
            f'<span class="bar-note">{esc(name)}</span>'
            f"</div>"
        )
    return '<div class="bars">' + "".join(rows) + "</div>"


def method_class(method: str | None) -> str:
    return f"m-{(method or 'any').lower()}"


def normalize_path(path: str) -> str:
    """Collapse identifier segments so one endpoint is one row.

    A thorough run hits `/api/payments/41/capture`, `/api/payments/42/capture`
    and a dozen more ids. Those are one endpoint being tested twelve ways, not
    twelve endpoints, and listing them separately turns the coverage table into
    a transcript — the opposite of a summary. Query strings go too: `?page=2`
    and `?size=0` are cases of the same path.
    """
    path = path.split("?")[0].split("#")[0]
    out = []
    for seg in path.split("/"):
        if re.fullmatch(r"\d+", seg):
            out.append("{id}")
        elif re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                          r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", seg):
            out.append("{uuid}")
        elif re.fullmatch(r"\$\{?\w+\}?", seg):
            # a shell variable standing in for an id, e.g. /api/orders/$ORDER_ID
            out.append("{id}")
        else:
            out.append(seg)
    return "/".join(out)


def coverage_table(cases: list[Section]) -> str:
    """One row per endpoint actually exercised, with every status it returned.

    This is the answer to "what did this run actually cover?" — a question the
    Markdown can only answer by reading all twelve cases.
    """
    rows: "OrderedDict[tuple[str, str], dict]" = OrderedDict()
    for c in cases:
        method, path, _ = c.request()
        if not path:
            continue
        key = (method or "GET", normalize_path(path))
        entry = rows.setdefault(key, {"statuses": [], "verdicts": Counter()})
        st = c.status()
        if st is not None and st not in entry["statuses"]:
            entry["statuses"].append(st)
        entry["verdicts"][c.verdict or "SKIP"] += 1

    if not rows:
        return ""

    body: list[str] = []
    for (method, path), entry in rows.items():
        chips = "".join(
            f'<span class="pill {STATUS_CLASS.get(str(s)[0], "s-ok")}">{s}</span>'
            for s in sorted(entry["statuses"])
        )
        v = entry["verdicts"]
        verdict = "FAIL" if v["FAIL"] else ("PASS" if v["PASS"] else "SKIP")
        total = sum(v.values())
        body.append(
            f"<tr>"
            f'<td><span class="pill {method_class(method)}">{esc(method)}</span></td>'
            f'<td class="mono">{esc(path)}</td>'
            f'<td>{chips or "&mdash;"}</td>'
            f'<td class="num">{total}</td>'
            f'<td><span class="badge {verdict}">{verdict}</span></td>'
            f"</tr>"
        )
    return (
        '<table class="cov"><thead><tr>'
        "<th>Method</th><th>Path</th><th>Statuses seen</th><th>Cases</th><th>Verdict</th>"
        "</tr></thead><tbody>" + "".join(body) + "</tbody></table>"
    )


NA_RE = re.compile(r"^\s*(?:n/?a|not applicable|none|—|-)\s*$", re.I)
NOTRUN_RE = re.compile(r"not\s+run", re.I)


def scenario_coverage(sections: list[Section]) -> tuple[dict[str, int], str]:
    """Counts and a chip strip from the report's `## Scenario coverage` table.

    The pass ring says how many claims held. This says how many categories were
    even asked about — covered (case numbers), N/A (with a reason), not run
    (deferred by the depth cap) or left blank, which is the one a reader should
    distrust. Nothing is inferred: a report without the table gets no chips.
    """
    counts = {"covered": 0, "na": 0, "notrun": 0, "empty": 0}
    sec = next((s for s in sections if s.level == 2 and s.title.strip().lower() == "scenario coverage"), None)
    if sec is None:
        return counts, ""
    rows = [r for r in sec.raw if r.lstrip().startswith("|") and r.count("|") >= 2
            and not re.match(r"^\s*\|?[\s:\-|]+\|?\s*$", r)]
    chips: list[str] = []
    for r in rows[1:]:
        cells = [c.strip() for c in r.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        cat, cases = cells[0], cells[1]
        note = cells[2] if len(cells) > 2 else ""
        active = " ".join(seg for seg in re.split(r"[;]", cases) if not NOTRUN_RE.search(seg))
        if re.search(r"\d", active):
            kind, label = "covered", cases
        elif NOTRUN_RE.search(cases):
            kind, label = "notrun", cases
        elif NA_RE.match(cases):
            kind, label = "na", "N/A"
        else:
            kind, label = "empty", "unfilled"
        counts[kind] += 1
        cls = {"covered": "c-yes", "na": "c-na", "notrun": "c-no", "empty": "c-empty"}[kind]
        title = f' title="{esc(note)}"' if note else ""
        chips.append(f'<span class="{cls}"{title}>{inline(cat)} <em>{inline(label)}</em></span>')
    return counts, (f'<div class="scov">{"".join(chips)}</div>' if chips else "")


def flow_strip(case: Section) -> str:
    """A one-line picture of what the case did: request → response → DB check.

    Only observed steps appear. No DB block means no DB node, which is itself
    informative — a negative case that never touched the database is exactly
    what you want to see.
    """
    method, path, host = case.request()
    nodes: list[str] = []
    if path:
        label = f'<span class="pill {method_class(method)}">{esc(method or "GET")}</span>' \
                f'<span class="mono">{esc(path)}</span>'
        if host:
            label += f'<span class="flow-host">@ {esc(host)}</span>'
        nodes.append(f'<span class="flow-node">{label}</span>')
    st = case.status()
    if st is not None:
        cls = STATUS_CLASS.get(str(st)[0], "s-ok")
        nodes.append(
            f'<span class="flow-node"><span class="pill {cls}">{st}</span>'
            f'<span class="flow-sub">{esc(STATUS_TEXT.get(st, ""))}</span></span>'
        )
    code = case.error_code()
    if code:
        nodes.append(f'<span class="flow-node"><span class="pill s-warn">{esc(code)}</span></span>')
    q = case.db_queries()
    if q:
        nodes.append(
            f'<span class="flow-node"><span class="flow-db">DB</span>'
            f'<span class="flow-sub">{q} quer{"y" if q == 1 else "ies"}</span></span>'
        )
    if not nodes:
        return '<div class="flow empty">no request captured in this case</div>'
    return '<div class="flow">' + '<span class="flow-arrow">&rarr;</span>'.join(nodes) + "</div>"


# ==========================================================================
# page
# ==========================================================================

CSS = """
:root{
--bg:#0e1013;--panel:#161a20;--panel2:#1b1f27;--line:#272c36;--fg:#e7eaf0;--muted:#98a1b2;
--ok:#3fb950;--err:#f85149;--warn:#e3b341;--info:#58a6ff;--violet:#bc8cff;--pink:#f778ba;
--code:#0b0d10;--shadow:0 1px 2px rgba(0,0,0,.4),0 8px 24px -12px rgba(0,0,0,.6);}
html[data-theme=light]{
--bg:#f7f8fa;--panel:#fff;--panel2:#f2f4f7;--line:#e0e4ea;--fg:#1a1d23;--muted:#5f6875;
--ok:#177245;--err:#b3261e;--warn:#8a5a00;--info:#1a4f8a;--violet:#6b3fa0;--pink:#a83a72;
--code:#f6f7f9;--shadow:0 1px 2px rgba(16,24,40,.05),0 8px 24px -14px rgba(16,24,40,.25);}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);-webkit-font-smoothing:antialiased;
font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Helvetica,Arial,sans-serif;}
.mono,code,pre{font-family:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace}
a{color:var(--info)}
.wrap{max-width:1180px;margin:0 auto;padding:0 22px 100px}

/* ---- masthead ---- */
.mast{padding:34px 0 22px;border-bottom:1px solid var(--line);margin-bottom:26px;
display:flex;gap:20px;align-items:flex-start;flex-wrap:wrap}
.mast h1{font-size:27px;margin:0 0 6px;letter-spacing:-.02em;line-height:1.25}
.mast .meta{color:var(--muted);font-size:13px;display:flex;gap:14px;flex-wrap:wrap}
.mast .meta span{display:inline-flex;gap:6px;align-items:center}
.grow{flex:1 1 320px}
.theme{font:inherit;font-size:12px;padding:6px 12px;border-radius:999px;cursor:pointer;
border:1px solid var(--line);background:var(--panel);color:var(--muted)}
.theme:hover{color:var(--fg)}

/* ---- dashboard ---- */
.dash{display:grid;grid-template-columns:190px 1fr;gap:16px;margin-bottom:14px}
@media(max-width:820px){.dash{grid-template-columns:1fr}}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:14px;
padding:16px 18px;box-shadow:var(--shadow)}
.panel h2{font-size:11px;letter-spacing:.11em;text-transform:uppercase;color:var(--muted);
margin:0 0 12px;font-weight:700}
.donut-wrap{display:flex;align-items:center;justify-content:center;padding:2px}
.donut{width:150px;height:150px}
.donut .d-num{font:700 27px/1 -apple-system,system-ui,sans-serif;fill:var(--fg);text-anchor:middle}
.donut.good .d-num{fill:var(--ok)} .donut.bad .d-num{fill:var(--err)} .donut.mid .d-num{fill:var(--warn)}
.donut .d-lab{font:500 11px/1 -apple-system,system-ui,sans-serif;fill:var(--muted);text-anchor:middle}
.donut .seg{transition:opacity .15s}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(108px,1fr));gap:10px;margin-bottom:14px}
.stat{background:var(--panel2);border:1px solid var(--line);border-radius:11px;padding:11px 13px}
.stat b{display:block;font-size:23px;line-height:1.2;letter-spacing:-.02em}
.scov{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 12px}.scov span{font-size:12px;padding:3px 9px;border-radius:999px;border:1px solid var(--line);color:var(--fg)}.scov span em{font-style:normal;color:var(--muted);margin-left:4px}.scov .c-yes{border-color:var(--ok)}.scov .c-yes em{color:var(--ok)}.scov .c-na{color:var(--muted)}.scov .c-no{border-color:var(--warn)}.scov .c-no em{color:var(--warn)}.scov .c-empty{border-color:var(--err)}.scov .c-empty em{color:var(--err)}
.stat span{font-size:11px;color:var(--muted);letter-spacing:.05em;text-transform:uppercase}
.stat.ok b{color:var(--ok)} .stat.err b{color:var(--err)} .stat.warn b{color:var(--warn)}

/* ---- bar chart ---- */
.bars{display:flex;flex-direction:column;gap:7px}
.bar-row{display:grid;grid-template-columns:44px 1fr 26px auto;gap:10px;align-items:center;font-size:12.5px}
.bar-key{font-weight:700;font-family:ui-monospace,monospace;text-align:right}
.bar-track{height:9px;border-radius:5px;background:var(--panel2);overflow:hidden}
.bar-fill{display:block;height:100%;border-radius:5px}
.bar-fill.s-ok{background:var(--ok)} .bar-fill.s-info{background:var(--info)}
.bar-fill.s-warn{background:var(--warn)} .bar-fill.s-err{background:var(--err)}
.bar-val{color:var(--muted);font-family:ui-monospace,monospace}
.bar-note{color:var(--muted);font-size:11.5px}

/* ---- pills, badges ---- */
.pill{display:inline-block;padding:2px 8px;border-radius:6px;font:700 11px/1.6 ui-monospace,monospace;
letter-spacing:.03em;border:1px solid currentColor}
.m-get{color:var(--info)} .m-post{color:var(--ok)} .m-put{color:var(--warn)}
.m-patch{color:var(--violet)} .m-delete{color:var(--err)} .m-any{color:var(--muted)}
.pill.s-ok{color:var(--ok)} .pill.s-info{color:var(--info)}
.pill.s-warn{color:var(--warn)} .pill.s-err{color:var(--err)}
.badge{display:inline-block;font:700 10.5px/1.7 -apple-system,system-ui,sans-serif;
letter-spacing:.09em;padding:1px 9px;border-radius:999px;color:#fff}
.badge.PASS{background:var(--ok)} .badge.FAIL{background:var(--err)} .badge.SKIP{background:var(--warn)}

/* ---- coverage table ---- */
table{width:100%;border-collapse:collapse;font-size:13.5px;margin:8px 0}
th{text-align:left;font-size:11px;letter-spacing:.09em;text-transform:uppercase;
color:var(--muted);padding:6px 10px;border-bottom:1px solid var(--line)}
td{padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:middle}
tbody tr:last-child td{border-bottom:0}
tbody tr:hover{background:var(--panel2)}
td.num{color:var(--muted);font-family:ui-monospace,monospace}
.cov td:nth-child(3) .pill{margin-right:4px}

/* ---- prose panels ---- */
.prose p{margin:9px 0} .prose ul,.prose ol{margin:9px 0;padding-left:20px} .prose li{margin:4px 0}
.prose code{background:var(--code);padding:1.5px 5px;border-radius:4px;font-size:12.5px}
details.fold>summary{cursor:pointer;list-style:none;font-size:11px;letter-spacing:.11em;
text-transform:uppercase;color:var(--muted);font-weight:700;display:flex;gap:8px;align-items:center}
details.fold>summary::-webkit-details-marker{display:none}
details.fold>summary::after{content:"show";font-size:10px;letter-spacing:.06em;color:var(--info)}
details.fold[open]>summary::after{content:"hide"}

/* ---- section head + toolbar ---- */
.sec-head{display:flex;align-items:baseline;gap:12px;margin:30px 0 12px;flex-wrap:wrap}
.sec-head h2{font-size:16px;margin:0;letter-spacing:-.01em}
.sec-head .count{color:var(--muted);font-size:12.5px}
.toolbar{margin-left:auto;display:flex;gap:7px;flex-wrap:wrap}
.toolbar button{font:inherit;font-size:12px;padding:5px 11px;border-radius:8px;cursor:pointer;
border:1px solid var(--line);background:var(--panel);color:var(--muted)}
.toolbar button:hover{color:var(--fg);border-color:var(--muted)}
.toolbar button.on{background:var(--err);border-color:var(--err);color:#fff}

/* ---- case cards ---- */
details.case{background:var(--panel);border:1px solid var(--line);border-left:3px solid var(--muted);
border-radius:12px;margin:9px 0;overflow:hidden;box-shadow:var(--shadow)}
details.case[data-verdict=PASS]{border-left-color:var(--ok)}
details.case[data-verdict=FAIL]{border-left-color:var(--err);border-color:var(--err)}
details.case[data-verdict=SKIP]{border-left-color:var(--warn)}
details.case>summary{cursor:pointer;padding:13px 16px;list-style:none;display:flex;
gap:11px;align-items:center;flex-wrap:wrap}
details.case>summary::-webkit-details-marker{display:none}
details.case>summary::before{content:"\\25B8";color:var(--muted);font-size:11px;transition:transform .15s}
details.case[open]>summary::before{transform:rotate(90deg)}
details.case>summary:hover{background:var(--panel2)}
.case-n{font:700 11px/1 ui-monospace,monospace;color:var(--muted);background:var(--panel2);
border:1px solid var(--line);border-radius:5px;padding:4px 6px;min-width:26px;text-align:center}
.case-t{font-weight:600;flex:1 1 260px;font-size:14.5px}
.case-t code{background:var(--code);padding:1px 5px;border-radius:4px;font-size:12.5px;font-weight:500}
.case-body{padding:0 16px 16px;border-top:1px solid var(--line)}
.verdict-note{color:var(--muted);font-size:13.5px;border-left:2px solid var(--line);
padding-left:11px;margin:12px 0 2px}

/* ---- flow strip ---- */
.flow{display:flex;align-items:center;gap:9px;flex-wrap:wrap;padding:12px 0 2px;font-size:12.5px}
.flow.empty{color:var(--muted);font-style:italic}
.flow-node{display:inline-flex;align-items:center;gap:7px;background:var(--panel2);
border:1px solid var(--line);border-radius:9px;padding:5px 10px}
.flow-arrow{color:var(--muted)}
.flow-sub,.flow-host{color:var(--muted);font-size:11.5px}
.flow-db{font:700 11px/1 ui-monospace,monospace;color:var(--violet);border:1px solid var(--violet);
border-radius:5px;padding:3px 6px}

/* ---- code blocks ---- */
.block{margin:12px 0;border:1px solid var(--line);border-radius:10px;overflow:hidden;background:var(--code)}
.block-bar{display:flex;align-items:center;gap:8px;padding:5px 11px;min-height:27px;
border-bottom:1px solid var(--line)}
.lang{font-size:10px;letter-spacing:.11em;text-transform:uppercase;color:var(--muted);font-weight:700}
.copy,.unfold{margin-left:auto;font:inherit;font-size:11px;padding:2px 9px;border-radius:6px;
cursor:pointer;border:1px solid var(--line);background:var(--panel);color:var(--muted)}
.copy:hover,.unfold:hover{color:var(--fg)}
.copy.done{color:var(--ok);border-color:var(--ok)}
.unfold{display:block;width:100%;margin:0;border:0;border-top:1px solid var(--line);
border-radius:0;padding:6px;text-align:center}
.block.tall pre{max-height:340px;overflow:auto}
.block.tall.open pre{max-height:none}
pre{margin:0;padding:13px 15px;overflow-x:auto}
pre code{background:none;padding:0;font-size:12.5px;line-height:1.55;display:block;
white-space:pre;color:var(--fg)}
.status{font-weight:700} .s-ok{color:var(--ok)} .s-info{color:var(--info)}
.s-warn{color:var(--warn)} .s-err{color:var(--err)}
.h-name{color:var(--muted)} .h-val{color:var(--fg)}
.j-key{color:var(--info)} .j-str{color:var(--ok)} .j-num{color:var(--warn)}
.j-lit{color:var(--pink);font-weight:600}
.k-sql{color:var(--violet);font-weight:600} .k-cmd{color:var(--info);font-weight:600}
.k-flag{color:var(--warn)} .k-var{color:var(--pink)}
.hide{display:none!important}
"""

JS = """
var root=document.documentElement;
var saved=localStorage.getItem('tbc-theme'); if(saved) root.dataset.theme=saved;
document.getElementById('theme').onclick=function(){
  var next = root.dataset.theme==='light' ? 'dark' : 'light';
  root.dataset.theme=next; localStorage.setItem('tbc-theme',next);
};
document.addEventListener('click',function(e){
  var b=e.target.closest('.copy');
  if(b){
    var el=document.getElementById(b.dataset.target);
    navigator.clipboard.writeText(el.dataset.raw).then(function(){
      b.textContent='copied'; b.classList.add('done');
      setTimeout(function(){b.textContent='copy';b.classList.remove('done');},1200);
    });
    return;
  }
  var u=e.target.closest('.unfold');
  if(u){
    var blk=u.closest('.block'); var open=blk.classList.toggle('open');
    u.textContent = open ? 'collapse' : 'show all lines';
  }
});
function cases(){return Array.prototype.slice.call(document.querySelectorAll('details.case'));}
document.getElementById('expand').onclick=function(){cases().forEach(function(d){d.open=true;});};
document.getElementById('collapse').onclick=function(){cases().forEach(function(d){d.open=false;});};
var only=document.getElementById('onlyfail');
if(only) only.onclick=function(){
  var on=only.classList.toggle('on');
  cases().forEach(function(d){
    var fail=d.dataset.verdict==='FAIL';
    d.classList.toggle('hide',on&&!fail);
    if(on&&fail) d.open=true;
  });
};
"""

PAGE = """<!doctype html>
<html lang="en" data-theme="dark"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>{css}</style></head>
<body><div class="wrap">
<header class="mast">
  <div class="grow"><h1>{h1}</h1><div class="meta">{meta}</div></div>
  <button class="theme" id="theme">light / dark</button>
</header>
{dash}
{coverage}
{prose}
<div class="sec-head"><h2>Test cases</h2><span class="count">{ncases}</span>
<div class="toolbar">
<button id="expand">Expand all</button><button id="collapse">Collapse all</button>{failbtn}
</div></div>
{cases}
{tail}
</div><script>{js}</script></body></html>
"""


def build(md: str, source_name: str) -> str:
    doc_title, sections = parse(md)

    # A "case" is any h3+ that reached a verdict. Everything else is prose.
    cases = [s for s in sections if s.level >= 3 and s.verdict]
    if not cases:
        cases = [s for s in sections if s.level >= 3 and s.blocks]

    verdicts = [c.verdict or "SKIP" for c in cases]
    counts: "OrderedDict[str,int]" = OrderedDict(
        (k, verdicts.count(k)) for k in ("PASS", "FAIL", "SKIP")
    )
    total = len(cases)

    codes = Counter(c.status() for c in cases if c.status() is not None)
    endpoints = {(c.request()[0], normalize_path(c.request()[1])) for c in cases if c.request()[1]}
    db_qs = sum(c.db_queries() for c in cases)
    cov_counts, cov_chips = scenario_coverage(sections)

    # --- masthead meta, scavenged from the report's own bullet lines --------
    meta_bits: list[str] = []
    m = re.search(r"^\s*[-*]\s*\*{0,2}Branch\*{0,2}:?\s*(.+)$", md, re.M)
    if m:
        meta_bits.append(f'<span>branch {inline(m.group(1).strip())}</span>')
    meta_bits.append(f'<span>{total} case{"" if total == 1 else "s"}</span>')
    if endpoints:
        meta_bits.append(f'<span>{len(endpoints)} endpoint{"" if len(endpoints) == 1 else "s"}</span>')
    meta_bits.append(f'<span>source <code>{esc(source_name)}</code></span>')

    # --- dashboard ---------------------------------------------------------
    stats = [
        ("", total, "cases"),
        ("ok", counts["PASS"], "passed"),
        ("err", counts["FAIL"], "failed"),
    ]
    if counts["SKIP"]:
        stats.append(("warn", counts["SKIP"], "skipped"))
    stats.append(("", len(endpoints), "endpoints"))
    stats.append(("", db_qs, "db checks"))
    if sum(cov_counts.values()):
        stats.append(("ok" if not cov_counts["empty"] else "", cov_counts["covered"], "categories"))
        if cov_counts["na"]:
            stats.append(("", cov_counts["na"], "n/a"))
        if cov_counts["notrun"]:
            stats.append(("warn", cov_counts["notrun"], "not run"))
        if cov_counts["empty"]:
            stats.append(("err", cov_counts["empty"], "unfilled"))
    stat_html = "".join(
        f'<div class="stat {cls}"><b>{val}</b><span>{lab}</span></div>'
        for cls, val, lab in stats
    )
    chart = status_chart(codes)
    dash = (
        '<div class="dash">'
        f'<div class="panel donut-wrap">{donut(counts)}</div>'
        f'<div class="panel"><h2>Run at a glance</h2><div class="stats">{stat_html}</div>'
        f'{"<h2>Response status codes</h2>" + chart if chart else ""}</div>'
        "</div>"
    )

    cov = coverage_table(cases)
    coverage = f'<div class="panel"><h2>Endpoint coverage</h2>{cov}</div>' if cov else ""

    # --- prose panels ------------------------------------------------------
    # The "Test cases" checklist and "Details" heading are dropped on purpose:
    # the dashboard and the cards already say all of that, and repeating it is
    # what made the first version feel like a photocopy of the Markdown.
    SKIP_SECTIONS = ("test cases", "details")
    LONG_SECTIONS = ("setup",)
    prose_parts: list[str] = []
    tail_parts: list[str] = []
    for s in sections:
        if s.level != 2:
            continue
        title_l = s.title.strip().lower()
        if title_l in SKIP_SECTIONS:
            continue
        content = "".join(s.html)
        if title_l == "scenario coverage" and cov_chips:
            content = cov_chips + content
        if not content.strip():
            continue
        if title_l in LONG_SECTIONS:
            panel = (
                f'<div class="panel"><details class="fold"><summary>{inline(s.title)}</summary>'
                f'<div class="prose">{content}</div></details></div>'
            )
        else:
            panel = f'<div class="panel"><h2>{inline(s.title)}</h2><div class="prose">{content}</div></div>'
        (tail_parts if title_l.startswith("summary") else prose_parts).append(panel)

    # --- case cards --------------------------------------------------------
    card_html: list[str] = []
    for i, c in enumerate(cases, 1):
        v = c.verdict or "SKIP"
        n = c.num() or str(i)
        method, path, _ = c.request()
        pill = f'<span class="pill {method_class(method)}">{esc(method)}</span>' if method else ""
        st = c.status()
        st_pill = (
            f'<span class="pill {STATUS_CLASS.get(str(st)[0], "s-ok")}">{st}</span>'
            if st is not None else ""
        )
        card_html.append(
            f'<details class="case" data-verdict="{v}"{" open" if v == "FAIL" else ""}>'
            f'<summary><span class="case-n">{esc(n)}</span>'
            f'<span class="case-t">{inline(c.short_title())}</span>'
            f'{pill}{st_pill}<span class="badge {v}">{v}</span></summary>'
            f'<div class="case-body">{flow_strip(c)}{"".join(c.html)}</div></details>'
        )

    failbtn = '<button id="onlyfail">Only failures</button>' if counts["FAIL"] else ""

    return PAGE.format(
        title=html.escape(doc_title, quote=True),
        css=CSS, js=JS,
        h1=inline(doc_title),
        meta="".join(meta_bits),
        dash=dash,
        coverage=coverage,
        prose="".join(prose_parts),
        ncases=f'{total} case{"" if total == 1 else "s"}',
        failbtn=failbtn,
        cases="".join(card_html) or '<div class="panel">No test cases found in this report.</div>',
        tail="".join(tail_parts),
    )


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Render a test-run Markdown report as a self-contained HTML dashboard."
    )
    ap.add_argument("report", help="path to the .md report")
    ap.add_argument("-o", "--output", help="output .html path (default: same name, .html)")
    ap.add_argument("--light", action="store_true", help="default to the light theme")
    args = ap.parse_args()

    src = Path(args.report)
    if not src.is_file():
        print(f"error: no such file: {src}", file=sys.stderr)
        return 1
    page = build(src.read_text(encoding="utf-8"), src.name)
    if args.light:
        page = page.replace('data-theme="dark"', 'data-theme="light"', 1)
    dest = Path(args.output) if args.output else src.with_suffix(".html")
    dest.write_text(page, encoding="utf-8")
    print(dest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
