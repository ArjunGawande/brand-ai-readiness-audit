#!/usr/bin/env python3
"""
Engagement audit for the AI-readiness marketplace.

Reads the evidence file produced by crawl-render-audit (specifically the
`engagement` and `link_graph` sections added by the engagement patch) and
emits FINDINGS: defects, each with evidence and a severity, plus a
suggested_action. This is the mirror image of recommend.py — that skill
proves absence and stays quiet without a denominator; this skill proves
presence, which needs no denominator, but has to aggregate across pages and
assign severity on a documented scale instead of a hardcoded per-rule label.

Question this skill is built around, repeated for every check: if a visitor
were dropped onto THIS page alone, with no memory of the rest of the site,
would they know where they are, what this is, and what to do next?

The engine never touches the network. Every finding is a pure function of the
evidence file, so the same input always produces the same output.

Usage:
    python engagement_audit.py evidence.json
    python engagement_audit.py evidence.json --out findings_engagement.json
"""

import argparse
import json
import sys
from collections import OrderedDict

# ---------------------------------------------------------------------------
# Severity rubric — documented once, used by every rule.
# ---------------------------------------------------------------------------
# Severity is a function of two things, never a gut call per-rule:
#   1. PREVALENCE  — what fraction of sampled pages show the defect
#   2. IMPORTANCE  — is the defect on a page a cold visitor is likely to land
#                     on (homepage, high click-depth-from-home, high inbound
#                     count) or on a low-traffic page deep in the site
#
# A defect on 100% of pages including the homepage is critical. The same
# defect on 1 of 12 low-importance pages is low. This table is the only place
# severity is decided — rules compute prevalence and importance, then call
# severity_for().

SEVERITY_MATRIX = {
    # (prevalence_band, importance_band): severity
    ("high", "high"):   "critical",
    ("high", "low"):    "high",
    ("medium", "high"): "high",
    ("medium", "low"):  "medium",
    ("low", "high"):    "medium",
    ("low", "low"):     "low",
}


def prevalence_band(fraction: float) -> str:
    if fraction >= 0.7:
        return "high"
    if fraction >= 0.3:
        return "medium"
    return "low"


def importance_band(pages: list) -> str:
    """
    High if the homepage is among the offending pages, or if any offender is
    within 1 click of the homepage (the pages a real visitor actually reaches,
    and the pages an assistant is most likely to have indexed and linked to).
    """
    for p in pages:
        if p.get("type") == "homepage":
            return "high"
        depth = (p.get("_click_depth"))
        if isinstance(depth, int) and depth <= 1:
            return "high"
    return "low"


def severity_for(offenders: list, total: int) -> str:
    if total == 0:
        return "low"
    frac = len(offenders) / total
    return SEVERITY_MATRIX[(prevalence_band(frac), importance_band(offenders))]


PRIORITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


# ---------------------------------------------------------------------------
# Loading and context
# ---------------------------------------------------------------------------

def load_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def build_context(evidence):
    """
    One pass over the evidence file producing the structures every rule reads,
    so each rule stays a single readable condition — same discipline as
    recommend.py's build_context.
    """
    pages = evidence.get("pages_checked", []) or []
    ok_pages = [p for p in pages if p.get("status_code") == 200]

    # Stitch click depth from the link graph onto each page record, so
    # importance_band can read it without a second lookup structure.
    graph = evidence.get("link_graph", {}) or {}
    depth_by_url = {n["url"]: n.get("click_depth_from_home") for n in graph.get("nodes", [])}
    inbound_by_url = {n["url"]: n.get("inbound_count_in_sample") for n in graph.get("nodes", [])}
    for p in ok_pages:
        p["_click_depth"] = depth_by_url.get(p["requested_url"])
        p["_inbound_in_sample"] = inbound_by_url.get(p["requested_url"])

    return {
        "evidence": evidence,
        "pages": pages,
        "ok_pages": ok_pages,
        "total_ok": len(ok_pages),
        "graph_scope": graph.get("scope", {}),
    }


def page_ref(p: dict) -> dict:
    """Minimal identifying info for a page, used inside evidence strings."""
    return {"url": p["requested_url"], "type": p.get("type")}


def finding(fid, title, severity, evidence, suggested_action, priority="medium",
            offenders=None, extra=None):
    out = OrderedDict([
        ("id", fid),
        ("title", title),
        ("severity", severity),
        ("evidence", evidence),
        ("suggested_action", suggested_action),
    ])
    if offenders is not None:
        out["affected_pages"] = [page_ref(p) for p in offenders[:8]]
        out["affected_count"] = len(offenders)
    if extra:
        out.update(extra)
    return out


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------
# Each rule scans ctx["ok_pages"], finds offenders, and returns one finding
# (aggregated across pages) or None. Unlike recommend.py's absence rules,
# these need no coverage gate — "9 of 12 sampled pages lack an h1" is fully
# provable from the sample; the other pages don't change it.

def rule_missing_h1(ctx):
    offenders = [p for p in ctx["ok_pages"]
                 if p.get("engagement", {}).get("heading_shape", {}).get("h1_count", 0) == 0]
    if not offenders:
        return None
    total = ctx["total_ok"]
    return finding(
        "ENG-001",
        "Pages have no h1 heading",
        severity_for(offenders, total),
        f"{len(offenders)} of {total} sampled pages contain zero h1 elements: "
        + ", ".join(p["requested_url"] for p in offenders[:5]),
        {"summary": "Add exactly one h1 stating the page's main topic, placed near the top "
                    "of the main content area.",
         "priority": "high" if importance_band(offenders) == "high" else "medium"},
        offenders=offenders,
    )


def rule_multiple_h1(ctx):
    offenders = [p for p in ctx["ok_pages"]
                 if p.get("engagement", {}).get("heading_shape", {}).get("h1_count", 0) > 1]
    if not offenders:
        return None
    total = ctx["total_ok"]
    return finding(
        "ENG-002",
        "Pages declare more than one h1",
        severity_for(offenders, total),
        f"{len(offenders)} of {total} sampled pages contain multiple h1 elements, so the page "
        "has no single unambiguous topic heading: "
        + ", ".join(f'{p["requested_url"]} ({p["engagement"]["heading_shape"]["h1_count"]} h1s)'
                     for p in offenders[:5]),
        {"summary": "Keep one h1 per page for the primary topic; demote the others to h2/h3 "
                    "according to their place in the hierarchy.",
         "priority": "medium"},
        offenders=offenders,
    )


def rule_skipped_heading_levels(ctx):
    offenders = [p for p in ctx["ok_pages"]
                 if p.get("engagement", {}).get("heading_shape", {}).get("max_skip", 0) >= 2]
    if not offenders:
        return None
    total = ctx["total_ok"]
    return finding(
        "ENG-003",
        "Heading levels skip a step (e.g. h1 straight to h3)",
        severity_for(offenders, total),
        f"{len(offenders)} of {total} sampled pages skip at least one heading level, which "
        "means headings are being chosen for font size rather than document structure: "
        + ", ".join(p["requested_url"] for p in offenders[:5]),
        {"summary": "Use heading levels in strict document order (h1 then h2 then h3); style "
                    "font size with CSS instead of by choosing a different heading level.",
         "priority": "low"},
        offenders=offenders,
    )


def rule_no_orientation_deep_page(ctx):
    """
    The core cold-landing check: a page more than one click from the homepage,
    with no breadcrumb telling a visitor where they are.
    """
    offenders = [
        p for p in ctx["ok_pages"]
        if (p.get("_click_depth") or 0) >= 2
        and not p.get("engagement", {}).get("orientation", {}).get("breadcrumb_present", False)
    ]
    if not offenders:
        return None
    total = ctx["total_ok"]
    return finding(
        "ENG-004",
        "Deep pages give no orientation (no breadcrumb) for a cold arrival",
        severity_for(offenders, total),
        f"{len(offenders)} of {total} sampled pages sit 2+ clicks from the homepage and carry "
        "no breadcrumb trail, so a visitor arriving directly — the common case when an AI "
        "assistant links here — has no indication of where this page sits in the site: "
        + ", ".join(p["requested_url"] for p in offenders[:5]),
        {"summary": "Add a breadcrumb trail (visible, plus BreadcrumbList schema) to templated "
                    "deep pages, showing the path back to the relevant section and homepage.",
         "priority": "high"},
        offenders=offenders,
        extra={"note": "click depth computed over the sampled crawl only; see coverage.link_graph"},
    )


def rule_no_cta(ctx):
    offenders = [p for p in ctx["ok_pages"]
                 if p.get("engagement", {}).get("cta", {}).get("total_cta_signals", 0) == 0]
    if not offenders:
        return None
    total = ctx["total_ok"]
    return finding(
        "ENG-005",
        "Pages offer no next action",
        severity_for(offenders, total),
        f"{len(offenders)} of {total} sampled pages contain no button, form, or action-worded "
        "link anywhere in the main content: "
        + ", ".join(p["requested_url"] for p in offenders[:5]),
        {"summary": "Give each page a clear single next step relevant to its content — a link "
                    "to a relevant product/contact/signup, not a generic menu.",
         "priority": "medium"},
        offenders=offenders,
    )


def rule_dead_end_main_content(ctx):
    """
    Distinct from rule_no_cta: this asks whether the BODY TEXT links anywhere,
    not whether there's a styled button. A page can have zero forms but three
    genuinely useful in-content links, which is a healthy page this rule
    should not flag — hence checking internal_in_main_count, not total links.
    """
    offenders = [p for p in ctx["ok_pages"]
                 if (p.get("links") or {}).get("internal_in_main_count", 0) == 0
                 and (p.get("engagement", {}).get("main_text_length", 0) > 200)]
    if not offenders:
        return None
    total = ctx["total_ok"]
    return finding(
        "ENG-006",
        "Main content links nowhere else on the site",
        severity_for(offenders, total),
        f"{len(offenders)} of {total} sampled pages have substantive body text but zero "
        "internal links inside that body text — every link on the page lives in the nav/footer "
        "chrome: " + ", ".join(p["requested_url"] for p in offenders[:5]),
        {"summary": "Add contextual links from the body copy to related pages (related "
                    "products, relevant guides, a logical next page) so a visitor has somewhere "
                    "to go that follows from what they just read.",
         "priority": "medium"},
        offenders=offenders,
    )


def rule_high_boilerplate_ratio(ctx):
    offenders = [p for p in ctx["ok_pages"]
                 if p.get("engagement", {}).get("boilerplate_ratio", 0) > 0.85
                 and p.get("visible_text_length", 0) > 300]
    if not offenders:
        return None
    total = ctx["total_ok"]
    return finding(
        "ENG-007",
        "Pages are mostly navigation, with little actual content",
        severity_for(offenders, total),
        f"{len(offenders)} of {total} sampled pages have over 85% of their visible text coming "
        "from navigation/header/footer chrome rather than the page's own content: "
        + ", ".join(
            f'{p["requested_url"]} ({int(p["engagement"]["boilerplate_ratio"]*100)}% chrome)'
            for p in offenders[:5]
        ),
        {"summary": "Increase the substantive content in the main content area, or check "
                    "whether the template is duplicating navigation markup inside <main>.",
         "priority": "low"},
        offenders=offenders,
    )


def rule_thin_main_content(ctx):
    offenders = [p for p in ctx["ok_pages"]
                 if 0 < p.get("engagement", {}).get("main_text_length", 0) < 150]
    if not offenders:
        return None
    total = ctx["total_ok"]
    return finding(
        "ENG-008",
        "Pages have very little content once navigation is excluded",
        severity_for(offenders, total),
        f"{len(offenders)} of {total} sampled pages contain fewer than 150 characters of main "
        "content text: " + ", ".join(p["requested_url"] for p in offenders[:5]),
        {"summary": "Expand the page's own content, or consolidate thin pages into a fuller "
                    "page covering the same topic.",
         "priority": "medium"},
        offenders=offenders,
    )


def rule_no_viewport(ctx):
    offenders = [p for p in ctx["ok_pages"]
                 if not p.get("engagement", {}).get("orientation", {}).get("viewport_present", False)]
    if not offenders:
        return None
    total = ctx["total_ok"]
    return finding(
        "ENG-009",
        "Pages have no mobile viewport meta tag",
        severity_for(offenders, total),
        f"{len(offenders)} of {total} sampled pages lack <meta name=\"viewport\">, so mobile "
        "browsers render the desktop layout at desktop width and scale it down: "
        + ", ".join(p["requested_url"] for p in offenders[:5]),
        {"summary": "Add <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"> "
                    "to the page template.",
         "priority": "high"},
        offenders=offenders,
    )


def rule_interstitials(ctx):
    offenders = [p for p in ctx["ok_pages"]
                 if p.get("engagement", {}).get("friction", {}).get("interstitial_total", 0) >= 2]
    if not offenders:
        return None
    total = ctx["total_ok"]
    return finding(
        "ENG-010",
        "Pages carry multiple cookie/consent/popup elements",
        severity_for(offenders, total),
        f"{len(offenders)} of {total} sampled pages contain 2 or more elements matching "
        "cookie/consent/modal/popup patterns in the raw HTML: "
        + ", ".join(p["requested_url"] for p in offenders[:5]),
        {"summary": "Audit which of these actually block the page on load versus render "
                    "passively; consolidate to a single non-blocking consent banner where "
                    "possible. This is a count of markup patterns, not confirmed visual "
                    "blocking — verify manually before treating as urgent.",
         "priority": "low"},
        offenders=offenders,
    )


def rule_render_blocking_scripts(ctx):
    offenders = [p for p in ctx["ok_pages"]
                 if p.get("engagement", {}).get("friction", {}).get("render_blocking_script_count", 0) >= 4]
    if not offenders:
        return None
    total = ctx["total_ok"]
    return finding(
        "ENG-011",
        "Pages load several render-blocking scripts in <head>",
        severity_for(offenders, total),
        f"{len(offenders)} of {total} sampled pages load 4 or more scripts in <head> without "
        "async or defer, which delays first paint: "
        + ", ".join(p["requested_url"] for p in offenders[:5]),
        {"summary": "Add async or defer to non-critical <head> scripts, or move them to the "
                    "end of <body>.",
         "priority": "medium"},
        offenders=offenders,
    )


def rule_dense_prose(ctx):
    offenders = [p for p in ctx["ok_pages"]
                 if p.get("engagement", {}).get("readability", {}).get("avg_sentence_words", 0) > 28
                 and p.get("engagement", {}).get("readability", {}).get("sentence_count", 0) >= 5]
    if not offenders:
        return None
    total = ctx["total_ok"]
    return finding(
        "ENG-012",
        "Main content is written in long, dense sentences",
        severity_for(offenders, total),
        f"{len(offenders)} of {total} sampled pages average more than 28 words per sentence in "
        "the main content: "
        + ", ".join(
            f'{p["requested_url"]} ({p["engagement"]["readability"]["avg_sentence_words"]} words/sentence)'
            for p in offenders[:5]
        ),
        {"summary": "Break long compound sentences into shorter ones; aim for an average under "
                    "20 words per sentence in customer-facing copy.",
         "priority": "low"},
        offenders=offenders,
    )


def rule_images_missing_alt(ctx):
    offenders = []
    total_imgs = 0
    total_missing = 0
    for p in ctx["ok_pages"]:
        media = p.get("engagement", {}).get("media", {})
        imgs = media.get("img_count", 0)
        missing = media.get("img_missing_alt", 0)
        total_imgs += imgs
        total_missing += missing
        if imgs > 0 and missing / imgs > 0.5:
            offenders.append(p)
    if not offenders:
        return None
    total = ctx["total_ok"]
    return finding(
        "ENG-013",
        "Images are missing alt text on most pages",
        severity_for(offenders, total),
        f"{len(offenders)} of {total} sampled pages have over half their <img> tags with no alt "
        f"attribute at all ({total_missing} of {total_imgs} images sampled site-wide): "
        + ", ".join(p["requested_url"] for p in offenders[:5]),
        {"summary": "Add descriptive alt text to informational images; use alt=\"\" explicitly "
                    "for purely decorative ones so the gap is intentional, not accidental.",
         "priority": "medium"},
        offenders=offenders,
    )


def rule_orphan_pages(ctx):
    """
    Gated hard on graph scope. inbound_count_in_sample == 0 in a 12-page
    sample of an unknown-size site proves nothing — see build_link_graph's
    docstring in the crawler. Only fires when the crawl covered the known
    inventory (sitemap-exhausted or link-exhausted), same discipline
    recommend.py applies via assess_coverage.
    """
    scope = ctx["graph_scope"]
    if scope.get("is_partial", True):
        return None  # cannot prove orphan status on a partial sample
    offenders = [p for p in ctx["ok_pages"]
                 if p.get("_inbound_in_sample") == 0 and p.get("type") != "homepage"]
    if not offenders:
        return None
    total = ctx["total_ok"]
    return finding(
        "ENG-014",
        "Pages have no inbound internal links",
        severity_for(offenders, total),
        f"{len(offenders)} of {total} pages receive zero internal links from any other crawled "
        "page: " + ", ".join(p["requested_url"] for p in offenders[:5]),
        {"summary": "Link to these pages from relevant nav, related-content sections, or the "
                    "sitemap page, so visitors and crawlers can reach them by browsing.",
         "priority": "medium"},
        offenders=offenders,
    )


RULES = [
    rule_missing_h1,
    rule_multiple_h1,
    rule_skipped_heading_levels,
    rule_no_orientation_deep_page,
    rule_no_cta,
    rule_dead_end_main_content,
    rule_high_boilerplate_ratio,
    rule_thin_main_content,
    rule_no_viewport,
    rule_interstitials,
    rule_render_blocking_scripts,
    rule_dense_prose,
    rule_images_missing_alt,
    rule_orphan_pages,
]


# ---------------------------------------------------------------------------
# Pending checks — be explicit about what could not be evaluated
# ---------------------------------------------------------------------------

def pending_checks(ctx):
    out = []
    if ctx["total_ok"] == 0:
        out.append("all rules — no pages returned status 200, nothing to evaluate")
    if not ctx["evidence"].get("link_graph"):
        out.append("orphan-page detection — link_graph section not present in evidence file "
                   "(crawler needs the engagement patch's build_link_graph step)")
    elif ctx["graph_scope"].get("is_partial", True):
        out.append("orphan-page detection — link graph covers a partial sample only "
                   f"({ctx['graph_scope'].get('pages_in_graph')} pages); "
                   "zero-inbound findings suppressed to avoid false positives")
    if any("engagement" not in p for p in ctx["ok_pages"]):
        missing = sum(1 for p in ctx["ok_pages"] if "engagement" not in p)
        out.append(f"{missing} page(s) have no engagement block — crawler evidence predates "
                   "the engagement patch, or the page failed mid-parse")
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def generate(evidence):
    ctx = build_context(evidence)
    results = [r for r in (rule(ctx) for rule in RULES) if r]
    results.sort(key=lambda f: (PRIORITY_ORDER.get(f["severity"], 9), f["id"]))

    counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    for f in results:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1

    return OrderedDict([
        ("site", evidence.get("site")),
        ("audited_at", evidence.get("crawled_at")),
        ("summary", OrderedDict([
            ("total_findings", len(results)),
            ("critical", counts["critical"]),
            ("high", counts["high"]),
            ("medium", counts["medium"]),
            ("low", counts["low"]),
            ("pages_sampled", ctx["total_ok"]),
        ])),
        ("findings", results),
        ("pending_checks", pending_checks(ctx)),
    ])


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("evidence", help="evidence JSON from crawl-render-audit")
    ap.add_argument("--out", help="write here instead of stdout", default=None)
    args = ap.parse_args()

    evidence = load_json(args.evidence)
    result = generate(evidence)
    text = json.dumps(result, indent=2)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        print(text)


if __name__ == "__main__":
    main()