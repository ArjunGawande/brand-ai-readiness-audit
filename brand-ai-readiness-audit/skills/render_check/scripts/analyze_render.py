#!/usr/bin/env python3
"""
Render-gap analyzer for the AI-readiness audit.

Reads the evidence file produced by crawl.py and emits findings about
content that is invisible to non-JS-executing crawlers.

This script makes NO network requests. It only judges recorded facts.
That split is deliberate: crawl.py records, this script judges.

Usage:
    python crawl.py https://example.com > evidence.json
    python analyze_render.py evidence.json

    # or piped:
    python crawl.py https://example.com | python analyze_render.py -
"""

import json
import sys
from collections import defaultdict

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------
# WHY THESE NUMBERS:
# text_delta_ratio = (rendered - raw) / rendered
#   = what fraction of the human-visible text is MISSING from raw HTML.
#
# The bands below are deliberately conservative at the low end to avoid
# false positives. Small deltas are normal and harmless: cookie banners,
# lazy-loaded footers, chat widgets, and analytics all inject a little
# text after load without hiding anything that matters. A finding is only
# raised once enough content is JS-only that a crawler would materially
# misunderstand the page.

DELTA_CRITICAL = 0.70   # most of the page is JS-only
DELTA_HIGH     = 0.40   # a large share of the page is JS-only
DELTA_MEDIUM   = 0.15   # a noticeable share is JS-only
DELTA_NOISE    = 0.15   # below this: normal widget noise, not a finding

# Below this raw character count, a page has essentially no readable text
# for a non-JS crawler regardless of what the ratio says.
RAW_TEXT_FLOOR = 250

# Serialization artifact guard. Playwright's page.content() normalizes
# whitespace/tags slightly differently than the raw HTTP body, so rendered
# text can measure a hair SHORTER than raw even on a purely static page.
# Anything within this band is treated as measurement noise, not content loss.
SERIALIZATION_TOLERANCE = 0.08

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


# ---------------------------------------------------------------------------
# Finding builder
# ---------------------------------------------------------------------------

class FindingBuilder:
    """Assigns sequential F-001 style IDs and collects findings."""

    def __init__(self, prefix="RG"):
        self.prefix = prefix
        self._n = 0
        self.findings = []

    def add(self, title, severity, evidence, action_summary, priority,
            action_detail=None, affected=None):
        self._n += 1
        finding = {
            "id": f"F-{self.prefix}-{self._n:03d}",
            "title": title,
            "severity": severity,
            "evidence": evidence,
            "suggested_action": {
                "summary": action_summary,
                "priority": priority,
            },
        }
        if action_detail:
            finding["suggested_action"]["detail"] = action_detail
        if affected:
            finding["affected_pages"] = affected
        self.findings.append(finding)
        return finding


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def severity_for_delta(delta, raw_len):
    """
    Map a text_delta_ratio to a severity band.
    Returns None when the gap is small enough to be normal widget noise.
    """
    # A page with almost no raw text is a problem regardless of ratio —
    # if raw is 40 chars and rendered is 300, the ratio is 0.87 but the
    # absolute numbers matter more: nothing readable was served.
    if raw_len < RAW_TEXT_FLOOR and delta >= DELTA_MEDIUM:
        return "critical"

    if delta >= DELTA_CRITICAL:
        return "critical"
    if delta >= DELTA_HIGH:
        return "high"
    if delta >= DELTA_MEDIUM:
        return "medium"
    return None


def pct(x):
    return f"{round(x * 100)}%"


def group_by_template(pages):
    """Group rendered page results by page_type."""
    groups = defaultdict(list)
    for p in pages:
        if p.get("render_failed"):
            continue
        groups[p.get("page_type", "unknown")].append(p)
    return groups


# ---------------------------------------------------------------------------
# Check 1 — the core render gap
# ---------------------------------------------------------------------------

def check_render_gap(groups, fb):
    """
    Per template type, decide whether content is JS-dependent.

    Reported per TEMPLATE, not per page. A site with 40 product pages built
    from one template has one problem, not 40 — and the fix is one template
    edit. Reporting per page would inflate the finding count and bury the
    actual root cause.
    """
    for ptype, pages in sorted(groups.items()):
        deltas = [p["text_delta_ratio"] for p in pages]
        raws = [p["raw_text_len"] for p in pages]

        worst = max(deltas)
        worst_page = pages[deltas.index(worst)]
        min_raw = min(raws)

        sev = severity_for_delta(worst, min_raw)
        if sev is None:
            continue

        n = len(pages)
        affected = [p["url"] for p in pages
                    if severity_for_delta(p["text_delta_ratio"], p["raw_text_len"])]

        evidence = (
            f"Template '{ptype}': sampled {len(affected)}/{n} page(s) where most "
            f"visible text is absent from the server HTML. "
            f"Worst case {worst_page['url']} — raw HTML contained "
            f"{worst_page['raw_text_len']} chars of text vs "
            f"{worst_page['rendered_text_len']} chars after JavaScript executed "
            f"({pct(worst)} of visible content is JS-only)."
        )

        fb.add(
            title=f"Primary content on '{ptype}' pages requires JavaScript to appear",
            severity=sev,
            evidence=evidence,
            action_summary=(
                f"Server-render the main content of the '{ptype}' template so it is "
                f"present in the initial HTML response."
            ),
            action_detail=(
                "Most AI crawlers fetch raw HTML and do not execute JavaScript, so "
                "they currently receive a near-empty shell for these pages. Move the "
                "primary content to server-side rendering (SSR) or static generation "
                "(SSG) — in Next.js use getServerSideProps/getStaticProps or a Server "
                "Component; in Nuxt use SSR mode; in a plain SPA add a prerender step. "
                "If a full SSR migration is not feasible short-term, add a <noscript> "
                "block containing the page's key facts (name, description, price, "
                "specifications) as plain text — a partial mitigation, not a "
                "substitute for SSR."
            ),
            priority=sev,
            affected=affected,
        )


# ---------------------------------------------------------------------------
# Check 2 — structured data that only exists after JS
# ---------------------------------------------------------------------------

def check_jsonld_gap(groups, fb):
    """
    JSON-LD injected by JavaScript is invisible to non-rendering crawlers.
    This is separate from the text gap: a page can be fully server-rendered
    for text and STILL inject its schema.org markup via a tag manager.
    """
    for ptype, pages in sorted(groups.items()):
        gapped = [p for p in pages
                  if p.get("jsonld_blocks_rendered", 0) > p.get("jsonld_blocks_raw", 0)]
        if not gapped:
            continue

        ex = gapped[0]
        fb.add(
            title=f"Structured data on '{ptype}' pages is injected by JavaScript",
            severity="high",
            evidence=(
                f"Template '{ptype}': {len(gapped)}/{len(pages)} sampled page(s) gained "
                f"JSON-LD only after rendering. Example {ex['url']} — "
                f"{ex['jsonld_blocks_raw']} block(s) in raw HTML vs "
                f"{ex['jsonld_blocks_rendered']} after JavaScript executed."
            ),
            action_summary=(
                "Emit JSON-LD server-side in the initial HTML rather than injecting it "
                "client-side."
            ),
            action_detail=(
                "Structured data is the most reliable way for an assistant to extract "
                "an exact fact, but it only helps if the crawler sees it. Schema "
                "injected through Google Tag Manager or a client-side script is absent "
                "from the response most AI crawlers actually read. Render the "
                "<script type=\"application/ld+json\"> block directly in the page "
                "template on the server."
            ),
            priority="high",
        )


# ---------------------------------------------------------------------------
# Check 3 — title and h1 that only resolve after JS
# ---------------------------------------------------------------------------

def check_title_h1_gap(groups, fb):
    """
    A generic raw title that becomes specific after render is the classic
    SPA signature: the shell ships one title for every route and JS rewrites
    it. A crawler indexes the shell title, so every page looks identical.
    """
    title_gapped = []
    h1_gapped = []

    for ptype, pages in groups.items():
        for p in pages:
            raw_t = (p.get("raw_title") or "").strip()
            rend_t = (p.get("rendered_title") or "").strip()
            if raw_t and rend_t and raw_t != rend_t:
                title_gapped.append((ptype, p, raw_t, rend_t))

            # Only flag the direction that indicates a real gap:
            # missing in raw, present after render.
            if p.get("h1_present_rendered") and not p.get("h1_present_raw"):
                h1_gapped.append((ptype, p))

    if title_gapped:
        ptype, p, raw_t, rend_t = title_gapped[0]
        fb.add(
            title="Page titles are rewritten by JavaScript after load",
            severity="high",
            evidence=(
                f"{len(title_gapped)} sampled page(s) served one <title> in raw HTML "
                f"and a different one after rendering. Example {p['url']} — "
                f"raw: \"{raw_t}\" / rendered: \"{rend_t}\"."
            ),
            action_summary="Set the correct, page-specific <title> server-side.",
            action_detail=(
                "The title is one of the strongest signals for what a page is about "
                "and is frequently used verbatim when an assistant cites a source. If "
                "every route ships the same shell title and JS rewrites it afterward, "
                "a non-rendering crawler sees identical titles across the whole site "
                "and cannot tell the pages apart."
            ),
            priority="high",
            affected=[x[1]["url"] for x in title_gapped],
        )

    if h1_gapped:
        ptype, p = h1_gapped[0]
        fb.add(
            title="Primary heading (h1) is added by JavaScript",
            severity="medium",
            evidence=(
                f"{len(h1_gapped)} sampled page(s) had no <h1> in raw HTML but did "
                f"after rendering. Example: {p['url']}."
            ),
            action_summary="Render the h1 server-side in the page template.",
            action_detail=(
                "The h1 states the page's primary topic in one line. Without it in the "
                "raw HTML, a crawler has to infer the subject from surrounding text, "
                "which is less reliable and more error-prone."
            ),
            priority="medium",
            affected=[x[1]["url"] for x in h1_gapped],
        )


# ---------------------------------------------------------------------------
# Check 4 — inconsistent rendering within one template
# ---------------------------------------------------------------------------

def check_template_inconsistency(render_check, groups, fb):
    """
    Two pages built from the same template that disagree on delta usually
    means partial hydration or a conditional render path. It matters because
    it breaks the generalization the sampling relies on — you can no longer
    assume the rest of that template behaves like the sample.
    """
    if not render_check.get("template_inconsistent"):
        return

    detail_bits = []
    for ptype, pages in groups.items():
        if len(pages) < 2:
            continue
        deltas = [p["text_delta_ratio"] for p in pages]
        if max(deltas) - min(deltas) > 0.3:
            detail_bits.append(
                f"'{ptype}' ranged {min(deltas):.2f}–{max(deltas):.2f}"
            )

    fb.add(
        title="Rendering behaviour is inconsistent within the same page template",
        severity="medium",
        evidence=(
            "Pages sharing a template produced materially different raw-vs-rendered "
            "gaps: " + "; ".join(detail_bits) + ". "
            "Sampling assumes pages of one template behave alike, so this reduces "
            "confidence that unsampled pages of these templates are safe."
        ),
        action_summary=(
            "Audit the template for conditional client-side rendering paths and make "
            "server output consistent across all its pages."
        ),
        action_detail=(
            "Common causes are partial hydration, A/B test branches, feature flags, "
            "or content that falls back to a client fetch when a cache misses. Until "
            "it is consistent, treat the worst-case page as representative."
        ),
        priority="medium",
    )


# ---------------------------------------------------------------------------
# Check 5 — coverage and data-quality caveats (not site defects)
# ---------------------------------------------------------------------------

def check_coverage(evidence, render_check, groups, fb):
    """
    Emits informational findings about the audit's own limits. These are not
    problems with the site — they tell the reader how much to trust the above.
    Omitting them would overstate the audit's confidence.
    """
    status = render_check.get("status")

    if status in ("unavailable", "error", "skipped"):
        reason = (render_check.get("reason") or "").split("\n")[0][:200]
        fb.add(
            title="Render-gap check did not run — JavaScript dependency unverified",
            severity="info",
            evidence=f"render_check.status = '{status}'. Reason: {reason}",
            action_summary=(
                "Install the headless browser and re-run to confirm whether content "
                "is JavaScript-dependent."
            ),
            action_detail=(
                "Run: pip install playwright && playwright install chromium. "
                "Note that `pip install playwright` alone does not download the "
                "browser binary; the second command is required. Until this check "
                "runs, no conclusion either way can be drawn about JS-dependent "
                "content."
            ),
            priority="medium",
        )
        return

    # Coverage note: how much of the crawl was actually rendered.
    summary = evidence.get("crawl_summary", {})
    attempted = summary.get("pages_attempted", 0)
    rendered = len([p for pages in groups.values() for p in pages])
    if attempted and rendered < attempted:
        fb.add(
            title="Render check covered a sample, not every crawled page",
            severity="info",
            evidence=(
                f"{rendered} of {attempted} crawled page(s) were rendered, covering "
                f"{len(groups)} template type(s): {', '.join(sorted(groups))}. "
                "Rendering is a template-level property, so findings generalize to "
                "unsampled pages of the same templates."
            ),
            action_summary="No action required — recorded so coverage is not overstated.",
            priority="low",
        )

    # Failed renders, if any.
    failed = [p for p in render_check.get("pages", []) if p.get("render_failed")]
    if failed:
        fb.add(
            title="Some pages could not be rendered",
            severity="info",
            evidence=(
                f"{len(failed)} page(s) failed to render (timeout or navigation error): "
                + ", ".join(p["url"] for p in failed[:5])
            ),
            action_summary=(
                "Re-run the audit; if these pages fail repeatedly, check for very slow "
                "load times, which harms both crawling and on-site engagement."
            ),
            priority="low",
        )

    # Serialization artifact note — prevents misreading negative deltas.
    artifacts = []
    for pages in groups.values():
        for p in pages:
            r, w = p.get("raw_text_len", 0), p.get("rendered_text_len", 0)
            if w and r > w and (r - w) / r <= SERIALIZATION_TOLERANCE:
                artifacts.append(p["url"])
    if artifacts:
        fb.add(
            title="Rendered text measured marginally shorter than raw on some pages",
            severity="info",
            evidence=(
                f"{len(artifacts)} page(s) measured slightly less text after rendering "
                f"(within {pct(SERIALIZATION_TOLERANCE)} tolerance). This is DOM "
                "serialization normalising whitespace, not content loss. Deltas are "
                "clamped at 0.0 and no gap was reported for these pages."
            ),
            action_summary="No action required — measurement artifact, not a site issue.",
            priority="low",
        )


# ---------------------------------------------------------------------------
# Clean-pass note
# ---------------------------------------------------------------------------

def note_clean_pass(groups, fb, had_gap_findings):
    """
    An audit that reports nothing when nothing is wrong is hard to trust —
    the reader cannot tell 'checked and fine' from 'never checked'. This
    records the positive result explicitly.
    """
    if had_gap_findings or not groups:
        return

    worst = max(
        (p["text_delta_ratio"] for pages in groups.values() for p in pages),
        default=0.0,
    )
    fb.add(
        title="Content is server-rendered — no JavaScript dependency detected",
        severity="info",
        evidence=(
            f"Across {len(groups)} template type(s), the largest raw-vs-rendered text "
            f"gap was {pct(worst)}, below the {pct(DELTA_NOISE)} noise threshold. "
            "Crawlers that do not execute JavaScript receive substantially the same "
            "content a browser does."
        ),
        action_summary=(
            "No fix needed. To strengthen further, keep new templates server-rendered "
            "and avoid moving existing content behind client-side fetches."
        ),
        priority="low",
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def analyze(evidence: dict) -> dict:
    fb = FindingBuilder(prefix="RG")

    render_check = evidence.get("render_check") or {}
    groups = group_by_template(render_check.get("pages", []))

    if render_check.get("status") == "completed":
        before = len(fb.findings)
        check_render_gap(groups, fb)
        check_jsonld_gap(groups, fb)
        check_title_h1_gap(groups, fb)
        check_template_inconsistency(render_check, groups, fb)
        had_gaps = len(fb.findings) > before
        note_clean_pass(groups, fb, had_gaps)

    check_coverage(evidence, render_check, groups, fb)

    # Sort: most severe first, stable within a band.
    fb.findings.sort(key=lambda f: SEVERITY_ORDER.get(f["severity"], 9))

    counts = defaultdict(int)
    for f in fb.findings:
        counts[f["severity"]] += 1

    return {
        "skill": "render-gap-audit",
        "site": evidence.get("site"),
        "analyzed_from": evidence.get("crawled_at"),
        "summary": {
            "total_findings": len(fb.findings),
            "critical": counts["critical"],
            "high": counts["high"],
            "medium": counts["medium"],
            "low": counts["low"],
            "info": counts["info"],
        },
        "findings": fb.findings,
    }


def main():
    if len(sys.argv) < 2:
        print("Usage: python analyze_render.py <evidence.json | ->", file=sys.stderr)
        sys.exit(1)

    src = sys.argv[1]
    try:
        raw = sys.stdin.read() if src == "-" else open(src, encoding="utf-8").read()
        evidence = json.loads(raw)
    except FileNotFoundError:
        print(f"Evidence file not found: {src}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Evidence file is not valid JSON: {e}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps(analyze(evidence), indent=2))


if __name__ == "__main__":
    main()