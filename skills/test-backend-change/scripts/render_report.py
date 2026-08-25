#!/usr/bin/env python3
"""Render a test-run Markdown report as a self-contained HTML dashboard.

Usage:
    python3 render_report.py docs/test-runs/2026-08-11-1216-feature.md
    python3 render_report.py <report.md> -o <output.html> [--dark]

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
import base64
import html
import json
import math
import re
import shlex
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
# `sql` is not something a reader copies out of a terminal, but once a psql
# wrapper has been unwrapped into plain SQL it is the thing they most want on
# the clipboard, so it earns a copy button without joining COPYABLE — which
# still means "a shell block" everywhere else in this file.
COPY_LANGS = COPYABLE | {"sql"}


# ==========================================================================
# shell, read once
# ==========================================================================
#
# One parser serves the dashboard (which wants method and path), the coverage
# table, the Run button (which needs every part of a request a browser is
# allowed to send) and the DB blocks (which want the SQL without the container
# plumbing). Two parsers would drift, and the day they disagreed the page would
# show one endpoint and fire another.


def shell_commands(blob: str, keep_comments: bool = False) -> list[str]:
    """Split a shell block into one string per command.

    Quote-aware on purpose. Splitting on newlines alone tears a backslash-wrapped
    curl into four fragments; splitting on `;` alone cuts through the middle of a
    JSON body that happens to contain one.
    """
    out: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i, n = 0, len(blob)
    while i < n:
        c = blob[i]
        if quote:
            buf.append(c)
            if c == quote:
                quote = None
            elif quote == '"' and c == "\\" and i + 1 < n:
                buf.append(blob[i + 1])
                i += 1
            i += 1
            continue
        if c in "'\"":
            quote = c
            buf.append(c)
            i += 1
            continue
        if c == "\\" and i + 1 < n and blob[i + 1] == "\n":
            buf.append(" ")             # a wrapped line is still one command
            i += 2
            continue
        if c == "#" and (not buf or buf[-1].isspace()):
            start = i
            while i < n and blob[i] != "\n":
                i += 1
            if keep_comments:
                out.append("".join(buf))
                buf = []
                out.append(blob[start:i])
            continue
        if c in "\n;|&":
            out.append("".join(buf))
            buf = []
            i += 1
            while i < n and blob[i] in "|&":
                i += 1
            continue
        buf.append(c)
        i += 1
    out.append("".join(buf))
    return [s.strip() for s in out if s.strip()]


# --------------------------------------------------------------------------
# curl
# --------------------------------------------------------------------------

# Things a browser is not doing and does not need to know: where curl wrote the
# headers, how loud it was, how long it waited.
CURL_IGNORED = {
    "-s", "--silent", "-S", "--show-error", "-v", "--verbose", "-i", "--include",
    "-k", "--insecure", "-f", "--fail", "--fail-with-body", "-g", "--globoff",
    "-#", "--progress-bar", "-N", "--no-buffer", "--compressed", "-L", "--location",
    "-n", "--netrc", "--http1.1", "--http2", "--no-progress-meter", "-q", "-4", "-6",
    "-j", "--junk-session-cookies", "-R", "--remote-time",
}
# The same, but they eat the next word too.
CURL_IGNORED_WITH_VALUE = {
    "-o", "--output", "-D", "--dump-header", "-w", "--write-out", "-m", "--max-time",
    "--connect-timeout", "--retry", "--retry-delay", "--retry-max-time", "-c",
    "--cookie-jar", "--resolve", "--proxy", "-x", "--limit-rate", "--interface",
    "--cacert", "--cert", "--key", "--stderr", "--trace", "--trace-ascii", "--tlsv1.2",
}
CURL_DATA_FLAGS = {"-d", "--data", "--data-raw", "--data-ascii", "--data-binary",
                   "--data-urlencode"}
CURL_VALUE_FLAGS = (CURL_IGNORED_WITH_VALUE | CURL_DATA_FLAGS
                    | {"-X", "--request", "-H", "--header", "-b", "--cookie", "-u", "--user",
                       "-A", "--user-agent", "-e", "--referer", "-F", "--form", "--url"})

# Header names the fetch spec forbids a page to set. Naming them is not
# pedantry: `Host` is how this application routes tenants, so a reader has to be
# told the header was dropped rather than left to wonder why a request landed on
# the wrong tenant.
FORBIDDEN_HEADERS = {
    "host", "cookie", "connection", "content-length", "date", "dnt", "expect",
    "keep-alive", "origin", "referer", "te", "trailer", "transfer-encoding",
    "upgrade", "via", "accept-encoding", "accept-charset",
}

VAR_RE = re.compile(r"\$\{(\w+)\}|\$(\w+)")
# The one variable the page can fill in for itself: Spring mints the CSRF cookie
# `HttpOnly=false` on purpose, so `document.cookie` can read it at click time.
XSRF_VAR_RE = re.compile(r"^XSRF", re.I)


def curl_commands(blob: str) -> list[str]:
    """Every curl invocation in a shell block, in the order it was written."""
    found: list[str] = []
    for cmd in shell_commands(blob):
        head = cmd
        # `do curl …`, `then curl …`, `TOK=x curl …` — strip the run-up.
        while True:
            m = re.match(r"^(?:[({]|do|then|else|time|env|sudo|nohup"
                         r"|\w+=(?:[^\s'\"]*|\"[^\"]*\"|'[^']*'))\s*",
                         head)
            if not m:
                break
            head = head[m.end():]
        if re.match(r"^curl\b", head):
            found.append(head)
    return found


def expand_short_flags(words: list[str], i: int) -> bool:
    """`-sS` is two flags; `-so file` is two flags and a value. Split in place."""
    w = words[i]
    if not re.fullmatch(r"-[A-Za-z0-9#]{2,}", w) or w in CURL_IGNORED:
        return False
    parts = [f"-{ch}" for ch in w[1:]]
    known = CURL_IGNORED | CURL_VALUE_FLAGS | {"-I"}
    if not all(p in known for p in parts):
        return False
    # Only the last letter of a cluster may take a value; `-Xo` is nonsense.
    if any(p in CURL_VALUE_FLAGS for p in parts[:-1]):
        return False
    words[i:i + 1] = parts
    return True


def parse_curl(cmd: str) -> dict:
    """Everything a browser needs to repeat one curl, and why it cannot.

    `blocked` is a sentence for the reader, not a flag: "the recorded command
    builds this request from $TOK" tells them what is missing; "unrunnable"
    does not.
    """
    spec: dict = {
        "method": None, "url": None, "host": None, "headers": [],
        "body": None, "cookies": [], "dropped": [], "vars": [], "blocked": "",
    }
    try:
        words = shlex.split(cmd, posix=True)
    except ValueError:
        spec["blocked"] = "the shell quoting in this command does not parse"
        return spec
    if not words:
        return spec

    data: list[str] = []
    i = 1                                   # words[0] is `curl`
    while i < len(words):
        if expand_short_flags(words, i):
            continue
        w = words[i]
        nxt = words[i + 1] if i + 1 < len(words) else None

        if w in CURL_IGNORED:
            i += 1
        elif w in CURL_IGNORED_WITH_VALUE:
            i += 2
        elif w in ("-I", "--head"):
            spec["method"] = spec["method"] or "HEAD"
            i += 1
        elif w in ("-X", "--request") and nxt:
            spec["method"] = nxt.upper()
            i += 2
        elif w in ("-H", "--header") and nxt is not None:
            name, sep, value = nxt.partition(":")
            name = name.strip()
            # `-H 'Accept-Language;'` is curl's way of sending an empty value.
            if not sep and name.endswith(";"):
                name, value = name[:-1].strip(), ""
            if name:
                spec["headers"].append([name, value.strip()])
            i += 2
        elif w in ("-b", "--cookie") and nxt is not None:
            spec["headers"].append(["Cookie", nxt.strip()])
            i += 2
        elif w in ("-u", "--user") and nxt is not None:
            spec["blocked"] = spec["blocked"] or "it authenticates with curl -u"
            i += 2
        elif w in ("-A", "--user-agent", "-e", "--referer") and nxt is not None:
            spec["dropped"].append("User-Agent" if w in ("-A", "--user-agent") else "Referer")
            i += 2
        elif w in ("-F", "--form") and nxt is not None:
            spec["blocked"] = (spec["blocked"]
                               or "it posts a multipart form built on the test machine")
            i += 2
        elif w in CURL_DATA_FLAGS and nxt is not None:
            if nxt.startswith("@"):
                spec["blocked"] = (spec["blocked"] or "its body is read from the file "
                                   f"{nxt[1:]} on the machine that ran the test")
            else:
                data.append(nxt)
            i += 2
        elif w == "--url" and nxt:
            spec["url"] = nxt
            i += 2
        elif w.startswith("-"):
            i += 1                          # an option this parser has not met
        else:
            if spec["url"] is None:
                spec["url"] = w
            i += 1

    if data:
        spec["body"] = "&".join(data) if len(data) > 1 else data[0]
    if spec["method"] is None:
        # curl defaults to GET; a body flag means it is not one.
        spec["method"] = "POST" if spec["body"] is not None else "GET"

    # Host and Cookie are stripped here, not at run time, so the page can tell
    # the reader exactly which headers will not travel and why.
    keep: list[list[str]] = []
    for name, value in spec["headers"]:
        low = name.lower()
        if low == "host":
            spec["host"] = value.split(":")[0].strip() or None
        elif low == "cookie":
            for part in value.split(";"):
                cname = part.split("=")[0].strip()
                if cname and cname not in spec["cookies"]:
                    spec["cookies"].append(cname)
        elif low in FORBIDDEN_HEADERS or low.startswith(("sec-", "proxy-")):
            spec["dropped"].append(name)
        else:
            keep.append([name, value])
    spec["headers"] = keep

    if not spec["url"]:
        spec["blocked"] = spec["blocked"] or "no URL could be read out of it"
    elif not re.match(r"https?://", spec["url"]):
        spec["blocked"] = spec["blocked"] or "its URL is not an absolute http address"

    # A shell variable the page cannot know would make the request wrong, and a
    # wrong request answered 200 is worse than no request at all. `$XSRF` is the
    # exception, resolved from the readable CSRF cookie when the button is hit.
    for text in [spec["url"] or ""] + [v for _, v in spec["headers"]] + [spec["body"] or ""]:
        for m in VAR_RE.finditer(text):
            name = m.group(1) or m.group(2)
            if not XSRF_VAR_RE.match(name) and name not in spec["vars"]:
                spec["vars"].append(name)
    if spec["vars"] and not spec["blocked"]:
        names = ", ".join("$" + v for v in spec["vars"])
        spec["blocked"] = f"it builds this request from {names}, set by an earlier step"
    return spec


# A block a browser can honestly replay, and the guard that keeps Run off
# everything else. The test is the block's content, not the label the report
# filed it under: a `docker exec … psql` block is evidence a browser can neither
# run nor fake, so it never gets a Run button even if a future report puts one
# under **Request:**.
NOT_IN_BROWSER_RE = re.compile(
    r"\b(?:docker|podman|kubectl|psql|mysql|mongosh|mongo|sqlite3|redis-cli|valkey-cli"
    r"|ssh|scp|systemctl|journalctl|awslocal|aws|gcloud|kafka-topics(?:\.sh)?)\b"
)


def browser_runnable(lang: str, text: str) -> bool:
    """True only for a shell block that is one or more plain curl calls."""
    if lang not in COPYABLE or "curl" not in text:
        return False
    return not NOT_IN_BROWSER_RE.search(text)


# --------------------------------------------------------------------------
# psql, unwrapped
# --------------------------------------------------------------------------

# psql options this renderer knows how to skip past. Anything else and the block
# is left exactly as it was written -- a query shown wrong is worse than a query
# shown with its container plumbing still attached.
PSQL_FLAGS = {"-A", "--no-align", "-t", "--tuples-only", "-x", "--expanded", "-q", "--quiet",
              "-X", "--no-psqlrc", "-w", "--no-password", "-E", "--echo-hidden",
              "-b", "--echo-errors", "-e", "--echo-queries", "-s", "--single-step",
              "-1", "--single-transaction", "-n", "--no-readline", "-L", "--log-file"}
PSQL_FLAGS_WITH_VALUE = {"-U", "--username", "-h", "--host", "-p", "--port", "-F",
                         "--field-separator", "-P", "--pset", "-v", "--set", "--variable",
                         "-R", "--record-separator", "-o", "--output", "--log-file"}
DOCKER_EXEC_FLAGS = {"-i", "--interactive", "-t", "--tty", "-d", "--detach", "--privileged"}
DOCKER_EXEC_FLAGS_WITH_VALUE = {"-e", "--env", "-u", "--user", "-w", "--workdir"}


def unwrap_psql(cmd: str) -> tuple[str, list[str]] | None:
    """`docker exec … psql -U x -d db -c "SQL"` reduced to (db, [SQL, …]).

    Returns None the moment anything is not that exact shape -- a psql reading a
    file, a docker exec running something else, an option this list has not met.
    The caller then leaves the block alone.
    """
    try:
        words = shlex.split(cmd, posix=True)
    except ValueError:
        return None
    if len(words) < 3 or words[0] not in ("docker", "podman") or words[1] != "exec":
        return None

    i = 2
    while i < len(words):                      # docker's own options
        w = words[i]
        if w in DOCKER_EXEC_FLAGS:
            i += 1
        elif w in DOCKER_EXEC_FLAGS_WITH_VALUE:
            i += 2
        elif re.fullmatch(r"-[a-zA-Z]{2,}", w) and all(f"-{c}" in DOCKER_EXEC_FLAGS for c in w[1:]):
            i += 1
        elif w.startswith("-"):
            return None
        else:
            break
    i += 1                                     # the container name
    if i >= len(words) or words[i] != "psql":
        return None

    i += 1
    db: str | None = None
    queries: list[str] = []
    positional: list[str] = []
    while i < len(words):
        w = words[i]
        nxt = words[i + 1] if i + 1 < len(words) else None
        if w in ("-c", "--command") and nxt is not None:
            queries.append(nxt)
            i += 2
        elif w in ("-d", "--dbname") and nxt is not None:
            db = nxt
            i += 2
        elif w in PSQL_FLAGS:
            i += 1
        elif w in PSQL_FLAGS_WITH_VALUE:
            i += 2
        elif re.fullmatch(r"-[a-zA-Z]{2,}", w) and all(f"-{c}" in PSQL_FLAGS or f"-{c}" in ("-c", "-d")
                                                       for c in w[1:-1]):
            # A cluster such as `-At`, or `-Atc "SELECT 1"` where only the last
            # letter may take a value.
            words[i:i + 1] = [f"-{c}" for c in w[1:]]
            continue
        elif w.startswith("-"):
            return None                        # an option this parser has not met
        else:
            positional.append(w)
            i += 1
    if not queries:
        return None                            # interactive psql, or -f file
    if db is None and positional:
        db = positional[0]
    return (db or "", queries)


def dedent_sql(sql: str) -> str:
    """Strip the indentation a query picked up from aligning under `-c "`."""
    lines = sql.strip("\n").split("\n")
    if len(lines) < 2:
        return sql.strip()
    tails = [ln for ln in lines[1:] if ln.strip()]
    pad = min((len(ln) - len(ln.lstrip()) for ln in tails), default=0)
    return "\n".join([lines[0].strip()] + [ln[pad:].rstrip() for ln in lines[1:]]).strip()


def as_sql_block(body: str) -> str | None:
    """A whole shell block rewritten as plain SQL, or None to leave it alone.

    Every command in the block has to be a recognised `docker exec … psql -c`
    for the rewrite to happen. One kafka or valkey line among them and the block
    stays as the report wrote it, container plumbing and all -- half-unwrapping a
    block would lose the half this parser did not understand.
    """
    if "psql" not in body or re.search(r"(?m)^\s*#", body):
        # A comment carries the tester's reasoning and there is nowhere to put it
        # in the rewritten SQL, so a commented block is left as written.
        return None
    cmds = shell_commands(body)
    if not cmds:
        return None
    out: list[str] = []
    last_db: str | None = None
    for cmd in cmds:
        parsed = unwrap_psql(cmd)
        if parsed is None:
            return None
        db, queries = parsed
        for q in queries:
            sql = dedent_sql(q)
            if db and db != last_db:
                sql = f"-- {db}\n{sql}"
                last_db = db
            out.append(sql)
    return "\n\n".join(out) if out else None


def render_http_block(body: str, block_id: int) -> str | None:
    """A response as a status chip, folded headers, and the body up front.

    The body is what a reader checks a claim against; the nine cache and
    frame headers above it are almost never the point. Fold them behind a
    count, and the JSON is the first thing seen. Returns None for a block that
    has no status line, so the caller can fall back to the plain rendering.
    """
    head, rest = split_http(body)
    first = head[0].strip() if head else ""
    if not re.match(r"^HTTP/", first, re.I):
        return None
    code_m = re.search(r"\b([1-5]\d\d)\b", first)
    code = int(code_m.group(1)) if code_m else None
    cls = STATUS_CLASS.get(str(code)[0], "s-ok") if code else "s-ok"
    reason = first[code_m.end():].strip() if code_m else ""
    chip = f"{code} {reason or STATUS_TEXT.get(code, '')}".strip() if code else first

    headers = head[1:]
    fold = ""
    if headers:
        lines = [f'<span class="status {cls}">{esc(first)}</span>']
        for line in headers:
            name, sep, value = line.partition(":")
            lines.append(
                f'<span class="h-name">{esc(name)}</span>:<span class="h-val">{esc(value)}</span>'
                if sep else esc(line)
            )
        n = len(headers)
        fold = (
            f'<details class="hdrs"><summary>{n} header{"" if n == 1 else "s"}</summary>'
            f'<pre><code>{chr(10).join(lines)}</code></pre></details>'
        )

    pretty = pretty_json(rest) if rest else None
    body_html = highlight_json(esc(pretty)) if pretty else esc(rest)
    shown = pretty or rest
    tall = " tall" if shown.count("\n") > 28 else ""
    content = (
        f'<pre><code id="cb{block_id}" data-raw="{html.escape(body, quote=True)}">{body_html}</code></pre>'
        if shown else f'<div class="http-empty" id="cb{block_id}">no body</div>'
    )
    return (
        f'<div class="block http{tall}">'
        f'<div class="block-bar"><span class="lang">http</span>'
        f'<span class="http-status {cls}">{esc(chip)}</span></div>'
        f"{fold}{content}"
        f'{"<button class=unfold>show all lines</button>" if tall else ""}</div>'
    )


def render_code(lang: str, body: str, block_id: int) -> str:
    lang = (lang or "").lower()
    raw_for_copy = body

    if lang == "http":
        block = render_http_block(body, block_id)
        if block is not None:
            return block
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
    # The Run slot is a placeholder, not a button. Only `build` knows which block
    # a case actually tested, so only `build` fills a slot in; every slot it does
    # not fill is stripped from the page. `browser_runnable` is what keeps Run off
    # a shell or DB block even if a future report files one under **Request:**.
    # `run` sits before `copy` in the DOM, but the CSS keeps copy where it has
    # always been — hard right. A new button that moves the one people already
    # use is a regression dressed as a feature.
    run = f"<!--RUN:cb{block_id}-->" if browser_runnable(lang, body) else ""
    copy = f'<button class="copy" data-target="cb{block_id}">copy</button>' if lang in COPY_LANGS else ""
    # Long blocks start folded. Nothing is truncated — the bytes are all there,
    # the reader just isn't forced to scroll past 400 lines of i18n bundle.
    tall = " tall" if body.count("\n") > 28 else ""
    return (
        f'<div class="block{tall}">'
        f'<div class="block-bar">{label}{run}{copy}</div>'
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
        self.block_ids: list[int] = []            # the cbN id each block was rendered with
        self.raw: list[str] = []                  # prose lines, for label sniffing
        self._spec: dict | None = ...             # parsed lazily, read many times

    # --- facts pulled out of the raw blocks -------------------------------
    # Everything here is observed, never assumed. A case that doesn't show its
    # curl simply has no method/path, and the card says "no request captured"
    # rather than inventing one.

    @property
    def shell(self) -> str:
        return "\n".join(t for l, t in self.blocks if l in COPYABLE)

    def request_block(self) -> tuple[int, str, str] | None:
        """(cbN id, lang, text) of the block this case tested.

        First shell block holding a curl. A case often ships a `docker exec …
        psql` block in the same section, and reading flags across both blobs is
        how you end up reporting the wrong method. Only this block earns a Run
        button and a live-vs-recorded comparison; a `psql` block has no HTTP
        status to compare and must never claim one.
        """
        for (lang, text), bid in zip(self.blocks, self.block_ids):
            if lang in COPYABLE and "curl" in text:
                return bid, lang, text
        return None

    def request_block_id(self) -> int | None:
        rb = self.request_block()
        return rb[0] if rb else None

    def request_spec(self) -> dict | None:
        """The parsed curl under test, or None if the case shows none.

        A case's shell block often opens with setup calls — fetching a CSRF
        token, grabbing a cookie — before the request being tested, so the
        *last* invocation in the block is the one that matters.
        """
        if self._spec is not ...:
            return self._spec
        self._spec = None
        rb = self.request_block()
        if rb is not None:
            cmds = curl_commands(rb[2])
            if cmds:
                self._spec = parse_curl(cmds[-1])
        return self._spec

    def request(self) -> tuple[str | None, str | None, str | None]:
        """(method, path, host) of the request under test, for the dashboard."""
        spec = self.request_spec()
        if not spec or not spec.get("url"):
            return None, None, None
        u = re.match(r"https?://([^/\s]+)(/[^\s]*)?", spec["url"])
        if not u:
            return spec["method"], None, spec["host"]
        # `-H "Host: tenant.localhost"` is the host that answered, whatever the
        # URL pointed at; that is exactly how this service routes tenants.
        return spec["method"], u.group(2) or "/", spec["host"] or u.group(1)

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


LIST_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
FENCE_RE = re.compile(r"^\s*```+\s*(\S*)\s*$")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")

# A step label is a bold lead-in that names what follows: `**Request:**`,
# `**DB after** (same table):`, `**Goal:** verify …`. Emphasis that merely
# opens a sentence (`**Nothing failed.** All good.`) has no colon and is prose.
STEP_RE = re.compile(r"^\s*\*\*(?P<label>[A-Za-z][^*\n]{0,38}?)\s*(?P<colon>:)?\*\*\s*(?P<rest>.*)$")

STEP_KINDS = (
    ("db-before", ("db before", "before")),
    ("db-after", ("db after", "after")),
    ("request", ("request", "curl")),
    ("response", ("response",)),
    ("goal", ("goal", "expect", "claim")),
)


def step_kind(label: str) -> str:
    low = label.lower()
    for kind, keys in STEP_KINDS:
        if any(k in low for k in keys):
            return kind
    return "other"


def is_step_label(m: "re.Match[str]") -> bool:
    rest = m.group("rest").strip()
    return bool(m.group("colon")) or rest.startswith(":") or (rest.endswith(":") and len(rest) < 80)


def render_list_tree(items: list[tuple[int, str, list[str]]]) -> str:
    """Nest list items by indent. `items` is (indent, 'ul'|'ol', text lines)."""
    out: list[str] = []
    stack: list[tuple[int, str]] = []

    def close_top() -> None:
        _, tag = stack.pop()
        out.append(f"</{tag}>")
        if stack:
            out.append("</li>")

    for indent, kind, text in items:
        while stack and indent < stack[-1][0]:
            close_top()
        if not stack:
            out.append(f"<{kind}>")
            stack.append((indent, kind))
        elif indent > stack[-1][0]:
            out.pop()                       # reopen the parent <li> and nest inside it
            out.append(f"<{kind}>")
            stack.append((indent, kind))
        elif stack[-1][1] != kind and len(stack) == 1:
            close_top()
            out.append(f"<{kind}>")
            stack.append((indent, kind))
        out.append(f"<li>{inline(' '.join(text))}")
        out.append("</li>")
    while stack:
        close_top()
    return "".join(out)


def parse_list(lines: list[str], i: int) -> tuple[str, int]:
    """Consume the list that starts at lines[i]; return its HTML and the next index.

    Real reports nest bullets two deep and wrap long items onto indented
    continuation lines. Reading those one line at a time split every wrapped
    item into a bullet plus a stray paragraph, and the paragraphs landed above
    the list they came from — the Summary read as if shuffled.
    """
    items: list[tuple[int, str, list[str]]] = []
    while i < len(lines):
        line = lines[i]
        m = LIST_RE.match(line)
        if m:
            kind = "ol" if m.group(2)[0].isdigit() else "ul"
            items.append((len(m.group(1).expandtabs(4)), kind, [m.group(3).strip()]))
            i += 1
            continue
        if line.strip() == "":
            j = i + 1
            while j < len(lines) and lines[j].strip() == "":
                j += 1
            nxt = lines[j] if j < len(lines) else ""
            if nxt and (LIST_RE.match(nxt) or (nxt[0].isspace() and not FENCE_RE.match(nxt))):
                i = j
                continue
            break
        if FENCE_RE.match(line) or HEADING_RE.match(line) or line.lstrip().startswith("|"):
            break
        items[-1][2].append(line.strip())   # lazy continuation of the last item
        i += 1
    return render_list_tree(items), i


def parse(md: str) -> tuple[str, list[Section]]:
    lines = md.split("\n")
    doc_title = "Test Run"
    sections: list[Section] = []
    current = Section(0, "")
    sections.append(current)

    i = block_id = 0
    para: list[str] = []
    table: list[str] = []
    step_open = False

    def flush_para():
        nonlocal para
        if para:
            current.html.append("<p>" + inline(" ".join(para)) + "</p>")
            para = []

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
        flush_para(); flush_table()

    # Steps: a case body is a sequence of labelled parts (Goal, DB before,
    # Request, Response, DB after, Result). Each label opens a step and
    # everything until the next label -- prose, blocks, lists -- lands inside
    # it, so the page can draw the case as a rail instead of a wall of blocks.
    def close_step():
        nonlocal step_open
        if step_open:
            current.html.append("</div></div>")
            step_open = False

    def absorb(i: int) -> tuple[str, int]:
        """The lines that continue a label or verdict line.

        `**Result:** PASS — 200 and a signed url carrying` wraps in the
        Markdown onto `the context path.` — one sentence, not a new paragraph,
        so it must not fall out of the step it belongs to.
        """
        extra: list[str] = []
        while i < len(lines):
            nxt = lines[i]
            sm = STEP_RE.match(nxt)
            if (nxt.strip() == "" or FENCE_RE.match(nxt) or HEADING_RE.match(nxt)
                    or LIST_RE.match(nxt) or nxt.lstrip().startswith("|")
                    or RESULT_RE.search(nxt) or (sm and is_step_label(sm))
                    or re.match(r"^\s*(---|\*\*\*|___)\s*$", nxt)):
                break
            extra.append(nxt.strip())
            current.raw.append(nxt)
            i += 1
        return " ".join(extra), i

    def open_step(kind: str, label: str, note: str):
        nonlocal step_open
        close_step()
        note_html = f'<span class="step-note">{inline(note)}</span>' if note else ""
        current.html.append(
            f'<div class="step step-{kind}"><div class="step-h">'
            f'<span class="step-l">{inline(label)}</span>{note_html}</div><div class="step-b">'
        )
        step_open = True

    while i < len(lines):
        line = lines[i]

        fence = FENCE_RE.match(line)
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
            # The model keeps the block exactly as the report wrote it: every
            # count, chip and endpoint on this page is derived from that, never
            # from the tidied-up view. Only what is drawn changes below.
            current.blocks.append((lang.lower(), raw))
            current.block_ids.append(block_id)
            shown_lang, shown = lang, raw
            sql = as_sql_block(raw) if lang.lower() in COPYABLE else None
            if sql:
                # `docker exec -i pg psql -U app -d t_x -c "…"` is plumbing.
                # The query is the evidence, so show that and label the database.
                shown_lang, shown = "sql", sql
            current.html.append(render_code(shown_lang, shown, block_id))
            continue

        current.raw.append(line)

        heading = HEADING_RE.match(line)
        if heading:
            flush_all()
            close_step()
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
            flush_para()
            table.append(line)
            i += 1
            continue
        flush_table()

        if LIST_RE.match(line):
            flush_para()
            start = i
            list_html, i = parse_list(lines, i)
            current.raw.extend(lines[start + 1:i])
            current.html.append(list_html)
            continue

        if line.strip() == "":
            flush_para()
            i += 1
            continue

        m = RESULT_RE.search(line)
        if m:
            flush_para()
            current.verdict = m.group(1).upper()
            # The verdict line is promoted into the card header, so drop the
            # duplicate from the body rather than saying it twice.
            rest = RESULT_RE.sub("", line).lstrip(" —-:").strip()
            more, i = absorb(i + 1)
            rest = f"{rest} {more}".strip()
            note = f'<p class="verdict-note">{inline(rest)}</p>' if rest else ""
            if current.level >= 3:
                close_step()
                v = current.verdict
                current.html.append(
                    f'<div class="step step-result"><div class="step-h">'
                    f'<span class="step-l">Result</span><span class="badge {v}">{v}</span></div>'
                    f'<div class="step-b">{note}</div></div>'
                )
            elif note:
                current.html.append(note)
            continue

        sm = STEP_RE.match(line) if (current.level >= 3 and not para) else None
        if sm and is_step_label(sm):
            flush_table()
            label = sm.group("label").strip()
            more, i = absorb(i + 1)
            note = f'{sm.group("rest").strip().lstrip(":").strip()} {more}'.strip()
            if note.endswith(":"):
                note = note[:-1].rstrip()
            open_step(step_kind(label), label, note)
            continue

        para.append(line.strip())
        i += 1

    flush_all()
    close_step()
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


def case_anchors(cases: list[Section]) -> list[tuple[str, str]]:
    """(label, DOM id) per case, so a link can point at the card that proves it.

    The label is the case's own number — `Test 37` gives `37` and `case-37` —
    because that is the handle the reader already has from the card and from the
    Markdown. A case that never numbered itself falls back to its position, and
    a number that somehow repeats gets a suffix instead of a duplicate id, which
    would quietly send two links to the same card.
    """
    out: list[tuple[str, str]] = []
    used: Counter = Counter()
    for i, c in enumerate(cases, 1):
        label = c.num() or str(i)
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", label).strip("-.") or str(i)
        used[slug] += 1
        if used[slug] > 1:
            slug = f"{slug}-{used[slug]}"
        out.append((label, f"case-{slug}"))
    return out


def case_hint(label: str, case: Section, area: str = "") -> dict[str, str]:
    """What a bare case number in the coverage table refuses to say by itself.

    A row of numbers is only scannable if the reader can tell which one is worth
    opening, and `41` tells them nothing. Every field here is read off the case
    the number points at — never guessed — so a case that showed no response
    simply carries no status and no error code, and the hint is shorter. `area`
    is the group the case sits in, empty on a report that has no groups.
    """
    st = case.status()
    return {
        "n": label,
        # Backticks are the Markdown for a code span; in a plain-text hint they
        # are just noise, so the title reads as prose.
        "t": case.short_title().replace("`", "").strip(),
        "v": case.verdict or "SKIP",
        "s": f"{st} {STATUS_TEXT.get(st, '')}".strip() if st is not None else "",
        "c": case.error_code() or "",
        "a": area,
    }


def case_hints(
    cases: list[Section],
    anchors: list[tuple[str, str]],
    area_of: dict[str, str] | None = None,
) -> dict[str, dict[str, str]]:
    """anchor -> the facts that case's number should whisper on hover.

    Built once and handed to every table that prints a case number, so the
    coverage rows and the area rows cannot describe the same case differently.
    """
    area_of = area_of or {}
    return {
        anchor: case_hint(label, c, area_of.get(anchor, ""))
        for (label, anchor), c in zip(anchors, cases)
    }


def hint_text(h: dict[str, str]) -> str:
    """The hint as one plain line, for `aria-label`.

    The styled tooltip is a hover affordance; a keyboard or screen-reader user
    reaches the same link and must get the same facts, so both are built from
    this one dict and cannot say different things.
    """
    out = f'Case {h["n"]}: {h["t"]} \u2014 {h["v"]}'
    if h.get("a"):
        out += f', in {h["a"]}'
    if h["s"]:
        out += f', HTTP {h["s"]}'
    if h["c"]:
        out += f', error {h["c"]}'
    return out


def case_link_cell(entries: list[tuple[str, str]], hints: dict[str, dict[str, str]]) -> str:
    """The case numbers behind one coverage row, each linked to its card.

    Each link also carries its case's hint: as `aria-label` for assistive tech
    and keyboard users, and as `data-*` for the tooltip the page draws on hover
    and focus. The native `title` attribute would have been less code and worse
    — a second of delay, no styling, and no way to colour the verdict.
    """
    def order(item: tuple[str, str]) -> tuple[int, int, str]:
        label = item[0]
        return (0, int(label), "") if label.isdigit() else (1, 0, label)

    def attr(name: str, value: str) -> str:
        return f' {name}="{html.escape(value, quote=True)}"' if value else ""

    parts: list[str] = []
    for label, anchor in sorted(dict.fromkeys(entries), key=order):
        h = hints.get(anchor)
        hint = ""
        if h:
            hint = (
                attr("aria-label", hint_text(h))
                + attr("data-n", h["n"]) + attr("data-t", h["t"])
                + attr("data-v", h["v"]) + attr("data-s", h["s"]) + attr("data-c", h["c"])
                + attr("data-a", h.get("a", ""))
            )
        parts.append(f'<a href="#{html.escape(anchor, quote=True)}"{hint}>{esc(label)}</a>')
    return f'<td class="cases">{", ".join(parts) or "&mdash;"}</td>'


def coverage_table(
    cases: list[Section],
    anchors: list[tuple[str, str]],
    hints: dict[str, dict[str, str]] | None = None,
) -> str:
    """One row per endpoint actually exercised, with every status it returned.

    This is the answer to "what did this run actually cover?" — a question the
    Markdown can only answer by reading all twelve cases. Each row also carries
    the case numbers behind it, linked to the cards, so the next question —
    "which case saw that 409?" — is a click instead of a text search. Hovering
    or tabbing to a number answers the question before the click.

    `anchors` is the same list build() gives the cards, so a link and the id it
    points at cannot drift apart.
    """
    rows: "OrderedDict[tuple[str, str], dict]" = OrderedDict()
    # A case whose curl never parsed has no endpoint to file under. It still
    # ran, so it gets a row of its own rather than vanishing from the coverage
    # it is part of.
    stray: dict = {"statuses": [], "verdicts": Counter(), "cases": []}
    if hints is None:
        hints = case_hints(cases, anchors)
    for (label, anchor), c in zip(anchors, cases):
        method, path, _ = c.request()
        if path:
            key = (method or "GET", normalize_path(path))
            entry = rows.setdefault(key, {"statuses": [], "verdicts": Counter(), "cases": []})
        else:
            entry = stray
        st = c.status()
        if st is not None and st not in entry["statuses"]:
            entry["statuses"].append(st)
        entry["verdicts"][c.verdict or "SKIP"] += 1
        entry["cases"].append((label, anchor))

    if not rows and not stray["cases"]:
        return ""

    def row(method_cell: str, path_cell: str, entry: dict) -> str:
        chips = "".join(
            f'<span class="pill {STATUS_CLASS.get(str(s)[0], "s-ok")}">{s}</span>'
            for s in sorted(entry["statuses"])
        )
        v = entry["verdicts"]
        verdict = "FAIL" if v["FAIL"] else ("PASS" if v["PASS"] else "SKIP")
        return (
            f"<tr>"
            f"<td>{method_cell}</td>"
            f"{path_cell}"
            f'<td>{chips or "&mdash;"}</td>'
            f'<td class="num">{sum(v.values())}</td>'
            f"{case_link_cell(entry['cases'], hints)}"
            f'<td><span class="badge {verdict}">{verdict}</span></td>'
            f"</tr>"
        )

    body: list[str] = [
        row(
            f'<span class="pill {method_class(method)}">{esc(method)}</span>',
            f'<td class="mono">{esc(path)}</td>',
            entry,
        )
        for (method, path), entry in rows.items()
    ]
    if stray["cases"]:
        body.append(row(
            '<span class="pill m-any">&mdash;</span>',
            '<td class="no-ep">no request captured</td>',
            stray,
        ))
    return (
        '<table class="cov"><thead><tr>'
        "<th>Method</th><th>Path</th><th>Statuses seen</th><th>Cases</th>"
        "<th>Case numbers</th><th>Verdict</th>"
        "</tr></thead><tbody>" + "".join(body) + "</tbody></table>"
    )


# --------------------------------------------------------------------------
# areas: the grouping the report gave its own cases
# --------------------------------------------------------------------------

# `## Details — Signup validation` names an area. A plain `## Details` names
# nothing: that is the flat shape a short run should keep, and it stays flat.
AREA_RE = re.compile(r"^details\s*(?:--|[\u2014\u2013:|-])\s*(?P<name>\S.*)$", re.I)


def section_area(title: str) -> str | None:
    """The area named by a `## Details — <area>` heading, or None."""
    m = AREA_RE.match(title.strip())
    return m.group("name").strip() if m else None


class Area:
    """One slice of a run: a title, the cases under it, and its own verdict."""

    def __init__(self, name: str, slug: str, intro: str = ""):
        self.name = name
        self.slug = slug
        self.intro = intro                                # the heading's own prose
        self.items: list[tuple[str, str, Section]] = []   # (label, anchor, case)

    @property
    def anchor(self) -> str:
        return f"area-{self.slug}"

    def counts(self) -> "OrderedDict[str, int]":
        v = [c.verdict or "SKIP" for _, _, c in self.items]
        return OrderedDict((k, v.count(k)) for k in ("PASS", "FAIL", "SKIP"))

    def failing(self) -> list[tuple[str, str]]:
        return [(l, a) for l, a, c in self.items if (c.verdict or "SKIP") == "FAIL"]

    def span(self) -> str:
        """`17–36`, or just `17` when the area holds one case.

        Read off the case labels themselves rather than assumed, so an area
        whose numbers are not contiguous says so instead of being tidied up.
        """
        if not self.items:
            return ""
        first, last = self.items[0][0], self.items[-1][0]
        return first if first == last else f"{first}\u2013{last}"


def group_cases(
    sections: list[Section],
    cases: list[Section],
    anchors: list[tuple[str, str]],
) -> list[Area]:
    """The areas this report grouped its cases into, in report order.

    Returns `[]` unless the report really is grouped: two or more
    `## Details — <area>` headings with not one case left outside them. That is
    the whole degradation story. A flat report — one `## Details`, or a report
    written before grouping existed — gets an empty list, and every area-aware
    part of the page then draws nothing at all, rather than inventing a single
    group called "Details" and charging the reader for it.
    """
    placed = {id(c): (label, anchor) for (label, anchor), c in zip(anchors, cases)}
    found: list[Area] = []
    used: Counter = Counter()
    current: Area | None = None
    for s in sections:
        if s.level == 2:
            name = section_area(s.title)
            if not name:
                current = None
                continue
            slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or str(len(found) + 1)
            used[slug] += 1
            if used[slug] > 1:
                slug = f"{slug}-{used[slug]}"
            current = Area(name, slug, "".join(s.html))
            found.append(current)
            continue
        if id(s) in placed:
            if current is None:
                return []           # a case under no area at all: not grouped
            label, anchor = placed[id(s)]
            current.items.append((label, anchor, s))
    found = [a for a in found if a.items]
    return found if len(found) >= 2 else []


def area_bar(counts: "OrderedDict[str, int]") -> str:
    """A stacked sliver: how much of one area held, and how much did not."""
    total = sum(counts.values()) or 1
    segs = "".join(
        f'<span class="ab {k}" style="width:{100 * n / total:.2f}%"></span>'
        for k, n in counts.items() if n
    )
    return f'<span class="abar" aria-hidden="true">{segs}</span>'


def area_panel(areas: list[Area], hints: dict[str, dict[str, str]]) -> str:
    """Per-area pass/fail in the dashboard: the map for a long run.

    One row per area, carrying the case range it covers, how many cases that
    is, how the split fell, and which numbers failed — the same four facts the
    Markdown's `## Navigation` list states, but read here off the cases
    themselves, so the two cannot drift apart. The failing numbers are the same
    linked, hint-carrying cells the coverage table uses, so a reader can go
    from "this area is red" to the card that says why in one click.
    """
    if not areas:
        return ""
    rows: list[str] = []
    for a in areas:
        c = a.counts()
        split = [f'<b class="ok">{c["PASS"]} pass</b>']
        if c["FAIL"]:
            split.append(f'<b class="err">{c["FAIL"]} fail</b>')
        if c["SKIP"]:
            split.append(f'<b class="warn">{c["SKIP"]} skip</b>')
        rows.append(
            "<tr>"
            f'<td><a href="#{html.escape(a.anchor, quote=True)}">{esc(a.name)}</a></td>'
            f'<td class="mono">{esc(a.span())}</td>'
            f'<td class="num">{sum(c.values())}</td>'
            f'<td class="split">{area_bar(c)}<span>{" &middot; ".join(split)}</span></td>'
            f"{case_link_cell(a.failing(), hints)}"
            "</tr>"
        )
    return (
        '<div class="panel"><h2>Areas</h2>'
        '<table class="cov areas"><thead><tr>'
        "<th>Area</th><th>Cases</th><th>Count</th><th>Result</th><th>Failing</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"
    )


def area_head(area: Area) -> str:
    """The divider that opens an area inside the case list.

    It carries its own failure count, because the Only-failures filter hides
    cards and a heading left standing over nothing reads as a broken page.
    """
    c = area.counts()
    total = sum(c.values())
    bits = [f"cases {area.span()}", f'{total} case{"" if total == 1 else "s"}',
            f'{c["PASS"]} pass']
    if c["FAIL"]:
        bits.append(f'{c["FAIL"]} fail')
    if c["SKIP"]:
        bits.append(f'{c["SKIP"]} skip')
    lead = f'<div class="prose area-lead">{area.intro}</div>' if area.intro.strip() else ""
    return (
        f'<section class="area-head{" bad" if c["FAIL"] else ""}" '
        f'id="{html.escape(area.anchor, quote=True)}" data-fails="{c["FAIL"]}">'
        f"<h2>{esc(area.name)}</h2>"
        f'<p class="area-m">{esc(" \u00b7 ".join(bits))}</p>{lead}</section>'
    )


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


def run_panel(runnable: int, blocked: int) -> str:
    """The one place the page is honest about what a Run button can reach.

    Everything stated here was checked against the running service, not assumed:
    the API answers no CORS preflight and sends no `Access-Control-Allow-Origin`,
    the session is an `HttpOnly; Secure` cookie and the token resolver ignores
    `Authorization`, and tenants are routed by `Host` — which `fetch` refuses to
    set. Each of those decides something the reader can see below, so each is
    said once, plainly, instead of being discovered one failed click at a time.
    """
    if not runnable and not blocked:
        return ""
    counted = f'{runnable} request{"" if runnable == 1 else "s"} in this report can be replayed'
    if blocked:
        counted += (f', {blocked} cannot and say why')
    return (
        '<div class="panel runp" id="runp">'
        '<h2>Run a request from this page</h2>'
        '<div class="runp-row">'
        '<label for="run-origin">Base origin</label>'
        '<input id="run-origin" type="url" spellcheck="false" autocomplete="off"'
        ' placeholder="http://localhost:8080" value="http://localhost:8080">'
        '<span class="runp-note" id="run-origin-note"></span></div>'
        f'<p class="runp-lead">{counted}. A Run fires the real request at that service '
        'from your browser &mdash; no proxy, no helper process, nothing to install. '
        'The recorded request, response and DB evidence on this page are never '
        'touched by it; a live result is drawn underneath, in its own frame.</p>'
        '<ul class="runp-facts">'
        '<li><b>The Host comes from the URL.</b> A page cannot set the <code>Host</code> '
        'header, and this service routes tenants by it. So a curl written with '
        '<code>-H "Host: tenant.localhost"</code> is replayed against '
        '<code>http://tenant.localhost:8080</code> instead &mdash; the scheme and port '
        'from the field above, the host from the header.</li>'
        '<li><b>Your session is a cookie, not a token.</b> The token resolver reads the '
        '<code>st_access</code> cookie and ignores <code>Authorization: Bearer</code>, '
        'so there is no token to paste here and no field asking for one. The cookie is '
        '<code>HttpOnly; Secure</code>, so it rides along only when this page is served '
        'from the very origin the request goes to. Anything needing auth will answer '
        '401 otherwise, and the panel will say so.</li>'
        '<li><b>Opened from <code>file://</code>, every Run will fail.</b> This API '
        'answers no CORS preflight and returns no '
        '<code>Access-Control-Allow-Origin</code>, so the browser blocks the reply '
        'before your code sees it. That is not a bug in this page. Serve the report '
        'from the app\'s own origin, or use <b>Copy as fetch snippet</b> on any block '
        'and paste it into DevTools on that origin.</li>'
        '<li><b>Writes are real.</b> A replayed PUT or DELETE changes the same rows the '
        'recorded run changed. Read the block before you run it.</li>'
        '</ul></div>'
    )

# ==========================================================================
# page
# ==========================================================================

FONT_DIR = Path(__file__).resolve().parent / "fonts"
FONT_FILES = (("JetBrainsMono-Regular.woff2", 400), ("JetBrainsMono-Bold.woff2", 700))


def font_css(font_dir: Path = FONT_DIR) -> str:
    """JetBrains Mono as inline @font-face rules, or nothing if the files are gone.

    Base64 in the page keeps the report one offline file; a CDN link would be a
    report that renders differently on a plane. A missing font file is not an
    error — the stack simply falls through to the system monospace.
    """
    rules: list[str] = []
    for name, weight in FONT_FILES:
        path = font_dir / name
        if not path.is_file():
            continue
        b64 = base64.b64encode(path.read_bytes()).decode("ascii")
        rules.append(
            f'@font-face{{font-family:"JetBrains Mono";font-style:normal;font-weight:{weight};'
            f'font-display:swap;src:url(data:font/woff2;base64,{b64}) format("woff2")}}'
        )
    return "\n".join(rules)


def headline(
    counts: "OrderedDict[str, int]",
    failing: list[tuple[str, str]],
    endpoints: int,
    db_checks: int,
    n_codes: int,
) -> str:
    """One sentence that says how the run went, before any chart is read.

    `failing` is (label, anchor) per failed case, in report order, so the
    sentence can point at the first one and list the rest as links.
    """
    total = sum(counts.values())
    p, f, sk = counts.get("PASS", 0), counts.get("FAIL", 0), counts.get("SKIP", 0)

    def n(count: int, word: str) -> str:
        return f"{count} {word}{'' if count == 1 else 's'}"

    if not total:
        mood, head = "none", "No test cases found in this report."
    elif f:
        label, anchor = failing[0]
        head = (
            f'{f} of {n(total, "case")} failed &mdash; start with '
            f'<a href="#{html.escape(anchor, quote=True)}">case {esc(label)}</a>.'
        )
        mood = "bad"
    elif sk:
        mood, head = "mid", f"{p} of {n(total, 'case')} passed, {sk} skipped."
    elif total == 1:
        mood, head = "good", "The only case passed."
    else:
        mood, head = "good", f"All {total} cases passed."

    sub = " &middot; ".join((n(endpoints, "endpoint"), n(db_checks, "DB check"),
                             f"{n(n_codes, 'status code')} seen"))
    more = ""
    if f > 1:
        links = ", ".join(
            f'<a href="#{html.escape(a, quote=True)}">{esc(l)}</a>' for l, a in failing
        )
        more = f'<p class="story-f">Failing cases: {links}</p>'
    return (
        f'<section class="story {mood}"><p class="story-h">{head}</p>'
        f'<p class="story-s">{sub}</p>{more}</section>'
    )


CSS = """
:root{
--bg:#eef0f4;--panel:#fff;--panel2:#f3f5f8;--line:#e0e4ea;--fg:#1b1e26;--muted:#697080;
--ok:oklch(.55 .13 155);--err:oklch(.55 .17 27);--warn:oklch(.58 .12 80);--info:oklch(.5 .13 255);
--violet:oklch(.52 .14 300);--pink:oklch(.55 .15 340);
--ok-soft:oklch(.95 .03 155);--err-soft:oklch(.95 .03 27);--warn-soft:oklch(.96 .035 90);
--code:#f6f7f9;--shadow:0 1px 2px rgba(20,26,40,.05),0 6px 20px -8px rgba(20,26,40,.12);
--mono:"JetBrains Mono",ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
--sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;}
html[data-theme=dark]{
--bg:#0b0d12;--panel:#13161d;--panel2:#1a1e28;--line:#262c38;--fg:#e9edf4;--muted:#98a2b3;
--ok:#3fb950;--err:#f85149;--warn:#e3b341;--info:#58a6ff;--violet:#bc8cff;--pink:#f778ba;
--ok-soft:rgba(63,185,80,.13);--err-soft:rgba(248,81,73,.13);--warn-soft:rgba(227,179,65,.13);
--code:#0a0c10;--shadow:0 1px 1px rgba(0,0,0,.35),0 2px 6px rgba(0,0,0,.25),0 12px 32px -12px rgba(0,0,0,.55);}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--fg);-webkit-font-smoothing:antialiased;
font:14.5px/1.6 var(--sans)}
::selection{background:color-mix(in srgb,var(--info) 25%,transparent)}
.mono,code,pre,kbd{font-family:var(--mono)}
a{color:var(--info)}
.layout{max-width:1440px;margin:0 auto;position:relative;
display:grid;grid-template-columns:var(--rail-w,252px) minmax(0,1fr)}
.wrap{max-width:1140px;margin:0 auto;padding:0 24px 100px;min-width:0;width:100%}

/* ---- case rail: a persistent index down the left side ---- */
.rail{position:sticky;top:0;height:100vh;overflow-y:auto;padding:24px 10px 24px 20px;
border-right:1px solid var(--line);scrollbar-width:thin;scrollbar-color:var(--line) transparent}
.rail:empty{display:none}
/* The grip sits on the rail's border and spans the whole page, so the reader
   can grab it wherever they have scrolled to. */
.grip{position:absolute;top:0;bottom:0;left:var(--rail-w,252px);width:11px;margin-left:-6px;
z-index:60;cursor:col-resize;touch-action:none;background:none;border:0;padding:0}
.grip::before{content:"";position:absolute;top:0;bottom:0;left:5px;width:1px;background:none;
transition:background .12s}
.grip:hover::before,.grip:focus-visible::before,.grip.drag::before{background:var(--info)}
.grip:focus-visible{outline:none}
.rail:empty+.grip{display:none}
html.resizing{cursor:col-resize;-webkit-user-select:none;user-select:none}
@media(max-width:1020px){.layout{display:block}.rail,.grip{display:none}}
.rail-top{display:block;font:700 10px/1 var(--mono);letter-spacing:.2em;text-transform:uppercase;
color:var(--muted);margin:8px 0 12px;padding-left:8px}
.rail-h{display:block;font:700 10px/1.5 var(--mono);letter-spacing:.14em;text-transform:uppercase;
color:var(--muted);margin:16px 0 4px;padding-left:8px}
.rl{display:flex;gap:8px;align-items:baseline;padding:5px 8px;border-radius:8px;color:var(--fg);
text-decoration:none;font:12px/1.5 var(--sans)}
.rl:hover{background:var(--panel);box-shadow:var(--shadow)}
.rl b{font:700 10.5px/1.6 var(--mono);color:var(--muted);min-width:18px;text-align:right}
.rl span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
.rl.FAIL{color:var(--err);font-weight:600}
.rd{width:7px;height:7px;border-radius:50%;flex:none;position:relative;top:-1px}
.rd.PASS{background:var(--ok)}.rd.FAIL{background:var(--err)}.rd.SKIP{background:var(--warn)}
.rl.on{background:var(--panel);box-shadow:var(--shadow)}
.rl.on b,.rl.on span{color:var(--fg)}
.rl.FAIL.on span{color:var(--err)}

/* ---- masthead ---- */
.mast{padding:30px 0 18px;border-bottom:1px solid var(--line);margin-bottom:20px;
display:flex;gap:20px;align-items:flex-start;flex-wrap:wrap}
.mast h1{font:700 20px/1.35 var(--mono);margin:0 0 8px;letter-spacing:-.02em;text-wrap:pretty}
.mast .meta{color:var(--muted);font:12px/1.6 var(--mono);display:flex;gap:16px;flex-wrap:wrap}
.mast .meta span{display:inline-flex;gap:6px;align-items:center}
.mast .meta code{color:var(--fg)}
.grow{flex:1 1 320px}
.theme{font:12px var(--mono);padding:6px 14px;border-radius:999px;cursor:pointer;
border:1px solid var(--line);background:var(--panel);color:var(--muted);box-shadow:var(--shadow)}
.theme:hover{color:var(--fg)}

/* ---- story: the one sentence that answers "how did it go?" ---- */
.story{position:relative;margin:0 0 16px;padding:24px 28px 22px;border-radius:18px;
border:1px solid var(--line);background:var(--panel);box-shadow:var(--shadow)}
.story.good{background:linear-gradient(90deg,var(--ok-soft),var(--panel) 55%)}
.story.bad{background:linear-gradient(90deg,var(--err-soft),var(--panel) 55%)}
.story.mid{background:linear-gradient(90deg,var(--warn-soft),var(--panel) 55%)}
.story-h{margin:0;font:700 27px/1.3 var(--sans);letter-spacing:-.03em;text-wrap:balance}
.story.good .story-h{color:var(--ok)} .story.bad .story-h{color:var(--err)} .story.mid .story-h{color:var(--warn)}
.story-h a{color:inherit;text-decoration:underline;text-decoration-thickness:2px;text-underline-offset:4px}
.story-s{margin:9px 0 0;color:var(--muted);font:12.5px/1.6 var(--mono)}
.story-f{margin:12px 0 0;font-size:13px;color:var(--muted);display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.story-f a{font:700 12px var(--mono);color:var(--err);text-decoration:none;border-radius:999px;
padding:3px 12px;background:color-mix(in srgb,var(--err) 10%,transparent)}

/* ---- dashboard ---- */
.dash{display:grid;grid-template-columns:210px 1fr;gap:16px;margin-bottom:14px}
@media(max-width:820px){.dash{grid-template-columns:1fr}}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:16px;
padding:18px 20px;box-shadow:var(--shadow);margin-bottom:14px}
.dash .panel{margin-bottom:0}
.panel h2{font:700 11px/1.4 var(--mono);letter-spacing:.14em;text-transform:uppercase;color:var(--muted);
margin:0 0 12px}
.donut-wrap{display:flex;align-items:center;justify-content:center;padding:2px}
.donut{width:152px;height:152px}
.donut .d-num{font:700 26px/1 var(--mono);fill:var(--fg);text-anchor:middle}
.donut.good .d-num{fill:var(--ok)} .donut.bad .d-num{fill:var(--err)} .donut.mid .d-num{fill:var(--warn)}
.donut .d-lab{font:500 10.5px/1 var(--mono);fill:var(--muted);text-anchor:middle}
.donut .seg{transition:opacity .15s}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(112px,1fr));gap:10px;margin-bottom:14px}
.stat{background:var(--panel2);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
.stat b{display:block;font:700 24px/1.2 var(--mono);letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.stat span{font:10.5px var(--mono);color:var(--muted);letter-spacing:.1em;text-transform:uppercase}
.stat.ok b{color:var(--ok)} .stat.err b{color:var(--err)} .stat.warn b{color:var(--warn)}

/* ---- bar chart ---- */
.bars{display:flex;flex-direction:column;gap:7px}
.bar-row{display:grid;grid-template-columns:44px 1fr 26px auto;gap:10px;align-items:center;font:12.5px var(--mono)}
.bar-key{font-weight:700;text-align:right}
.bar-track{height:10px;border-radius:999px;background:var(--bg);overflow:hidden}
.bar-fill{display:block;height:100%;border-radius:999px}
.bar-fill.s-ok{background:var(--ok)} .bar-fill.s-info{background:var(--info)}
.bar-fill.s-warn{background:var(--warn)} .bar-fill.s-err{background:var(--err)}
.bar-val{color:var(--muted);font-variant-numeric:tabular-nums}
.bar-note{color:var(--muted);font:11.5px var(--sans)}

/* ---- pills, badges ---- */
.pill{display:inline-block;padding:2px 9px;border-radius:999px;font:700 11px/1.7 var(--mono);
letter-spacing:.02em;background:color-mix(in srgb,currentColor 11%,transparent)}
.m-get{color:var(--info)} .m-post{color:var(--ok)} .m-put{color:var(--warn)}
.m-patch{color:var(--violet)} .m-delete{color:var(--err)} .m-any{color:var(--muted)}
.pill.s-ok{color:var(--ok)} .pill.s-info{color:var(--info)}
.pill.s-warn{color:var(--warn)} .pill.s-err{color:var(--err)}
.badge{display:inline-block;font:700 10px/1.9 var(--mono);
letter-spacing:.1em;padding:1px 10px;border-radius:999px;color:#fff}
.badge.PASS{background:var(--ok)} .badge.FAIL{background:var(--err)} .badge.SKIP{background:var(--warn)}

/* ---- coverage table ---- */
table{width:100%;border-collapse:collapse;font-size:13.5px;margin:8px 0}
th{text-align:left;font:700 10px var(--mono);letter-spacing:.14em;text-transform:uppercase;
color:var(--muted);padding:6px 10px;border-bottom:1px solid var(--line)}
td{padding:9px 10px;border-bottom:1px solid var(--line);vertical-align:middle}
tbody tr:last-child td{border-bottom:0}
tbody tr:hover{background:var(--panel2)}
td.num{color:var(--muted);font-family:var(--mono);font-variant-numeric:tabular-nums}
td.mono{font-size:12.5px}
.cov td:nth-child(3) .pill{margin-right:4px}
.cov td.cases{font:12.5px/1.75 var(--mono)}
.cov td.cases a{text-decoration:none;border-bottom:1px solid transparent}
.cov td.cases a:hover,.cov td.cases a:focus{border-bottom-color:currentColor}
.cov td.no-ep{color:var(--muted);font-style:italic}

/* ---- areas: the run's own grouping, drawn only when it has one ---- */
.cov.areas td:first-child a{text-decoration:none;font-weight:600}
.cov.areas td:first-child a:hover,.cov.areas td:first-child a:focus{text-decoration:underline}
td.split{white-space:nowrap}
td.split>span{font:12px var(--mono);color:var(--muted);margin-left:10px}
td.split b{font-weight:700}
td.split b.ok{color:var(--ok)} td.split b.err{color:var(--err)} td.split b.warn{color:var(--warn)}
.abar{display:inline-block;vertical-align:middle;width:92px;height:8px;border-radius:999px;
overflow:hidden;background:var(--bg);font-size:0;white-space:nowrap}
.abar .ab{display:inline-block;height:100%;vertical-align:top}
.abar .ab.PASS{background:var(--ok)} .abar .ab.FAIL{background:var(--err)}
.abar .ab.SKIP{background:var(--warn)}
.area-head{margin:30px 0 8px;padding:14px 18px 12px;border:1px solid var(--line);
border-radius:14px;background:var(--panel);box-shadow:var(--shadow);scroll-margin-top:76px}
.area-head.bad{background:linear-gradient(90deg,var(--err-soft),var(--panel) 55%)}
.area-head:target{border-color:var(--info);box-shadow:0 0 0 2px var(--info),var(--shadow)}
.area-head h2{margin:0;font:700 16px/1.35 var(--sans);letter-spacing:-.02em}
.area-m{margin:5px 0 0;font:11.5px/1.5 var(--mono);color:var(--muted)}
.area-lead{margin-top:7px;color:var(--muted);font-size:13px}
.area-lead p{margin:6px 0}

/* ---- case-number hint ----
   One shared tooltip, parented to <body> and positioned against the viewport,
   so no table or panel can crop it. */
#tip{position:fixed;left:0;top:0;z-index:90;max-width:340px;padding:10px 12px;
background:var(--panel);border:1px solid var(--line);border-radius:12px;box-shadow:var(--shadow);
font-size:12.5px;line-height:1.5;pointer-events:none;visibility:hidden;opacity:0;
transition:opacity .1s ease-out}
#tip.on{visibility:visible;opacity:1}
#tip .tip-h{display:flex;gap:8px;align-items:center;margin-bottom:5px}
#tip .tip-n{font:700 11px/1 var(--mono);color:var(--muted);background:var(--panel2);
border:1px solid var(--line);border-radius:6px;padding:4px 6px}
#tip .tip-t{font-weight:600;overflow-wrap:anywhere;word-break:break-word}
#tip .tip-m{color:var(--muted);font:11.5px/1.5 var(--mono);margin-top:7px}
#tip .tip-m:empty{display:none}
#tip .tip-a{color:var(--muted);font:10.5px/1.5 var(--mono);margin-top:6px;
letter-spacing:.09em;text-transform:uppercase}
#tip .tip-a:empty{display:none}

/* ---- prose panels ---- */
.prose p{margin:9px 0} .prose ul,.prose ol{margin:9px 0;padding-left:22px} .prose li{margin:5px 0}
.prose li>ul,.prose li>ol{margin:5px 0 2px}
.prose li::marker{color:var(--muted)}
.prose code{background:var(--code);border:1px solid var(--line);padding:1px 5px;border-radius:5px;font-size:12px}
.prose strong{color:var(--fg)}
details.fold>summary{cursor:pointer;list-style:none;font:700 11px/1.4 var(--mono);letter-spacing:.14em;
text-transform:uppercase;color:var(--muted);display:flex;gap:8px;align-items:center}
details.fold>summary::-webkit-details-marker{display:none}
details.fold>summary::after{content:"show";font-size:10px;letter-spacing:.06em;color:var(--info)}
details.fold[open]>summary::after{content:"hide"}
details.fold[open]>summary{margin-bottom:12px}

/* ---- sticky mini-bar ---- */
.minibar{position:sticky;top:10px;z-index:50;display:flex;align-items:center;gap:12px;flex-wrap:wrap;
margin:26px 0 12px;padding:10px 16px;border:1px solid var(--line);border-radius:14px;
background:color-mix(in srgb,var(--panel) 85%,transparent);
backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);box-shadow:var(--shadow)}
.minibar h2{font:700 14px/1.3 var(--sans);margin:0;letter-spacing:-.01em}
.mb-count{font:12px var(--mono);color:var(--muted)}
.mb-count b{color:var(--fg)} .mb-count b.ok{color:var(--ok)} .mb-count b.err{color:var(--err)}
.mb-count b.warn{color:var(--warn)}
.mb-tools{margin-left:auto;display:flex;gap:7px;flex-wrap:wrap;align-items:center}
.mb-tools button,.mb-tools select{font:12px var(--mono);padding:5px 13px;border-radius:999px;cursor:pointer;
border:1px solid var(--line);background:var(--panel);color:var(--muted);max-width:280px}
.mb-tools button:hover,.mb-tools select:hover{color:var(--fg);border-color:var(--muted)}
.mb-tools button.on{background:var(--err);border-color:var(--err);color:#fff}

/* ---- case cards ---- */
details.case{background:var(--panel);border:1px solid var(--line);
border-radius:16px;margin:11px 0;overflow:hidden;box-shadow:var(--shadow);scroll-margin-top:70px}
details.case:target{border-color:var(--info);box-shadow:0 0 0 2px var(--info),var(--shadow)}
details.case[data-verdict=FAIL]{border-color:color-mix(in srgb,var(--err) 40%,var(--line))}
details.case[data-verdict=FAIL]>summary{background:linear-gradient(90deg,var(--err-soft),transparent 60%)}
details.case>summary{cursor:pointer;padding:14px 18px;list-style:none;display:flex;
gap:11px;align-items:center;flex-wrap:wrap}
details.case>summary::-webkit-details-marker{display:none}
details.case>summary::before{content:"\\25B8";color:var(--muted);font-size:11px;transition:transform .15s}
details.case[open]>summary::before{transform:rotate(90deg)}
details.case>summary:hover{background:var(--panel2)}
details.case[data-verdict=FAIL]>summary:hover{background:var(--err-soft)}
.case-n{font:700 11px/1 var(--mono);color:var(--muted);background:var(--panel2);
border:1px solid var(--line);border-radius:7px;padding:5px 7px;min-width:27px;text-align:center}
.case-t{font-weight:600;flex:1 1 260px;font-size:14.5px;letter-spacing:-.01em}
.case-t code{background:var(--code);border:1px solid var(--line);padding:1px 5px;border-radius:5px;font-size:12px;font-weight:500}
.case-body{padding:0 18px 20px;border-top:1px solid var(--line);counter-reset:step}
.case-body>p{margin:12px 0}
.verdict-note{color:var(--fg);font-size:13.5px;margin:6px 0 0}

/* ---- steps: the case as a rail, one numbered stop per labelled part ---- */
.step{position:relative;padding:0 0 4px 42px}
.step::before{content:"";position:absolute;left:13px;top:36px;bottom:-4px;width:2px;background:var(--line)}
.step:last-child::before{display:none}
.step-h{position:relative;display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;padding-top:14px;min-height:42px}
.step-h::before{counter-increment:step;content:counter(step);position:absolute;left:-42px;top:12px;
width:28px;height:28px;border-radius:50%;display:flex;align-items:center;justify-content:center;
font:700 12px/1 var(--mono);background:var(--panel2);border:2px solid var(--line);color:var(--muted)}
.step-l{font:700 10.5px/1.6 var(--mono);letter-spacing:.14em;text-transform:uppercase;color:var(--muted)}
.step-goal .step-l{color:var(--info)}
.step-db-before .step-l,.step-db-after .step-l{color:var(--violet)}
.step-request .step-l,.step-response .step-l{color:var(--fg)}
.step-note{font-size:13.5px;color:var(--fg)}
.step-goal .step-note{font-size:14px;line-height:1.55}
.step-b>:first-child{margin-top:6px}
.step-b p{margin:8px 0}
.step-b ul,.step-b ol{margin:8px 0;padding-left:20px}
.step-b .block{margin:8px 0 10px}
.step-result .step-h::before{counter-increment:none;color:#fff}
.step-result[data-verdict=PASS] .step-h::before{content:"\\2713";background:var(--ok);border-color:var(--ok)}
.step-result[data-verdict=FAIL] .step-h::before{content:"\\2717";background:var(--err);border-color:var(--err)}
.step-result[data-verdict=SKIP] .step-h::before{content:"\\2013";background:var(--warn);border-color:var(--warn)}
.step-result .step-b:empty{display:none}

/* ---- flow strip ---- */
.flow{display:flex;align-items:center;gap:9px;flex-wrap:wrap;padding:12px 0 2px;font-size:12.5px}
.flow.empty{color:var(--muted);font-style:italic}
.flow-node{display:inline-flex;align-items:center;gap:7px;background:var(--panel2);
border:1px solid var(--line);border-radius:999px;padding:4px 12px}
.flow-arrow{color:var(--muted)}
.flow-sub,.flow-host{color:var(--muted);font-size:11.5px}
.flow-db{font:700 11px/1 var(--mono);color:var(--violet);background:color-mix(in srgb,var(--violet) 11%,transparent);
border-radius:5px;padding:3px 7px}

/* ---- code blocks ---- */
.block{margin:12px 0;border:1px solid var(--line);border-radius:12px;overflow:hidden;background:var(--code)}
.block-bar{display:flex;flex-wrap:wrap;align-items:center;gap:8px;padding:5px 12px;min-height:30px;
border-bottom:1px solid var(--line);background:color-mix(in srgb,var(--panel2) 65%,transparent)}
.lang{font:700 10px/1.4 var(--mono);letter-spacing:.14em;text-transform:uppercase;color:var(--muted)}
.http-status{font:700 11px/1.7 var(--mono);padding:1px 9px;border-radius:999px;
background:color-mix(in srgb,currentColor 11%,transparent);letter-spacing:.02em}
details.hdrs{border-bottom:1px solid var(--line)}
details.hdrs>summary{cursor:pointer;list-style:none;padding:6px 15px;font:600 11px/1.6 var(--mono);
color:var(--muted);letter-spacing:.05em}
details.hdrs>summary::-webkit-details-marker{display:none}
details.hdrs>summary::before{content:"\\25B8";display:inline-block;margin-right:7px;transition:transform .15s}
details.hdrs[open]>summary::before{transform:rotate(90deg)}
details.hdrs>summary:hover{color:var(--fg)}
details.hdrs pre{padding-top:4px}
.http-empty{padding:12px 15px;color:var(--muted);font:italic 12.5px var(--mono)}
.copy,.unfold{margin-left:auto;font:11px var(--mono);padding:2px 10px;border-radius:999px;
cursor:pointer;border:1px solid var(--line);background:var(--panel);color:var(--muted)}
.copy:hover,.unfold:hover{color:var(--fg)}
.copy.done{color:var(--ok);border-color:var(--ok)}
.unfold{display:block;width:100%;margin:0;border:0;border-top:1px solid var(--line);
border-radius:0;padding:6px;text-align:center}
.block.tall pre{max-height:340px;overflow:auto}
.block.tall.open pre{max-height:none}
pre{margin:0;padding:13px 15px;overflow-x:auto;scrollbar-width:thin;scrollbar-color:var(--line) transparent}
pre::-webkit-scrollbar{height:8px;width:8px}
pre::-webkit-scrollbar-thumb{background:var(--line);border-radius:4px}
pre code{background:none;padding:0;font-size:12.5px;line-height:1.65;display:block;
white-space:pre;color:var(--fg)}
.status{font-weight:700} .s-ok{color:var(--ok)} .s-info{color:var(--info)}
.s-warn{color:var(--warn)} .s-err{color:var(--err)}
.h-name{color:var(--muted)} .h-val{color:var(--fg)}
.j-key{color:var(--info)} .j-str{color:var(--ok)} .j-num{color:var(--warn)}
.j-lit{color:var(--pink);font-weight:600}
.k-sql{color:var(--violet);font-weight:600} .k-cmd{color:var(--info);font-weight:600}
.k-flag{color:var(--warn)} .k-var{color:var(--pink)}
.hide{display:none!important}

/* ---- in-browser runner ---- */
.run{font:11px var(--mono);padding:2px 10px;border-radius:999px;cursor:pointer;
border:1px solid var(--line);background:var(--panel);color:var(--info);margin-left:auto}
.run:hover:not(:disabled){color:var(--fg);border-color:var(--info)}
.run:disabled{opacity:.5;cursor:not-allowed;color:var(--muted)}
.run ~ .copy,.run ~ .snip{margin-left:0}
.snip{font:11px var(--mono);padding:2px 10px;border-radius:999px;cursor:pointer;
border:1px solid var(--line);background:var(--panel);color:var(--muted)}
.snip:hover{color:var(--fg);border-color:var(--info)}
.snip.done,.copy.done{color:var(--ok);border-color:var(--ok)}
.run:focus-visible,.copy:focus-visible,.snip:focus-visible,.unfold:focus-visible,
.mb-tools button:focus-visible,.mb-tools select:focus-visible,.rl:focus-visible,
.runp input:focus-visible{outline:2px solid var(--info);outline-offset:2px}
.run.busy{color:var(--warn);border-color:var(--warn)}
.run-why{font:11px/1.5 var(--mono);color:var(--warn);flex:1 1 100%;order:9;
padding:2px 0 0;white-space:normal}
.run-need{font:11px/1.5 var(--mono);color:var(--muted);flex:1 1 100%;order:9;
padding:2px 0 0;white-space:normal}

/* the panel */
.runp{margin-bottom:14px}
.runp-row{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:10px}
.runp-row label{font:600 12px var(--mono);color:var(--muted)}
.runp input{font:12.5px var(--mono);padding:6px 12px;border-radius:10px;min-width:280px;
border:1px solid var(--line);background:var(--code);color:var(--fg)}
.runp-note{font:11.5px var(--mono);color:var(--warn)}
.runp-note.ok{color:var(--ok)}
.runp-lead{margin:0 0 9px;font-size:13px;color:var(--fg)}
.runp-facts{margin:0;padding-left:18px;font-size:12.5px;color:var(--muted)}
.runp-facts li{margin:0 0 5px}
.runp-facts b{color:var(--fg);font-weight:600}

/* the live result, under its block and unmistakably not the recorded one */
.run-out{border-top:2px dashed var(--info);background:var(--panel2)}
.run-out.bad{border-top-color:var(--err)}
.run-head{display:flex;align-items:center;gap:9px;flex-wrap:wrap;padding:8px 12px}
.run-live{font:700 10px/1.6 var(--mono);letter-spacing:.12em;text-transform:uppercase;
color:var(--info);border:1px solid currentColor;border-radius:999px;padding:1px 8px}
.run-out.bad .run-live{color:var(--err)}
.run-code{font:700 12px/1.6 var(--mono);padding:1px 9px;border-radius:999px;
background:color-mix(in srgb,currentColor 11%,transparent)}
.run-code.zero{color:var(--ok)} .run-code.nonzero{color:var(--err)}
.run-time{color:var(--muted);font:11.5px var(--mono)}
.run-url{flex-basis:100%;font:11.5px/1.5 var(--mono);color:var(--muted);word-break:break-all}
.run-cmp{font:600 11.5px var(--mono);padding:1px 9px;border-radius:999px;
background:color-mix(in srgb,currentColor 11%,transparent)}
.run-cmp.same{color:var(--ok)} .run-cmp.diff{color:var(--warn)}
.run-note{color:var(--warn);font-size:12px;flex-basis:100%;line-height:1.5;padding:0 12px 10px}
.run-sec{border-top:1px solid var(--line)}
.run-lab{display:block;padding:5px 12px 0;font:700 10px/1.6 var(--mono);letter-spacing:.12em;
text-transform:uppercase;color:var(--muted)}
.run-sec pre{max-height:340px;overflow:auto;padding:5px 12px 11px}
.run-sec pre code{font-size:12px}
.run-empty{padding:6px 12px 11px;color:var(--muted);font-style:italic;font-size:12px}
.run-close{font:11px var(--mono);margin-left:auto;padding:1px 9px;border-radius:999px;cursor:pointer;
border:1px solid var(--line);background:var(--panel);color:var(--muted)}
.run-close:hover{color:var(--fg)}
.sr-only{position:absolute;width:1px;height:1px;margin:-1px;padding:0;overflow:hidden;
clip:rect(0 0 0 0);white-space:nowrap;border:0}

/* ---- motion, only where it says something, and only if the reader allows it ---- */
@media(prefers-reduced-motion:no-preference){
  @keyframes rise{from{opacity:0;transform:translateY(10px)}}
  @keyframes pop{from{opacity:0;transform:scale(.72)}}
  @keyframes grow{from{transform:scaleX(0)}}
  @keyframes fade{from{opacity:0;transform:translateY(-4px)}}
  .story{animation:rise .5s cubic-bezier(.2,.8,.2,1) both}
  .donut{animation:pop .6s .1s cubic-bezier(.2,.8,.2,1) both}
  .stat{animation:rise .5s cubic-bezier(.2,.8,.2,1) both}
  .stat:nth-child(2){animation-delay:.05s} .stat:nth-child(3){animation-delay:.1s}
  .stat:nth-child(4){animation-delay:.15s} .stat:nth-child(5){animation-delay:.2s}
  .stat:nth-child(6){animation-delay:.25s}
  .bar-fill{transform-origin:left;animation:grow .7s .2s cubic-bezier(.2,.8,.2,1) both}
  details.case[open]>.case-body{animation:fade .18s ease-out}
  .step-h::before{transition:transform .15s}
  .step:hover>.step-h::before{transform:scale(1.08)}
}
"""

JS = """
var root=document.documentElement;
var saved=localStorage.getItem('tbc-theme'); if(saved) root.dataset.theme=saved;

/* ---- resizable case rail ---------------------------------------------
   The width lives in one custom property that both the grid column and the
   grip's own left offset read, so the handle stays on the border it moves. */
var RAIL_KEY='tbc-rail-w', RAIL_MIN=170, RAIL_MAX=560, RAIL_DEF=252;
var grip=document.querySelector('.grip');
function setRail(px,save){
  px=Math.max(RAIL_MIN,Math.min(RAIL_MAX,Math.round(px)));
  root.style.setProperty('--rail-w',px+'px');
  if(grip) grip.setAttribute('aria-valuenow',px);
  if(save) localStorage.setItem(RAIL_KEY,px);
  return px;
}
var savedRail=parseInt(localStorage.getItem(RAIL_KEY),10);
if(savedRail) setRail(savedRail,false);
if(grip){
  grip.setAttribute('aria-valuemin',RAIL_MIN);
  grip.setAttribute('aria-valuemax',RAIL_MAX);
  grip.setAttribute('aria-valuenow',savedRail||RAIL_DEF);
  grip.addEventListener('pointerdown',function(e){
    e.preventDefault();
    var left=document.querySelector('.layout').getBoundingClientRect().left;
    // Capture keeps the pointer on the grip when it outruns an 11px target.
    // The window listeners below are what actually carry the drag, so a
    // browser that refuses the capture still resizes.
    try{ grip.setPointerCapture(e.pointerId); }catch(_){}
    grip.classList.add('drag'); root.classList.add('resizing');
    function move(ev){ setRail(ev.clientX-left,false); }
    function up(ev){
      try{ grip.releasePointerCapture(e.pointerId); }catch(_){}
      window.removeEventListener('pointermove',move);
      window.removeEventListener('pointerup',up);
      window.removeEventListener('pointercancel',up);
      grip.classList.remove('drag'); root.classList.remove('resizing');
      setRail(ev.clientX-left,true);
    }
    window.addEventListener('pointermove',move);
    window.addEventListener('pointerup',up);
    window.addEventListener('pointercancel',up);
  });
  grip.addEventListener('dblclick',function(){ setRail(RAIL_DEF,true); });
  grip.addEventListener('keydown',function(e){
    var step=e.shiftKey?40:10, cur=parseInt(getComputedStyle(root).getPropertyValue('--rail-w'),10)||RAIL_DEF;
    if(e.key==='ArrowLeft') setRail(cur-step,true);
    else if(e.key==='ArrowRight') setRail(cur+step,true);
    else if(e.key==='Home') setRail(RAIL_DEF,true);
    else return;
    e.preventDefault();
  });
}
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
  var cl=e.target.closest('td.cases a, .story a, .rail a');
  if(cl){tipHide();reveal(cl.getAttribute('href'));return;}
  var u=e.target.closest('.unfold');
  if(u){
    var blk=u.closest('.block'); var open=blk.classList.toggle('open');
    u.textContent = open ? 'collapse' : 'show all lines';
  }
});
/* Cards are collapsed by default, so a jump to one has to open it -- and
   un-hide it, in case the failures filter is on -- or the link lands on a
   closed summary and looks broken. */
function reveal(hash){
  if(!hash||hash.charAt(0)!=='#') return;
  var el=document.getElementById(decodeURIComponent(hash.slice(1)));
  if(!el) return;
  el.classList.remove('hide');
  if(el.tagName==='DETAILS') el.open=true;
  el.scrollIntoView();
}
window.addEventListener('hashchange',function(){reveal(location.hash);});
reveal(location.hash);
/* Case-number hints. The coverage table's numbers carry their case's title,
   verdict, status and error code as data-* attributes; this draws them.

   Why one fixed-position node on <body> rather than a span inside the cell:
   an in-cell tooltip is only safe while every ancestor keeps its overflow
   visible, and this stylesheet already clips .block and details.case. Fixed
   also means the placement is measured against the viewport, so the first row
   drops its hint below the number and the last row flips it above, and neither
   runs off an edge. The same text is on the link as aria-label, so nothing
   here is the only way to read it. */
var tip=document.createElement('div');
tip.id='tip'; tip.setAttribute('aria-hidden','true');
tip.innerHTML='<div class="tip-h"><span class="tip-n"></span><span class="badge"></span></div>'
             +'<div class="tip-t"></div><div class="tip-m"></div><div class="tip-a"></div>';
document.body.appendChild(tip);
var tipN=tip.querySelector('.tip-n'),tipV=tip.querySelector('.badge'),
    tipT=tip.querySelector('.tip-t'),tipM=tip.querySelector('.tip-m'),
    tipA=tip.querySelector('.tip-a'),tipFor=null,tipKeyed=false;
function tipLink(e){return e.target&&e.target.closest?e.target.closest('td.cases a'):null;}
function tipHide(){tipFor=null;tipKeyed=false;tip.classList.remove('on');}
function tipPlace(){
  if(!tipFor) return;
  var r=tipFor.getBoundingClientRect(),pad=10;
  if(r.bottom<0||r.top>window.innerHeight){tipHide();return;}
  tip.style.left='0px'; tip.style.top='0px';           /* measure unsqueezed */
  var w=tip.offsetWidth,h=tip.offsetHeight,top=r.bottom+pad;
  if(top+h>window.innerHeight-pad) top=r.top-h-pad;    /* no room below: flip */
  if(top<pad) top=pad;                                 /* nor above: pin */
  tip.style.left=Math.max(pad,Math.min(r.left+r.width/2-w/2,window.innerWidth-w-pad))+'px';
  tip.style.top=top+'px';
}
function tipShow(a){
  var d=a.dataset;
  if(!d.t){tipHide();return;}
  tipFor=a;
  tipN.textContent=d.n||a.textContent;
  tipV.textContent=d.v||'SKIP'; tipV.className='badge '+(d.v||'SKIP');
  tipT.textContent=d.t;
  tipM.textContent=[d.s?'HTTP '+d.s:'',d.c||''].filter(Boolean).join('  \\u00b7  ');
  tipA.textContent=d.a||'';   /* empty on an ungrouped report, and then hidden */
  tipPlace(); tip.classList.add('on');
}
/* A hint raised by the keyboard outranks the mouse. Scrolling the page under
   a parked pointer makes Chrome fire mouseover on whatever slid beneath it,
   and without this the tabbed-to hint would vanish on the reader's own scroll. */
document.addEventListener('mouseover',function(e){
  var a=tipLink(e);
  if(a){tipShow(a);tipKeyed=false;} else if(tipFor&&!tipKeyed) tipHide();
});
document.addEventListener('focusin',function(e){
  var a=tipLink(e);
  if(a){tipShow(a);tipKeyed=true;} else if(tipFor) tipHide();
});
document.addEventListener('focusout',function(e){if(tipFor===e.target) tipHide();});
document.addEventListener('keydown',function(e){if(e.key==='Escape') tipHide();});
window.addEventListener('scroll',tipPlace,true);
window.addEventListener('resize',tipPlace);
/* ---- mini-bar tools: expand, collapse, only-failures, jump ---- */
function cases(){return Array.prototype.slice.call(document.querySelectorAll('details.case'));}
var onlyFail=false;
function setOnlyFail(on){
  onlyFail=on;
  Array.prototype.forEach.call(document.querySelectorAll('[data-act=onlyfail]'),function(b){
    b.classList.toggle('on',on); b.setAttribute('aria-pressed',on?'true':'false');
  });
  cases().forEach(function(d){
    var fail=d.dataset.verdict==='FAIL';
    d.classList.toggle('hide',on&&!fail);
    if(on&&fail) d.open=true;
  });
  /* An area divider whose cards have all just been hidden goes with them; a
     title left standing over nothing reads as a broken page. */
  Array.prototype.forEach.call(document.querySelectorAll('.area-head'),function(h){
    h.classList.toggle('hide',on&&h.dataset.fails==='0');
  });
}
document.addEventListener('click',function(e){
  var b=e.target.closest('[data-act]');
  if(!b) return;
  var act=b.dataset.act;
  if(act==='expand') cases().forEach(function(d){d.open=true;});
  else if(act==='collapse') cases().forEach(function(d){d.open=false;});
  else if(act==='onlyfail') setOnlyFail(!onlyFail);
});
var jump=document.getElementById('jump');
if(jump) jump.onchange=function(){
  if(jump.value){ reveal(jump.value); history.replaceState(null,'',jump.value); }
  jump.value='';
};

/* ---- in-browser runner ------------------------------------------------
   Each Run button carries its own request as JSON -- method, path, host,
   headers, body -- read out of the recorded curl at render time. Clicking one
   calls `fetch` from this very page. There is no proxy, no helper process and
   nothing to install; a report is a file, and this keeps it one.

   What a browser cannot do, this does not pretend to do. It cannot set `Host`,
   so the host is folded into the URL instead. It cannot set `Cookie`, and the
   session cookie here is HttpOnly, so a request needing auth only works when
   the page is served from that same origin. And it cannot read a reply from an
   API that sends no CORS headers, which is why every failure below names the
   likely cause and offers a snippet to paste into DevTools instead. */
var RUN_KEY='tbc-run-origin';
var oIn=document.getElementById('run-origin'),oNote=document.getElementById('run-origin-note');
/* One announcer for the whole page. A live region per block would make 90 of
   them; a screen reader needs exactly one place to hear the result. */
var announce=document.createElement('div');
announce.className='sr-only';
announce.setAttribute('role','status'); announce.setAttribute('aria-live','polite');
document.body.appendChild(announce);
function say(t){announce.textContent=t;}
function baseOrigin(){
  var v=(oIn&&oIn.value.trim())||'http://localhost:8080';
  try{var u=new URL(v);return (u.protocol==='http:'||u.protocol==='https:')?u:null;}
  catch(e){return null;}
}
function originNote(){
  if(!oNote) return;
  var u=baseOrigin();
  if(!u){oNote.textContent='not an http(s) URL';oNote.className='runp-note';return;}
  var msg,cls='runp-note';
  if(u.origin===location.origin){msg='same origin as this page \\u2014 cookies ride along, no CORS';cls+=' ok';}
  else if(location.protocol==='file:') msg='this page is on file:// \\u2014 every Run will be blocked by CORS';
  else msg='this page is '+location.origin+' \\u2014 a cross-origin Run will be blocked';
  oNote.textContent=msg; oNote.className=cls;
}
if(oIn){
  var savedOrigin=localStorage.getItem(RUN_KEY);
  if(savedOrigin) oIn.value=savedOrigin;
  oIn.addEventListener('input',function(){
    localStorage.setItem(RUN_KEY,oIn.value.trim()); originNote();
  });
  originNote();
}
/* Spring mints XSRF-TOKEN with HttpOnly=false so a SPA can echo it back. That
   makes it the one recorded shell variable this page can resolve honestly. */
function xsrfCookie(){
  var m=document.cookie.match(/(?:^|;\\s*)XSRF-TOKEN=([^;]*)/);
  return m?decodeURIComponent(m[1]):null;
}
function fillVars(text,xsrf){
  return text.replace(/\\$\\{(\\w+)\\}|\\$(\\w+)/g,function(all,a,b){
    var n=a||b;
    return /^XSRF/i.test(n)?(xsrf===null?all:xsrf):all;
  });
}
/* The URL a browser can actually ask for: the recorded path, the scheme and
   port from the panel, and the host taken from the curl's Host header because
   `fetch` will not send one. */
function buildReq(spec){
  var base=baseOrigin();
  if(!base) return {error:'The base origin field is not an http(s) URL.'};
  var path;
  try{var u=new URL(spec.url);path=u.pathname+u.search;}
  catch(e){return {error:'The recorded URL does not parse.'};}
  var host=spec.host||base.hostname;
  var url=base.protocol+'//'+host+(base.port?':'+base.port:'')+path;
  var xsrf=xsrfCookie(),headers={},shown=[],missing=null;
  (spec.headers||[]).forEach(function(h){
    var v=fillVars(h[1],xsrf);
    if(/\\$/.test(v)&&/\\$\\{?XSRF/i.test(h[1])&&xsrf===null) missing='XSRF-TOKEN';
    headers[h[0]]=v; shown.push(h[0]+': '+v);
  });
  var body=spec.body==null?null:fillVars(spec.body,xsrf);
  if(missing) return {error:'This request echoes the CSRF cookie, and no readable '
    +'XSRF-TOKEN cookie exists for '+location.origin+'. Load the app on this origin '
    +'first, or copy the fetch snippet and run it there.'};
  return {url:url,host:host,method:spec.method||'GET',headers:headers,
          shown:shown,body:body,base:base};
}
function el(tag,cls,text){
  var n=document.createElement(tag);
  if(cls) n.className=cls;
  if(text!=null) n.textContent=text;   /* textContent, so a reply can never be markup */
  return n;
}
function outFor(block){
  var o=block.querySelector('.run-out');
  if(!o){o=el('div','run-out');o.tabIndex=-1;o.setAttribute('role','group');
    o.setAttribute('aria-label','Live result, not the recorded one');block.appendChild(o);}
  o.textContent=''; o.className='run-out'; return o;
}
function section(name,text,mono){
  var s=el('div','run-sec');
  s.appendChild(el('span','run-lab',name));
  if(text){var p=el('pre');p.appendChild(el('code',null,text));s.appendChild(p);}
  else s.appendChild(el('div','run-empty','empty'));
  return s;
}
function pretty(text,type){
  if(!text) return text;
  if(/json/i.test(type||'')||/^\\s*[\\[{]/.test(text)){
    try{return JSON.stringify(JSON.parse(text),null,2);}catch(e){}
  }
  return text;
}
function head(out,req,label,cls){
  var h=el('div','run-head');
  h.appendChild(el('span','run-live','live result'));
  h.appendChild(el('span','run-code '+(cls||''),label));
  var x=el('button','run-close','dismiss');
  x.onclick=function(){out.remove();};
  h.appendChild(x);
  h.appendChild(el('div','run-url',req?req.method+' '+req.url:''));
  out.appendChild(h);
  return h;
}
function statusClass(code){
  if(code>=500) return 'nonzero';
  if(code>=400) return 'nonzero';
  return 'zero';
}
function whyFailed(req){
  if(location.protocol==='file:')
    return 'Blocked before it left the browser: this page is on file://, so its origin '
      +'is null, and this API sends no Access-Control-Allow-Origin. Nothing reached the '
      +'server. Use Copy as fetch snippet and paste it into DevTools on '+req.base.origin+'.';
  if(req.base.origin!==location.origin||req.host!==location.hostname)
    return 'Blocked by CORS: this page is '+location.origin+' and the request goes to '
      +req.method+' '+req.url+', which answers no preflight and sends no '
      +'Access-Control-Allow-Origin. Serve this report from that origin, or use '
      +'Copy as fetch snippet in DevTools there.';
  return 'The request did not complete. The service at '+req.base.origin+' may be down, '
    +'or the host '+req.host+' may not resolve.';
}
function fetchSnippet(req){
  var init={method:req.method,credentials:'include'};
  if(req.shown.length) init.headers=req.headers;
  if(req.body!=null&&req.method!=='GET'&&req.method!=='HEAD') init.body=req.body;
  return 'await fetch('+JSON.stringify(req.url)+', '+JSON.stringify(init,null,2)+')\\n'
    +'  .then(async r => ({ status: r.status, body: await r.text() }));';
}
var RUNNING=false;
function runOne(btn){
  if(btn.disabled||RUNNING) return;
  var block=btn.closest('.block');
  var spec;
  try{spec=JSON.parse(btn.dataset.req||'null');}catch(e){spec=null;}
  if(!spec||!block) return;
  var req=buildReq(spec);
  var out=outFor(block);
  if(req.error){out.classList.add('bad');head(out,null,'not sent','nonzero');
    out.appendChild(el('div','run-note',req.error));say(req.error);return;}
  RUNNING=true; btn.disabled=true; btn.classList.add('busy');
  var was=btn.textContent; btn.textContent='running';
  say('Running '+req.method+' '+req.url);
  var init={method:req.method,credentials:'include',redirect:'follow',cache:'no-store'};
  if(req.shown.length) init.headers=req.headers;
  if(req.body!=null&&req.method!=='GET'&&req.method!=='HEAD') init.body=req.body;
  var t0=performance.now();
  fetch(req.url,init).then(function(r){
    return r.text().then(function(text){return {r:r,text:text};});
  }).then(function(got){
    var ms=Math.round(performance.now()-t0),r=got.r;
    out.className='run-out'+(r.status>=400?' bad':'');
    var h=head(out,req,r.status+(r.statusText?' '+r.statusText:''),statusClass(r.status));
    h.insertBefore(el('span','run-time',ms+' ms'),h.querySelector('.run-close'));
    var rec=btn.dataset.recorded;
    if(rec){
      var same=(parseInt(rec,10)===r.status);
      h.insertBefore(el('span','run-cmp '+(same?'same':'diff'),
        same?('matches the recorded '+rec):('recorded '+rec+', now '+r.status)),
        h.querySelector('.run-close'));
    }
    var hs=[];
    r.headers.forEach(function(v,k){hs.push(k+': '+v);});
    hs.sort();
    if(!hs.length) hs.push('(the browser exposed no response headers)');
    out.appendChild(section('response headers',hs.join('\\n')));
    out.appendChild(section('response body',
      pretty(got.text,r.headers.get('content-type'))));
    if(req.shown.length) out.appendChild(section('sent headers',req.shown.join('\\n')));
    say('Answered '+r.status+' in '+ms+' milliseconds.');
  }).catch(function(){
    var ms=Math.round(performance.now()-t0);
    out.className='run-out bad';
    var h=head(out,req,'no reply','nonzero');
    h.insertBefore(el('span','run-time',ms+' ms'),h.querySelector('.run-close'));
    var why=whyFailed(req);
    out.appendChild(el('div','run-note',why));
    say('The request failed. '+why);
  }).then(function(){
    RUNNING=false; btn.disabled=false; btn.classList.remove('busy'); btn.textContent=was;
  });
}
function copySnippet(btn){
  var spec;
  try{spec=JSON.parse(btn.dataset.req||'null');}catch(e){spec=null;}
  if(!spec) return;
  var req=buildReq(spec);
  var text=req.error?('/* '+req.error+' */'):fetchSnippet(req);
  navigator.clipboard.writeText(text).then(function(){
    btn.textContent='copied'; btn.classList.add('done');
    setTimeout(function(){btn.textContent='copy as fetch';btn.classList.remove('done');},1400);
  });
}
document.addEventListener('click',function(e){
  var t=(e.target&&e.target.closest)?e.target:null;
  if(!t) return;
  var r=t.closest('.run');
  if(r){runOne(r);return;}
  var s=t.closest('.snip');
  if(s) copySnippet(s);
});

/* ---- rail scroll-sync: highlight the case the reader is on ------------
   The current case is the last card whose top sits above the reading line
   (35% down the viewport). Hidden cards (Only-failures filter) don't count.
   The rail scrolls its own highlight into view with scrollTop math. */
var railEl=document.querySelector('.rail');
if(railEl&&railEl.querySelector('a')){
  var railLinks={};
  Array.prototype.forEach.call(railEl.querySelectorAll('a[href^="#"]'),function(a){
    railLinks[decodeURIComponent(a.getAttribute('href').slice(1))]=a;
  });
  var spyTick=false,spyCur=null;
  function spy(){
    spyTick=false;
    var line=window.innerHeight*0.35,cur=null;
    var ts=document.querySelectorAll('details.case');
    for(var i=0;i<ts.length;i++){
      if(!railLinks[ts[i].id]||ts[i].classList.contains('hide')) continue;
      if(ts[i].getBoundingClientRect().top<=line) cur=ts[i]; else break;
    }
    var id=cur?cur.id:null;
    if(id===spyCur) return;
    if(spyCur&&railLinks[spyCur]) railLinks[spyCur].classList.remove('on');
    spyCur=id;
    if(!id) return;
    var a=railLinks[id];
    a.classList.add('on');
    var rr=railEl.getBoundingClientRect(),ar=a.getBoundingClientRect();
    if(ar.top<rr.top+48||ar.bottom>rr.bottom-48)
      railEl.scrollTop+=ar.top-(rr.top+rr.height/2);
  }
  window.addEventListener('scroll',function(){
    if(!spyTick){spyTick=true;requestAnimationFrame(spy);}
  },{passive:true});
  window.addEventListener('resize',function(){
    if(!spyTick){spyTick=true;requestAnimationFrame(spy);}
  });
  spy();
}
"""

PAGE = """<!doctype html>
<html lang="en" data-theme="light"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>{fonts}{css}</style></head>
<body><div class="layout">
<nav class="rail" aria-label="Test cases">{rail}</nav>
<div class="grip" role="separator" aria-orientation="vertical" tabindex="0"
 aria-label="Resize the case list" title="Drag to resize \u00b7 double-click to reset"></div>
<div class="wrap">
<header class="mast">
  <div class="grow"><h1>{h1}</h1><div class="meta">{meta}</div></div>
  <button class="theme" id="theme">light / dark</button>
</header>
{story}
{runner}
{dash}
{areas}
{coverage}
{prose}
<div class="minibar" id="minibar"><h2>Test cases</h2><span class="mb-count">{mbcount}</span>
<div class="mb-tools"><button data-act="expand">Expand all</button><button data-act="collapse">Collapse all</button>{failbtn}
<select id="jump" aria-label="Jump to a case"><option value="">Jump to case&hellip;</option>{jump}</select></div></div>
{cases}
{tail}
</div></div><script>{js}</script></body></html>
"""


def run_slot(case: "Section") -> tuple[str, bool]:
    """The Run affordance for one case, and whether it can actually fire.

    A refusal is a sentence, not a missing button. "Run is off here: it builds
    this request from $TOK" tells a reader what the recorded run had that their
    browser does not; a button that quietly is not there tells them nothing, and
    a button that fires anyway would answer a question they did not ask.
    """
    rb = case.request_block()
    if rb is None:
        return "", False
    bid, lang, text = rb
    if not browser_runnable(lang, text):
        # Deliberately nothing at all. A block that shells out to docker or psql
        # is evidence, and evidence does not get a button -- not even a greyed
        # one, which would still invite the click it can never honour.
        return "", False
    spec = case.request_spec()
    if spec is None:
        return "", False
    if spec["blocked"]:
        return (f'<button class="run" disabled>run</button>'
                f'<span class="run-why">Run is off here: {esc(spec["blocked"])}.</span>'), False

    payload = html.escape(json.dumps({
        "method": spec["method"], "url": spec["url"], "host": spec["host"],
        "headers": spec["headers"], "body": spec["body"],
    }, ensure_ascii=False), quote=True)
    recorded = case.status()
    notes: list[str] = []
    if spec["host"]:
        notes.append(f'Host comes from the URL: the request goes to {spec["host"]}')
    if "st_access" in spec["cookies"]:
        notes.append("carries your st_access cookie only if this page is served "
                     "from that origin and you are signed in")
    if spec["dropped"]:
        notes.append("the browser will not send " + ", ".join(sorted(set(spec["dropped"]))))
    note = f'<span class="run-need">{esc(" \u00b7 ".join(notes))}</span>' if notes else ""
    return (
        f'<button class="run" data-req="{payload}"'
        f'{f" data-recorded={recorded}" if recorded is not None else ""}'
        f' title="Send this request from your browser">run</button>'
        f'<button class="snip" data-req="{payload}">copy as fetch</button>{note}'
    ), True


def build(md: str, source_name: str, source_path: str | None = None) -> str:
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
    anchors = case_anchors(cases)
    failing = [(n, a) for (n, a), c in zip(anchors, cases) if (c.verdict or "SKIP") == "FAIL"]

    # The report's own grouping, or nothing at all if it has none. Everything
    # below asks `if area_list` rather than assuming groups exist, so an
    # ungrouped report renders exactly the page it rendered before areas.
    area_list = group_cases(sections, cases, anchors)
    area_of = {anchor: a.name for a in area_list for _, anchor, _ in a.items}
    hints = case_hints(cases, anchors, area_of)

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

    cov = coverage_table(cases, anchors, hints)
    coverage = f'<div class="panel"><h2>Endpoint coverage</h2>{cov}</div>' if cov else ""
    areas = area_panel(area_list, hints)

    # --- prose panels ------------------------------------------------------
    # The "Test cases" checklist and "Details" heading are dropped on purpose:
    # the dashboard and the cards already say all of that, and repeating it is
    # what made the first version feel like a photocopy of the Markdown.
    SKIP_SECTIONS = ("test cases", "details")
    # Folded shut: the reader opens these only when they need them. The cards
    # are what the page is for, and these three sit between the reader and them.
    LONG_SECTIONS = ("setup", "what changed", "scenario coverage")
    prose_parts: list[str] = []
    tail_parts: list[str] = []
    for s in sections:
        if s.level != 2:
            continue
        title_l = s.title.strip().lower()
        if title_l in SKIP_SECTIONS:
            continue
        # A grouped report states its areas twice more: as `## Navigation`, and
        # as the lead paragraph under each `## Details — <area>`. Both are on
        # the page already — the Areas panel counted from the cases themselves,
        # the lead moved onto its own area heading — so neither is repeated
        # here. An ungrouped report has neither, and keeps whatever it wrote.
        if area_list and (title_l == "navigation" or section_area(s.title)):
            continue
        content = "".join(s.html)
        if not content.strip():
            continue
        if title_l in LONG_SECTIONS:
            panel = (
                f'<div class="panel"><details class="fold">'
                f"<summary>{inline(s.title)}</summary>"
                f'<div class="prose">{content}</div></details></div>'
            )
        else:
            panel = f'<div class="panel"><h2>{inline(s.title)}</h2><div class="prose">{content}</div></div>'
        (tail_parts if title_l.startswith("summary") else prose_parts).append(panel)

    # --- case cards --------------------------------------------------------
    card_html: list[str] = []
    jump_opts: list[str] = []
    n_live = n_blocked = 0
    # anchor of an area's first case -> the area, so the divider and the jump
    # list's optgroup both open at exactly the card the area starts on.
    opens = {a.items[0][1]: a for a in area_list}
    in_group = False
    for (n, anchor), c in zip(anchors, cases):
        starting = opens.get(anchor)
        if starting is not None:
            card_html.append(area_head(starting))
            if in_group:
                jump_opts.append("</optgroup>")
            jump_opts.append(f'<optgroup label="{html.escape(starting.name, quote=True)}">')
            in_group = True
        v = c.verdict or "SKIP"
        # Exactly one block per case can be fired, and only after `run_slot`
        # has agreed it is a request a browser may send. Everything else on the
        # card -- the recorded request, the response, the DB evidence -- keeps
        # its copy button and is never written to by a run.
        bid = c.request_block_id()
        slot, live = run_slot(c)
        if bid is not None and slot:
            c.html = [h.replace(f"<!--RUN:cb{bid}-->", slot, 1) for h in c.html]
        if live:
            n_live += 1
        elif slot:
            n_blocked += 1
        method, path, _ = c.request()
        pill = f'<span class="pill {method_class(method)}">{esc(method)}</span>' if method else ""
        st = c.status()
        st_pill = (
            f'<span class="pill {STATUS_CLASS.get(str(st)[0], "s-ok")}">{st}</span>'
            if st is not None else ""
        )
        body = "".join(c.html).replace(
            '<div class="step step-result">', f'<div class="step step-result" data-verdict="{v}">', 1
        )
        card_html.append(
            f'<details class="case" id="{html.escape(anchor, quote=True)}" '
            f'data-verdict="{v}"{" open" if v == "FAIL" else ""}>'
            f'<summary><span class="case-n">{esc(n)}</span>'
            f'<span class="case-t">{inline(c.short_title())}</span>'
            f'{pill}{st_pill}<span class="badge {v}">{v}</span></summary>'
            f'<div class="case-body">{flow_strip(c)}{body}</div></details>'
        )
        plain = c.short_title().replace("`", "").strip()
        jump_opts.append(
            f'<option value="#{html.escape(anchor, quote=True)}">'
            f'{html.escape(n)} &middot; {v} &middot; {html.escape(plain)}</option>'
        )

    if in_group:
        jump_opts.append("</optgroup>")

    failbtn = '<button data-act="onlyfail" aria-pressed="false">Only failures</button>' if counts["FAIL"] else ""
    mb = [f'<b>{total}</b> case{"" if total == 1 else "s"}', f'<b class="ok">{counts["PASS"]}</b> pass']
    if counts["FAIL"]:
        mb.append(f'<b class="err">{counts["FAIL"]}</b> fail')
    if counts["SKIP"]:
        mb.append(f'<b class="warn">{counts["SKIP"]}</b> skip')

    # --- case rail: a persistent index down the left side -------------------
    # One link per case, grouped under the report's own area names when it has
    # them. The rail is the sidebar's whole content; an empty report gets an
    # empty <nav>, which the CSS collapses.
    rail_items: list[str] = []
    seen_areas: set = set()
    for (n, anchor), c in zip(anchors, cases):
        aname = area_of.get(anchor)
        if aname and aname not in seen_areas:
            seen_areas.add(aname)
            rail_items.append(f'<span class="rail-h">{esc(aname)}</span>')
        v = c.verdict or "SKIP"
        plain_t = c.short_title().replace("\u0060", "").strip()
        rail_items.append(
            f'<a href="#{html.escape(anchor, quote=True)}" class="rl {v}">'
            f'<i class="rd {v}"></i><b>{esc(n)}</b><span>{esc(plain_t)}</span></a>'
        )
    rail = ""
    if rail_items:
        rail = ('<div class="rail-in"><span class="rail-top">Cases</span>'
                + "".join(rail_items) + "</div>")

    page = PAGE.format(
        title=html.escape(doc_title, quote=True),
        rail=rail,
        fonts=font_css(), css=CSS, js=JS,
        h1=inline(doc_title),
        meta="".join(meta_bits),
        story=headline(counts, failing, len(endpoints), db_qs, len(codes)),
        runner=run_panel(n_live, n_blocked),
        dash=dash,
        areas=areas,
        coverage=coverage,
        prose="".join(prose_parts),
        mbcount=" &middot; ".join(mb),
        failbtn=failbtn,
        jump="".join(jump_opts),
        cases="".join(card_html) or '<div class="panel">No test cases found in this report.</div>',
        tail="".join(tail_parts),
    )
    # A slot no case claimed is a block that looked runnable but is not the
    # request under test. It leaves nothing behind.
    return re.sub(r"<!--RUN:cb\d+-->", "", page)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Render a test-run Markdown report as a self-contained HTML dashboard."
    )
    ap.add_argument("report", help="path to the .md report")
    ap.add_argument("-o", "--output", help="output .html path (default: same name, .html)")
    ap.add_argument("--light", action="store_true",
                    help="default to the light theme (already the default)")
    ap.add_argument("--dark", action="store_true", help="default to the dark theme")
    args = ap.parse_args()

    src = Path(args.report)
    if not src.is_file():
        print(f"error: no such file: {src}", file=sys.stderr)
        return 1
    page = build(src.read_text(encoding="utf-8"), src.name, str(src))
    if args.dark:
        page = page.replace('data-theme="light"', 'data-theme="dark"', 1)
    dest = Path(args.output) if args.output else src.with_suffix(".html")
    dest.write_text(page, encoding="utf-8")
    print(dest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
