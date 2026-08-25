#!/usr/bin/env python3
"""Stitch worker part files into the planner's skeleton report, then render it.

    python3 assemble_report.py docs/test-runs/2026-08-24-2130-order-discount.md

Layout the script expects (the planner creates it, the workers fill it):

    docs/test-runs/2026-08-24-2130-order-discount.md          skeleton (rewritten in place)
    docs/test-runs/2026-08-24-2130-order-discount/parts/A.md  worker parts, any names
    docs/test-runs/2026-08-24-2130-order-discount/parts/B.md

A part file holds only `### Test N: title` sections, each ending in a
`**Result:** PASS|FAIL|SKIP — note` line. Anything before the first heading is
ignored, so a worker can leave itself a scratch note at the top.

What the script does, in order:

1. Collects every `### Test N:` section from every part. If two parts carry the
   same number, the one modified most recently wins — that is what makes an
   `only=3,7` rerun overwrite the old cases instead of duplicating them.
2. Rewrites each numbered line of `## Test cases` to end in its verdict. A case
   listed but found in no part becomes `SKIP — not run`, and a stub section is
   written for it so the dashboard's count stays honest.
3. Files each section under its `## Details — <area>` heading, in numeric
   order. A run that did not group gets one flat `## Details` instead.
4. Writes `## Summary`: counts, one line per FAIL and SKIP (with the note from
   the result line), and the `## Not run` count if that section exists.
5. Renders the HTML with render_report.py from the same directory, unless
   `--no-render` is given.

Nothing here needs anything beyond Python 3.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

HEADING_RE = re.compile(r"^###\s+Test\s+(\d+)\s*[:.\-—]\s*(.*)$", re.M | re.I)
RESULT_RE = re.compile(r"\*\*Result:?\*\*:?\s*(PASS|FAIL|SKIP)\b\s*(?:[—:\-]\s*(.*))?", re.I)
LISTED_RE = re.compile(r"^(\s*)(\d+)\.\s+(.*?)\s*$")
H2_RE = re.compile(r"^##\s+(.*?)\s*$", re.M)


def split_sections(text: str) -> dict[int, str]:
    """{case number: full section text} for every `### Test N:` in one part."""
    out: dict[int, str] = {}
    matches = list(HEADING_RE.finditer(text))
    for idx, m in enumerate(matches):
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        out[int(m.group(1))] = text[m.start():end].rstrip() + "\n"
    return out


def verdict_of(section: str) -> tuple[str, str]:
    m = RESULT_RE.search(section)
    if not m:
        return "SKIP", "no **Result:** line in the part"
    return m.group(1).upper(), (m.group(2) or "").strip()


def h2_bounds(md: str, title: str) -> tuple[int, int] | None:
    """(start, end) character offsets of a `## title` section including its heading."""
    for m in H2_RE.finditer(md):
        if m.group(1).strip().lower() == title.lower():
            nxt = H2_RE.search(md, m.end())
            return m.start(), (nxt.start() if nxt else len(md))
    return None


def replace_h2(md: str, title: str, body: str) -> str:
    """Replace the body of `## title` (append the section if it is missing)."""
    block = f"## {title}\n\n{body.rstrip()}\n\n"
    b = h2_bounds(md, title)
    if b is None:
        return md.rstrip() + "\n\n" + block
    return md[: b[0]] + block + md[b[1]:]


AREA_RE = re.compile(r"^###\s+(.*?)\s*$", re.M)


def areas_of_checklist(md: str) -> dict[int, str]:
    """{case number: area name} read off the `### <area>` sub-headings.

    An ungrouped checklist has no sub-headings, so every case maps to nothing
    and the caller falls back to one flat `## Details`.
    """
    b = h2_bounds(md, "Test cases")
    if b is None:
        return {}
    out: dict[int, str] = {}
    area = ""
    for line in md[b[0]:b[1]].split("\n"):
        a = AREA_RE.match(line)
        if a:
            area = a.group(1)
            continue
        m = LISTED_RE.match(line)
        if m and area:
            out[int(m.group(2))] = area
    return out


def place_in_areas(md: str, sections: dict[int, tuple[float, str]]) -> str | None:
    """Put each card under its own `## Details — <area>`, keeping the lead line.

    Returns None when the report is not grouped, or when the skeleton's area
    headings do not cover every case — either way the caller writes one flat
    `## Details` instead. Half a grouping renders worse than none: the renderer
    needs every case under an area before it will draw the areas panel.
    """
    area_of = areas_of_checklist(md)
    if not area_of or any(n not in area_of for n in sections):
        return None
    headings = [t for t in H2_RE.findall(md) if t.startswith("Details —") or t.startswith("Details -")]
    named = {t.split("—", 1)[-1].split("-", 1)[-1].strip() if "—" in t else t.split("-", 1)[-1].strip(): t
             for t in headings}
    if set(named) != set(area_of.values()):
        return None
    stale = h2_bounds(md, "Details")   # a flat block left by an earlier assemble
    if stale is not None:
        md = md[: stale[0]] + md[stale[1]:]
    for area, title in named.items():
        b = h2_bounds(md, title)
        if b is None:
            return None
        body_now = md[b[0]:b[1]].split("\n", 1)[1]
        cut = body_now.find("\n### ")          # keep the lead; drop cards from an earlier assemble
        lead = (body_now if cut < 0 else body_now[:cut]).strip()
        cards = [sections[n][1] for n in sorted(sections) if area_of[n] == area]
        body = (lead + "\n\n" if lead else "") + "\n".join(cards)
        md = replace_h2(md, title, body)
    return md


def strip_listed_verdict(text: str) -> str:
    """Remove a trailing `— PASS`, `— FAIL (…)`, `— NOT RUN`, `— expected …` from a list line."""
    return re.sub(
        r"\s*(?:—|--|-)\s*\**(?:PASS|FAIL|SKIP|NOT RUN|expected\b)[^\n]*$", "", text, flags=re.I
    ).rstrip()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("skeleton", help="the report .md written by the planner")
    ap.add_argument("--parts", help="parts directory (default: <skeleton-stem>/parts next to the skeleton)")
    ap.add_argument("--no-render", action="store_true", help="skip the HTML render")
    args = ap.parse_args()

    skel = Path(args.skeleton)
    if not skel.is_file():
        print(f"error: no such file: {skel}", file=sys.stderr)
        return 1
    parts_dir = Path(args.parts) if args.parts else skel.with_suffix("") / "parts"
    part_files = sorted(parts_dir.glob("*.md")) if parts_dir.is_dir() else []
    if not part_files:
        print(f"error: no part files under {parts_dir}", file=sys.stderr)
        return 1

    # 1. collect sections; newest part wins on a collision
    sections: dict[int, tuple[float, str]] = {}
    for pf in part_files:
        mtime = pf.stat().st_mtime
        for n, sec in split_sections(pf.read_text(encoding="utf-8")).items():
            if n not in sections or sections[n][0] < mtime:
                sections[n] = (mtime, sec)

    md = skel.read_text(encoding="utf-8")

    # 2. verdicts into the case list
    listed: list[tuple[int, str]] = []
    b = h2_bounds(md, "Test cases")
    if b is None:
        print("error: skeleton has no `## Test cases` section", file=sys.stderr)
        return 1
    new_lines: list[str] = []
    for line in md[b[0]:b[1]].split("\n"):
        m = LISTED_RE.match(line)
        if not m:
            new_lines.append(line)
            continue
        n = int(m.group(2))
        title = strip_listed_verdict(m.group(3))
        listed.append((n, title))
        if n in sections:
            v, note = verdict_of(sections[n][1])
            tail = f" — {v}" + (f" ({note})" if note and v != "PASS" else "")
        else:
            tail = " — SKIP (not run)"
        new_lines.append(f"{m.group(1)}{n}. {title}{tail}")
    md = md[: b[0]] + "\n".join(new_lines) + md[b[1]:]

    # stubs for listed cases that no worker reached
    for n, title in listed:
        if n not in sections:
            plain = re.sub(r"\*\*", "", title)
            sections[n] = (0.0, f"### Test {n}: {plain}\n\n**Result:** SKIP — not run (no worker reached this case).\n")

    # 3. details — into the area sections when the run grouped, else one flat block
    grouped = place_in_areas(md, sections)
    if grouped is not None:
        md = grouped
    else:
        ordered = [sections[n][1] for n in sorted(sections)]
        md = replace_h2(md, "Details", "\n".join(ordered))

    # 4. summary
    verdicts = {n: verdict_of(sec) for n, (_, sec) in sections.items()}
    passed = sum(1 for v, _ in verdicts.values() if v == "PASS")
    failed = [(n, verdicts[n][1]) for n in sorted(verdicts) if verdicts[n][0] == "FAIL"]
    skipped = [(n, verdicts[n][1]) for n in sorted(verdicts) if verdicts[n][0] == "SKIP"]
    titles = {n: re.sub(r"\*\*", "", t) for n, t in listed}
    for n, (_, sec) in sections.items():
        if n not in titles:
            hm = HEADING_RE.search(sec)
            titles[n] = hm.group(2).strip() if hm else f"case {n}"
    not_run = 0
    nb = h2_bounds(md, "Not run")
    if nb:
        not_run = sum(1 for l in md[nb[0]:nb[1]].split("\n") if LISTED_RE.match(l))

    lines = [f"- Tests passed: {passed} / {len(verdicts)}"]
    if failed:
        lines.append("- Failed:")
        lines += [f"  - Test {n} — {titles[n]}" + (f": {note}" if note else "") for n, note in failed]
    if skipped:
        lines.append("- Skipped:")
        lines += [f"  - Test {n} — {titles[n]}" + (f": {note}" if note else "") for n, note in skipped]
    if not_run:
        lines.append(f"- Not run (over cap): {not_run} — see `## Not run`; rerun with `only=<numbers>`")
    lines.append("- Next steps: <fill in if any>")
    md = replace_h2(md, "Summary", "\n".join(lines))

    skel.write_text(md, encoding="utf-8")
    print(skel)
    print(f"cases {len(verdicts)}: {passed} pass, {len(failed)} fail, {len(skipped)} skip"
          + (f", {not_run} not run" if not_run else ""))

    # 5. render
    if not args.no_render:
        renderer = Path(__file__).with_name("render_report.py")
        r = subprocess.run([sys.executable, str(renderer), str(skel)], capture_output=True, text=True)
        sys.stdout.write(r.stdout)
        sys.stderr.write(r.stderr)
        return r.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
