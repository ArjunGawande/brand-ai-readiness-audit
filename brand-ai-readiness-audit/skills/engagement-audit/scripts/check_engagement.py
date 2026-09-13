#!/usr/bin/env python3
"""
Engagement audit analyzer for brand AI-readiness marketplace.
Consumes evidence JSON produced by crawl-render-audit and evaluates:
1. Heading structure (H1 presence, H2 subheadings)
2. Clear next steps / Call-to-action buttons (CTAs)
3. Dead-end pages (pages linking nowhere useful)
4. Orphan pages (pages with zero inbound links in the sampled crawl)
5. Readability / text density (dense walls of jargon)

Usage:
  python check_engagement.py evidence.json
  python check_engagement.py - < evidence.json
"""

import datetime
import json
import sys
from collections import defaultdict
from urllib.parse import urlparse


def normalize_url(u: str) -> str:
    """Normalize URL by stripping fragments and trailing slash for graph comparisons."""
    p = urlparse(u)
    path = p.path.rstrip("/")
    return f"{p.scheme}://{p.netloc}{path}"


def finding(title: str, severity: str, evidence: str, action: str, affected_pages: list) -> dict:
    return {
        "id": None,
        "title": title,
        "severity": severity,
        "evidence": evidence,
        "suggested_action": {
            "summary": action,
            "priority": severity,
        },
        "affected_pages": affected_pages,
    }


def check_headings(pages: list) -> list:
    """Verify pages have primary H1 and substantive content has H2 subheadings."""
    findings = []
    missing_h1 = [p for p in pages if p.get("status_code") == 200 and (not p.get("h1_present") or not p.get("h1_text"))]
    if missing_h1:
        urls = [p["requested_url"] for p in missing_h1]
        findings.append(finding(
            "Pages missing primary <h1> heading",
            "high",
            f"{len(missing_h1)} of {len(pages)} sampled pages do not contain a primary <h1> tag, "
            "leaving both visitors and AI summarizers without a clear document topic.",
            "Add a single, descriptive <h1> heading to each page declaring its main topic or purpose.",
            urls,
        ))

    # Pages with long content (> 250 words) lacking subheadings
    unstructured_long = [
        p for p in pages
        if p.get("status_code") == 200
        and p.get("word_count", 0) >= 250
        and p.get("h2_count", 0) == 0
    ]
    if unstructured_long:
        urls = [p["requested_url"] for p in unstructured_long]
        findings.append(finding(
            "Substantive pages lack <h2> subheadings to break up content",
            "medium",
            f"{len(unstructured_long)} page(s) contain over 250 words but have zero <h2> subheadings, "
            "creating dense, scannability-resistant content.",
            "Break long body copy into logical sections with clear <h2> subheadings.",
            urls,
        ))

    return findings


def check_calls_to_action(pages: list) -> list:
    """Identify pages that lack buttons, inputs, or CTA links."""
    active_pages = [p for p in pages if p.get("status_code") == 200]
    if not active_pages:
        return []

    no_cta = [p for p in active_pages if p.get("cta_count", 0) == 0]
    if len(no_cta) >= max(1, len(active_pages) // 2):
        urls = [p["requested_url"] for p in no_cta]
        severity = "high" if len(no_cta) == len(active_pages) else "medium"
        return [finding(
            "Sampled pages lack clear calls-to-action (CTAs) or next steps",
            severity,
            f"{len(no_cta)} of {len(active_pages)} sampled pages contain 0 buttons or CTA elements, "
            "providing visitors and automated assistants with no clear next step or conversion path.",
            "Introduce visible, accessible action buttons or highlighted next-step links (e.g. 'Get Started', 'Contact Us', 'Learn More').",
            urls,
        )]
    return []


def check_dead_ends(pages: list) -> list:
    """Check for pages that link nowhere useful (zero outbound internal links)."""
    active_pages = [p for p in pages if p.get("status_code") == 200]
    dead_ends = [p for p in active_pages if p.get("outbound_internal_links_count", 0) == 0]
    if dead_ends:
        urls = [p["requested_url"] for p in dead_ends]
        return [finding(
            "Dead-end pages detected with no internal outbound links",
            "high",
            f"{len(dead_ends)} page(s) contain 0 internal links to other sections of the website, "
            "stranding visitors and preventing search/AI crawlers from continuing their journey.",
            "Ensure all pages include primary navigation, contextual related links, or a footer directory.",
            urls,
        )]
    return []


def check_orphan_pages(pages: list, site_url: str) -> list:
    """Check if any sampled non-homepage page has 0 inbound links from any other sampled page."""
    active_pages = [p for p in pages if p.get("status_code") == 200]
    if len(active_pages) < 2:
        return []

    # Collect all normalized outbound links
    all_referenced_urls = set()
    for p in active_pages:
        for link in p.get("outbound_internal_urls", []):
            all_referenced_urls.add(normalize_url(link))

    norm_site = normalize_url(site_url)
    orphans = []
    for p in active_pages:
        norm_req = normalize_url(p["requested_url"])
        norm_final = normalize_url(p.get("final_url", p["requested_url"]))
        # Skip homepage / entrypoint
        if norm_req == norm_site or norm_final == norm_site or p.get("type") == "homepage":
            continue
        if norm_req not in all_referenced_urls and norm_final not in all_referenced_urls:
            orphans.append(p["requested_url"])

    if orphans:
        return [finding(
            "Orphan or weakly connected internal pages detected",
            "medium",
            f"{len(orphans)} sampled page(s) have zero incoming links from any other sampled pages: "
            f"{', '.join(orphans[:3])}{'...' if len(orphans) > 3 else ''}.",
            "Link these pages from relevant parent categories, navigation menus, or related articles to ensure discoverability.",
            orphans,
        )]
    return []


def check_readability(pages: list) -> list:
    """Check for walls of dense jargon (low Flesch reading ease)."""
    active_pages = [p for p in pages if p.get("status_code") == 200 and p.get("word_count", 0) >= 100]
    jargon_pages = [p for p in active_pages if p.get("reading_ease_score", 100.0) < 30.0]
    if jargon_pages:
        urls = [p["requested_url"] for p in jargon_pages]
        avg_score = round(sum(p.get("reading_ease_score", 0) for p in jargon_pages) / len(jargon_pages), 1)
        severity = "high" if avg_score < 15.0 else "medium"
        return [finding(
            "Content readability is very low (dense wall of text/jargon)",
            severity,
            f"{len(jargon_pages)} page(s) scored below 30 on the Flesch Reading Ease index (average {avg_score}/100), "
            "indicating overly complex sentence structures and dense jargon that reduce comprehension.",
            "Simplify sentence structure, write in active voice, and reduce polysyllabic jargon to improve readability for both humans and AI models.",
            urls,
        )]
    return []


def analyze(data: dict) -> dict:
    """Run all engagement checks against the crawler evidence."""
    site = data.get("site", "")
    pages = data.get("pages_checked", [])

    if not pages:
        findings = [finding(
            "Engagement audit could not be performed — no pages were accessible",
            "high",
            "The crawl yielded 0 accessible pages (likely blocked upfront by WAF or network error).",
            "Resolve site accessibility or firewall rules before running the engagement audit.",
            [],
        )]
    else:
        findings = (
            check_headings(pages)
            + check_calls_to_action(pages)
            + check_dead_ends(pages)
            + check_orphan_pages(pages, site)
            + check_readability(pages)
        )

    for i, f in enumerate(findings, start=1):
        f["id"] = f"F-ENG-{i:03d}"

    counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    for f in findings:
        sev = f.get("severity", "medium")
        if sev in counts:
            counts[sev] += 1

    return {
        "skill": "engagement-audit",
        "site": site,
        "audited_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "summary": {
            "total_findings": len(findings),
            **counts,
        },
        "findings": findings,
    }


def main():
    if len(sys.argv) < 2:
        print("Usage: python check_engagement.py evidence.json (or - for stdin)")
        sys.exit(1)

    input_source = sys.argv[1]
    try:
        if input_source == "-":
            data = json.load(sys.stdin)
        else:
            with open(input_source, "r", encoding="utf-8") as f:
                data = json.load(f)
    except FileNotFoundError:
        print(f"Error: file not found: {input_source}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Error: invalid JSON in {input_source}: {e}")
        sys.exit(1)

    result = analyze(data)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
