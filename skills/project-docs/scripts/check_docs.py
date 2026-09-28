#!/usr/bin/env python3
"""Check a generated docs set against the repo.

Usage: check_docs.py <repo-root>

Errors (must fix): missing required files, CLAUDE.md not forwarding, broken relative links or
heading anchors, a feature doc not opening with '## Invariants', a doc missing from the index.
Warnings (confirm or remove): backticked paths or class names that do not appear in the repo.
Exit code 1 when there is any error.
"""
import os
import re
import subprocess
import sys

REQUIRED = ["AGENTS.md", "CLAUDE.md", "CONTEXT.md", "docs/README.md", "docs/architecture.md",
            "docs/standards.md", "docs/testing.md", "docs/development.md"]
SKIP_DIRS = {".git", "node_modules", "target", "build", "dist", "out", ".gradle", ".idea",
             "__pycache__", ".venv", "venv"}
LINK_RX = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")
TICK_RX = re.compile(r"`([^`\n]+)`")
FENCE_RX = re.compile(r"^(```|~~~).*?^\1", re.S | re.M)
IDENT_RX = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")
CAMEL_RX = re.compile(r"^[A-Z][a-z0-9]+(?:[A-Z][A-Za-z0-9]*)+$")
PATHLIKE_RX = re.compile(r"^[\w.\-/]+$")


def repo_files(root):
    try:
        out = subprocess.run(["git", "-C", root, "ls-files", "--cached", "--others",
                              "--exclude-standard"], capture_output=True, text=True,
                             check=True).stdout.splitlines()
        if out:
            return out
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        files += [os.path.relpath(os.path.join(dirpath, n), root) for n in filenames]
    return files


def slug(heading):
    s = heading.strip().lower()
    s = re.sub(r"[`*_~]", "", s)
    s = re.sub(r"[^\w\- ]", "", s)
    return s.replace(" ", "-")


def anchors(path):
    result, seen = set(), {}
    with open(path, encoding="utf-8", errors="replace") as fh:
        text = FENCE_RX.sub("", fh.read())
    for m in re.finditer(r"^#{1,6}\s+(.+?)\s*#*\s*$", text, re.M):
        s = slug(m.group(1))
        n = seen.get(s, 0)
        result.add(s if n == 0 else f"{s}-{n}")
        seen[s] = n + 1
    return result


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    root = os.path.abspath(sys.argv[1])
    errors, warnings = [], []

    for rel in REQUIRED:
        if not os.path.isfile(os.path.join(root, rel)):
            errors.append(f"missing required file {rel}")

    claude = os.path.join(root, "CLAUDE.md")
    if os.path.isfile(claude):
        with open(claude, encoding="utf-8") as fh:
            if "@AGENTS.md" not in fh.read():
                errors.append("CLAUDE.md does not contain '@AGENTS.md'")

    docs = [f for f in ("AGENTS.md", "CONTEXT.md", "CONTEXT-MAP.md")
            if os.path.isfile(os.path.join(root, f))]
    for dirpath, _, filenames in os.walk(os.path.join(root, "docs")):
        for n in filenames:
            if n.endswith(".md"):
                docs.append(os.path.relpath(os.path.join(dirpath, n), root))

    files = repo_files(root)
    doc_set = set(docs)
    source_files = [f for f in files if f not in doc_set and not f.endswith(".md")]
    file_set = set(files)
    dir_set = {os.path.dirname(f) for f in files}
    dir_set |= {d.rsplit("/", i)[0] for d in list(dir_set) for i in range(1, d.count("/") + 1)}
    idents = set()
    for f in source_files:
        try:
            if os.path.getsize(os.path.join(root, f)) > 2_000_000:
                continue
            with open(os.path.join(root, f), encoding="utf-8", errors="ignore") as fh:
                idents.update(IDENT_RX.findall(fh.read()))
        except OSError:
            continue
    basenames = {os.path.basename(f) for f in files}
    dir_names = {part for d in dir_set for part in d.split("/") if part}

    def path_exists(tok):
        t = tok.strip().rstrip("/")
        t = t[2:] if t.startswith("./") else t
        if not t:
            return True
        if t in file_set or t in dir_set or t in basenames:
            return True
        return any(f.endswith("/" + t) or ("/" + t + "/") in ("/" + f)
                   or ("/" + t + ".") in ("/" + f) for f in files)

    anchor_cache = {}
    for rel in docs:
        path = os.path.join(root, rel)
        with open(path, encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
        text = FENCE_RX.sub("", raw)

        for target in LINK_RX.findall(TICK_RX.sub("", text)):
            if re.match(r"^[a-z]+:", target) or target.startswith("mailto:"):
                continue
            file_part, _, frag = target.partition("#")
            dest = path if not file_part else os.path.normpath(
                os.path.join(os.path.dirname(path), file_part))
            if not os.path.exists(dest):
                errors.append(f"{rel}: broken link -> {target}")
                continue
            if frag and dest.endswith(".md") and os.path.isfile(dest):
                anchor_cache.setdefault(dest, anchors(dest))
                if frag.lower() not in anchor_cache[dest]:
                    errors.append(f"{rel}: missing anchor -> {target}")

        for tok in TICK_RX.findall(text):
            t = tok.strip()
            if (" " in t or not t or re.search(r"[<>*{}$=|\\]", t) or t.startswith(("/", "~", "http"))
                    or t.startswith("-") or re.fullmatch(r"\.\w+", t)):
                continue
            if "/" in t or re.search(r"\.(java|kt|ts|js|py|go|xml|ya?ml|properties|sql|json|sh|"
                                     r"gradle|md|html|scss|css|jks|p12|txt|conf)$", t):
                has_ext = re.search(r"\.\w{1,10}$", t.rstrip("/"))
                first_dir = (t[2:] if t.startswith("./") else t).split("/")[0]
                looks_like_path = (has_ext or t.endswith("/") or first_dir in dir_names)
                if PATHLIKE_RX.match(t) and looks_like_path and not path_exists(t):
                    warnings.append(f"{rel}: path not found in repo: `{t}`")
                continue
            head = re.split(r"[.#(:]", t)[0]
            if CAMEL_RX.match(head) and head not in idents and not re.search(r"(Exception|Error)$", head):
                warnings.append(f"{rel}: name not found in source: `{t}`")

        if rel.startswith("docs/features/"):
            first = re.search(r"^##\s+(.+)$", text, re.M)
            if not first or first.group(1).strip() != "Invariants":
                errors.append(f"{rel}: first '##' section must be '## Invariants'")

    index = os.path.join(root, "docs", "README.md")
    if os.path.isfile(index):
        with open(index, encoding="utf-8") as fh:
            idx = fh.read()
        linked = {os.path.normpath(os.path.join("docs", t.partition("#")[0]))
                  for t in LINK_RX.findall(idx) if not re.match(r"^[a-z]+:", t)}
        for rel in docs:
            if rel.startswith("docs/") and rel != "docs/README.md" and rel not in linked:
                errors.append(f"docs/README.md does not link {rel}")
        if "Edit it when" not in idx:
            errors.append("docs/README.md has no 'Edit it when' column")

    for e in errors:
        print(f"ERROR   {e}")
    for w in sorted(set(warnings)):
        print(f"WARN    {w}")
    print(f"\n{len(docs)} docs checked: {len(errors)} errors, {len(set(warnings))} warnings")
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
