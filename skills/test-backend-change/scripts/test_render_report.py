#!/usr/bin/env python3
"""Tests for render_report.py.  Run:  python3 -m unittest test_render_report -v"""

from __future__ import annotations

import re
import sys
import unittest
from collections import OrderedDict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import render_report as rr  # noqa: E402


def section_html(md: str, index: int = 1) -> str:
    _, secs = rr.parse(md)
    return "".join(secs[index].html)


class NestedLists(unittest.TestCase):
    def test_nested_bullets_render_as_nested_lists(self):
        html = section_html("## Summary\n- Issues:\n  - inner one\n  - inner two\n- Next\n")
        self.assertEqual(html.count("<ul>"), 2)
        self.assertIn("<li>Issues:<ul><li>inner one</li><li>inner two</li></ul></li>", html)

    def test_continuation_lines_join_their_item(self):
        html = section_html(
            "## Summary\n- **Bold start** of a long item that wraps\n  onto a second line.\n- Second\n"
        )
        self.assertNotIn("<p>", html)
        self.assertIn("<strong>Bold start</strong> of a long item that wraps onto a second line.</li>", html)

    def test_ordered_and_unordered_nesting(self):
        html = section_html("## Steps\n1. first\n   - detail\n2. second\n")
        self.assertIn("<ol><li>first<ul><li>detail</li></ul></li><li>second</li></ol>", html)

    def test_list_ends_at_unindented_paragraph(self):
        html = section_html("## Notes\n- one\n- two\n\nAfter the list.\n")
        self.assertIn("<ul><li>one</li><li>two</li></ul><p>After the list.</p>", html)


class Steps(unittest.TestCase):
    MD = (
        "### Test 1 — create order\n\n**Goal:** verify POST creates a row.\n\n**DB before:**\n\n"
        "```sql\nSELECT 1;\n```\n\n**Request:**\n\n```bash\ncurl -X POST http://h/api/orders\n```\n\n"
        "**Response:**\n\n```http\nHTTP/1.1 201\n\n{}\n```\n\n**DB after** (same table):\n\n"
        "```sql\nSELECT 2;\n```\n\n**Result:** PASS — row present\n"
    )

    def kinds(self, html: str) -> list[str]:
        return re.findall(r'class="step step-([a-z-]+)"', html)

    def test_labels_become_steps_in_order(self):
        html = section_html(self.MD)
        self.assertEqual(
            self.kinds(html), ["goal", "db-before", "request", "response", "db-after", "result"]
        )

    def test_step_holds_the_blocks_that_follow_its_label(self):
        html = section_html(self.MD)
        req = html.split('class="step step-request"')[1].split('class="step step-response"')[0]
        self.assertIn("curl", req)
        self.assertNotIn("SELECT", req)

    def test_goal_text_after_label_stays_in_goal_step(self):
        html = section_html(self.MD)
        goal = html.split('class="step step-goal"')[1].split('class="step step-db-before"')[0]
        self.assertIn("verify POST creates a row.", goal)

    def test_label_trailing_note_is_kept(self):
        html = section_html(self.MD)
        after = html.split('class="step step-db-after"')[1].split('class="step step-result"')[0]
        self.assertIn("(same table)", after)

    def test_verdict_note_lands_in_result_step(self):
        html = section_html(self.MD)
        result = html.split('class="step step-result"')[1]
        self.assertIn("row present", result)
        self.assertIn('class="badge PASS"', result)

    def test_case_without_labels_has_only_a_result_step(self):
        html = section_html("### Test 2\n\nJust a paragraph.\n\n```bash\ncurl x\n```\n\n**Result:** PASS\n")
        self.assertEqual(self.kinds(html), ["result"])
        self.assertIn("<p>Just a paragraph.</p>", html)

    def test_unknown_label_becomes_a_generic_step(self):
        html = section_html("### Test 3\n\n**Workaround used:** ran it twice.\n\n**Result:** PASS\n")
        self.assertEqual(self.kinds(html), ["other", "result"])
        self.assertIn("Workaround used", html)

    def test_result_note_wrapped_onto_next_line_stays_in_result_step(self):
        html = section_html(
            "### Test 5\n\n**Result:** PASS \u2014 200 and a signed url carrying\nthe context path.\n"
        )
        result = html.split('class="step step-result"')[1]
        self.assertIn("signed url carrying the context path.", result)
        self.assertFalse(html.rstrip().endswith("</p>"), html)

    def test_goal_wrapped_onto_next_line_stays_in_its_note(self):
        html = section_html("### Test 6\n\n**Goal:** verify the request\nreturns 200.\n\n**Result:** PASS\n")
        self.assertIn('<span class="step-note">verify the request returns 200.</span>', html)
        self.assertNotIn("<p>returns 200.</p>", html)

    def test_bold_sentence_in_prose_section_is_not_a_step(self):
        html = section_html("## Summary\n\n**Nothing failed.** All good.\n")
        self.assertNotIn('class="step', html)
        self.assertIn("<strong>Nothing failed.</strong>", html)

    def test_bold_sentence_without_colon_in_case_is_not_a_step(self):
        html = section_html("### Test 4\n\n**This is emphasis** and not a label.\n\n**Result:** PASS\n")
        self.assertEqual(self.kinds(html), ["result"])


class Headline(unittest.TestCase):
    def counts(self, p: int, f: int, s: int) -> "OrderedDict[str, int]":
        return OrderedDict((("PASS", p), ("FAIL", f), ("SKIP", s)))

    def test_all_passed(self):
        h = rr.headline(self.counts(18, 0, 0), [], endpoints=1, db_checks=18, n_codes=5)
        self.assertIn("All 18 cases passed.", h)
        self.assertIn("1 endpoint", h)
        self.assertIn("18 DB checks", h)
        self.assertIn("5 status codes seen", h)

    def test_single_case(self):
        h = rr.headline(self.counts(1, 0, 0), [], endpoints=1, db_checks=0, n_codes=1)
        self.assertIn("The only case passed.", h)

    def test_failures_link_to_every_failing_case(self):
        fails = [("7", "case-7"), ("12", "case-12")]
        h = rr.headline(self.counts(16, 2, 0), fails, endpoints=1, db_checks=18, n_codes=5)
        self.assertIn("2 of 18 cases failed", h)
        self.assertIn('start with <a href="#case-7">case 7</a>.', h)
        self.assertIn('href="#case-7"', h)
        self.assertIn('href="#case-12"', h)

    def test_skips_are_named(self):
        h = rr.headline(self.counts(8, 0, 2), [], endpoints=2, db_checks=3, n_codes=2)
        self.assertIn("8 of 10 cases passed, 2 skipped.", h)

    def test_no_cases(self):
        h = rr.headline(self.counts(0, 0, 0), [], endpoints=0, db_checks=0, n_codes=0)
        self.assertIn("No test cases found", h)


class HttpBlock(unittest.TestCase):
    RAW = 'HTTP/1.1 404\nContent-Type: application/json\nX-Trace: abc\n\n{"code":"ORD-NF-0001"}'

    def test_status_chip_in_bar(self):
        out = rr.render_code("http", self.RAW, 1)
        self.assertIn('class="http-status s-warn">404 Not Found<', out)

    def test_headers_fold_with_count(self):
        out = rr.render_code("http", self.RAW, 1)
        self.assertIn("<summary>2 headers</summary>", out)
        self.assertIn("X-Trace", out.split("</details>")[0])

    def test_body_is_outside_the_fold(self):
        out = rr.render_code("http", self.RAW, 1)
        self.assertIn("ORD-NF-0001", out.split("</details>")[1])

    def test_single_header_is_singular(self):
        out = rr.render_code("http", "HTTP/1.1 200\nX-One: 1\n\n{}", 1)
        self.assertIn("<summary>1 header</summary>", out)

    def test_no_headers_means_no_fold(self):
        out = rr.render_code("http", "HTTP/1.1 204", 1)
        self.assertNotIn("<details", out)
        self.assertIn('class="http-status s-ok">204 No Content<', out)

    def test_block_without_status_line_renders_plainly(self):
        out = rr.render_code("http", '{"a":1}', 1)
        self.assertNotIn("http-status", out)
        self.assertIn('"a"', out)


class Font(unittest.TestCase):
    def test_font_css_embeds_both_weights(self):
        css = rr.font_css(rr.FONT_DIR)
        self.assertEqual(css.count("@font-face"), 2)
        self.assertIn('font-family:"JetBrains Mono"', css)
        self.assertIn("font-weight:700", css)
        self.assertIn("data:font/woff2;base64,", css)

    def test_missing_font_dir_gives_empty_css(self):
        self.assertEqual(rr.font_css(Path("/nonexistent-dir-for-test")), "")


class Page(unittest.TestCase):
    MD = (
        "# Run\n\n### Test 1 — a\n\n```bash\ncurl http://h/x\n```\n\n**Result:** PASS\n\n"
        "### Test 2 — b\n\n**Result:** FAIL\n"
    )

    def test_minibar_lists_every_case(self):
        page = rr.build(self.MD, "r.md")
        self.assertIn('id="minibar"', page)
        self.assertIn('<option value="#case-1">', page)
        self.assertIn('<option value="#case-2">', page)

    def test_headline_present(self):
        page = rr.build(self.MD, "r.md")
        self.assertIn("1 of 2 cases failed", page)

    def test_font_embedded(self):
        page = rr.build(self.MD, "r.md")
        self.assertIn('font-family:"JetBrains Mono"', page)
        self.assertIn("data:font/woff2;base64,", page)


# --------------------------------------------------------------------------
# The three navigation features: links from the coverage table to the cards,
# the hint each of those numbers carries, and the areas a long run is cut into.
# --------------------------------------------------------------------------

GROUPED = (
    "# Run\n\n"
    "## Navigation\n\n- **Alpha** \u2014 cases 1\u20132\n\n"
    "## Details \u2014 Alpha\n\nIntro for alpha.\n\n"
    "### Test 1 \u2014 reads a thing\n\n```bash\ncurl http://h/api/x\n```\n\n"
    "```http\nHTTP/1.1 200\n\n{}\n```\n\n**Result:** PASS\n\n"
    "### Test 2 \u2014 refuses a bad `body`\n\n```bash\ncurl -X POST http://h/api/x\n```\n\n"
    '```http\nHTTP/1.1 422\n\n{"code":"ORD-VAL-0001"}\n```\n\n**Result:** FAIL\n\n'
    "## Details \u2014 Beta\n\n"
    "### Test 3 \u2014 the neighbour\n\n```bash\ncurl http://h/api/y\n```\n\n"
    "```http\nHTTP/1.1 200\n\n{}\n```\n\n**Result:** PASS\n"
)

FLAT = (
    "# Run\n\n## Navigation\n\nnothing to navigate.\n\n## Details\n\n"
    "### Test 1 \u2014 a\n\n```bash\ncurl http://h/api/x\n```\n\n"
    "```http\nHTTP/1.1 200\n\n{}\n```\n\n**Result:** PASS\n\n"
    "### Test 2 \u2014 b\n\n```bash\ncurl http://h/api/x\n```\n\n"
    "```http\nHTTP/1.1 404\n\n{}\n```\n\n**Result:** FAIL\n"
)


def parts(md: str):
    """(cases, anchors) the way build() computes them."""
    _, secs = rr.parse(md)
    cases = [s for s in secs if s.level >= 3 and s.verdict]
    return secs, cases, rr.case_anchors(cases)


class CaseLinks(unittest.TestCase):
    """Endpoint coverage links every row to the cases that produced it."""

    def test_each_case_number_links_to_its_card(self):
        page = rr.build(GROUPED, "r.md")
        ids = set(re.findall(r'\sid="([^"]+)"', page))
        hrefs = set(re.findall(r'href="#([^"]+)"', page))
        hrefs |= set(re.findall(r'<option value="#([^"]+)"', page))
        self.assertEqual(hrefs - ids, set(), "dead links on the page")
        self.assertIn("case-1", ids)
        self.assertIn("case-3", ids)

    def test_one_endpoint_row_lists_every_case_that_hit_it(self):
        md = ("# Run\n\n"
              "### Test 1 \u2014 a\n\n```bash\ncurl http://h/api/x/1\n```\n\n**Result:** PASS\n\n"
              "### Test 2 \u2014 b\n\n```bash\ncurl http://h/api/x/2\n```\n\n**Result:** PASS\n")
        _, cases, anchors = parts(md)
        cov = rr.coverage_table(cases, anchors)
        self.assertEqual(cov.count("<tr>"), 2)          # one header row, one endpoint row
        cell = cov.split('<td class="cases">')[1]
        self.assertIn('href="#case-1"', cell)
        self.assertIn('href="#case-2"', cell)

    def test_different_methods_on_one_path_stay_separate_rows(self):
        _, cases, anchors = parts(GROUPED)
        cov = rr.coverage_table(cases, anchors)
        self.assertIn('<span class="pill m-get">GET</span>', cov)
        self.assertIn('<span class="pill m-post">POST</span>', cov)
        self.assertEqual(cov.count('<td class="mono">/api/x</td>'), 2)

    def test_case_numbers_are_sorted_numerically_not_lexically(self):
        entries = [("10", "case-10"), ("2", "case-2"), ("1", "case-1")]
        cell = rr.case_link_cell(entries, {})
        self.assertEqual(re.findall(r">(\d+)</a>", cell), ["1", "2", "10"])

    def test_case_with_no_request_still_gets_a_row_and_a_link(self):
        md = "# Run\n\n### Test 1 \u2014 no curl here\n\n**Result:** PASS\n"
        _, cases, anchors = parts(md)
        cov = rr.coverage_table(cases, anchors)
        self.assertIn("no request captured", cov)
        self.assertIn('href="#case-1"', cov)

    def test_no_cases_at_all_means_no_coverage_table(self):
        self.assertEqual(rr.coverage_table([], []), "")

    def test_repeated_case_numbers_get_distinct_anchors(self):
        md = ("# Run\n\n### Test 1 \u2014 a\n\n**Result:** PASS\n\n"
              "### Test 1 \u2014 again\n\n**Result:** PASS\n")
        _, cases, anchors = parts(md)
        self.assertEqual([a for _, a in anchors], ["case-1", "case-1-2"])


class CaseHints(unittest.TestCase):
    """Every linked case number carries what the reader needs before clicking."""

    def test_hint_text_names_number_title_verdict_status_and_code(self):
        h = {"n": "7", "t": "rejects a blank name", "v": "FAIL",
             "s": "500 Internal Server Error", "c": "GEN-SYS-0001", "a": ""}
        self.assertEqual(
            rr.hint_text(h),
            "Case 7: rejects a blank name \u2014 FAIL, HTTP 500 Internal Server Error, "
            "error GEN-SYS-0001",
        )

    def test_hint_text_drops_what_the_case_never_showed(self):
        h = {"n": "3", "t": "a claim", "v": "PASS", "s": "", "c": "", "a": ""}
        self.assertEqual(rr.hint_text(h), "Case 3: a claim \u2014 PASS")

    def test_hint_text_names_the_area_when_there_is_one(self):
        h = {"n": "35", "t": "a claim", "v": "FAIL", "s": "", "c": "", "a": "Signup validation"}
        self.assertIn("FAIL, in Signup validation", rr.hint_text(h))

    def test_hint_is_read_off_the_case_not_guessed(self):
        _, cases, anchors = parts(GROUPED)
        h = rr.case_hint("2", cases[1], "Alpha")
        self.assertEqual(h["v"], "FAIL")
        self.assertEqual(h["c"], "ORD-VAL-0001")
        self.assertTrue(h["s"].startswith("422"))
        self.assertNotIn("`", h["t"])          # code spans read as prose in a hint

    def test_link_carries_the_hint_for_pointer_and_for_assistive_tech(self):
        _, cases, anchors = parts(GROUPED)
        hints = rr.case_hints(cases, anchors)
        cell = rr.case_link_cell([("2", "case-2")], hints)
        self.assertIn('aria-label="Case 2', cell)
        self.assertIn('data-v="FAIL"', cell)
        self.assertIn('data-c="ORD-VAL-0001"', cell)
        self.assertIn('data-n="2"', cell)

    def test_link_without_a_hint_is_still_a_working_link(self):
        cell = rr.case_link_cell([("4", "case-4")], {})
        self.assertIn('<a href="#case-4">4</a>', cell)
        self.assertNotIn("aria-label", cell)

    def test_page_ships_the_tooltip_that_draws_those_hints(self):
        page = rr.build(GROUPED, "r.md")
        self.assertIn("tip.id='tip'", page)
        self.assertIn("#tip{position:fixed", page)
        self.assertIn('aria-label="Case 1', page)

    def test_report_with_no_parseable_request_still_hints(self):
        md = ("# Run\n\n### Test 1 \u2014 nothing captured\n\n**Result:** PASS\n\n"
              "### Test 2 \u2014 also nothing\n\n**Result:** FAIL\n")
        page = rr.build(md, "r.md")
        self.assertIn("no request captured", page)
        self.assertIn('aria-label="Case 2: also nothing \u2014 FAIL"', page)
        self.assertNotIn('<button class="run"', page)


class Areas(unittest.TestCase):
    """`## Details — <area>` cuts a long run into ranges the reader can hold."""

    def test_area_heading_is_recognised_in_its_several_spellings(self):
        for title in ("Details \u2014 Signup validation", "Details -- Signup validation",
                      "Details: Signup validation", "details - Signup validation"):
            self.assertEqual(rr.section_area(title), "Signup validation", title)

    def test_plain_details_heading_names_no_area(self):
        self.assertIsNone(rr.section_area("Details"))
        self.assertIsNone(rr.section_area("Summary"))

    def test_grouping_reads_range_count_and_split_off_the_cases(self):
        secs, cases, anchors = parts(GROUPED)
        areas = rr.group_cases(secs, cases, anchors)
        self.assertEqual([a.name for a in areas], ["Alpha", "Beta"])
        self.assertEqual(areas[0].span(), "1\u20132")
        self.assertEqual(areas[1].span(), "3")
        self.assertEqual(dict(areas[0].counts()), {"PASS": 1, "FAIL": 1, "SKIP": 0})
        self.assertEqual(areas[0].failing(), [("2", "case-2")])
        self.assertEqual(areas[1].failing(), [])

    def test_flat_report_is_left_flat(self):
        secs, cases, anchors = parts(FLAT)
        self.assertEqual(rr.group_cases(secs, cases, anchors), [])

    def test_a_single_area_is_not_worth_a_grouping(self):
        md = GROUPED.split("## Details \u2014 Beta")[0]
        secs, cases, anchors = parts(md)
        self.assertEqual(rr.group_cases(secs, cases, anchors), [])

    def test_a_case_outside_every_area_cancels_the_grouping(self):
        md = ("# Run\n\n### Test 0 \u2014 loose\n\n**Result:** PASS\n\n" + GROUPED)
        secs, cases, anchors = parts(md)
        self.assertEqual(rr.group_cases(secs, cases, anchors), [])

    def test_dashboard_shows_per_area_pass_and_fail(self):
        page = rr.build(GROUPED, "r.md")
        self.assertIn("<h2>Areas</h2>", page)
        panel = page.split("<h2>Areas</h2>")[1].split("</table>")[0]
        self.assertIn('href="#area-alpha"', panel)
        self.assertIn('href="#area-beta"', panel)
        self.assertIn("1 pass", panel)
        self.assertIn("1 fail", panel)
        self.assertIn('href="#case-2"', panel)      # the failing number, linked

    def test_case_list_opens_each_area_with_a_divider_that_links_back(self):
        page = rr.build(GROUPED, "r.md")
        heads = re.findall(r'<section class="area-head[^"]*" id="([^"]+)" data-fails="(\d+)"', page)
        self.assertEqual(heads, [("area-alpha", "1"), ("area-beta", "0")])
        self.assertIn("cases 1\u20132 \u00b7 2 cases \u00b7 1 pass \u00b7 1 fail", page)
        self.assertIn("Intro for alpha.", page)     # the heading's own prose comes along
        self.assertLess(page.index('id="area-beta"'), page.index('id="case-3"'))

    def test_jump_list_is_grouped_by_area(self):
        page = rr.build(GROUPED, "r.md")
        self.assertEqual(re.findall(r'<optgroup label="([^"]*)">', page), ["Alpha", "Beta"])
        self.assertEqual(page.count("</optgroup>"), 2)

    def test_case_numbers_whisper_their_area(self):
        page = rr.build(GROUPED, "r.md")
        self.assertIn('data-a="Alpha"', page)
        self.assertIn("in Alpha", page)

    def test_grouped_report_does_not_repeat_navigation_or_the_area_leads(self):
        page = rr.build(GROUPED, "r.md")
        self.assertNotIn("<h2>Navigation</h2>", page)
        self.assertNotIn("<h2>Details \u2014 Alpha</h2>", page)

    def test_ungrouped_report_keeps_its_own_navigation_prose(self):
        page = rr.build(FLAT, "r.md")
        self.assertNotIn("<h2>Areas</h2>", page)
        self.assertNotIn('<section class="area-head', page)
        self.assertNotIn("<optgroup", page)
        self.assertIn("<h2>Navigation</h2>", page)
        self.assertNotIn("<h2>Details</h2>", page)  # the flat Details heading is still dropped

    def test_cases_are_still_found_by_their_result_line_under_an_area(self):
        _, cases, _ = parts(GROUPED)
        self.assertEqual([c.verdict for c in cases], ["PASS", "FAIL", "PASS"])
        page = rr.build(GROUPED, "r.md")
        self.assertIn("<b>3</b><span>cases</span>", page)
        self.assertIn("1 of 3 cases failed", page)


if __name__ == "__main__":
    unittest.main()
