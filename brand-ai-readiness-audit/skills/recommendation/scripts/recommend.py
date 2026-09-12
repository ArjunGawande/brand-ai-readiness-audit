#!/usr/bin/env python3
"""
Proactive recommendation engine for the AI-readiness audit.

Reads the evidence file produced by crawl-render-audit (and, optionally, the
findings already assembled by the other audit skills) and emits additions the
site could make to strengthen AI discoverability and engagement.

This does NOT report defects. Defects are findings, with evidence and a
severity, and they belong to the detection skills. Everything emitted here is
an addition to a site that may be working perfectly well, so it carries a
rationale / priority / effort instead. Keeping the two vocabularies apart is
what stops "site has no FAQ page" from being dressed up as a bug.

The engine never touches the network. Every recommendation is a pure function
of the evidence file, so the same input always produces the same output.

Usage:
    python recommend.py evidence.json
    python recommend.py evidence.json --findings findings.json
    python recommend.py evidence.json --out proactive.json
"""

import argparse
import json
import re
import sys
from collections import OrderedDict

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Path groups we look for when asking "does the site have a page of this kind?"
# Matched against the `type` field of pages_checked and against sitemap URLs.
PAGE_FAMILIES = {
    "pricing":      ["pricing", "plans", "cost", "price"],
    "faq":          ["faq", "faqs", "help", "support", "questions"],
    "blog":         ["blog", "news", "insights", "articles", "updates", "changelog", "press"],
    "case_studies": ["case-stud", "casestud", "customers", "clients", "work", "portfolio", "success"],
    "comparison":   ["compare", "comparison", "vs", "alternatives"],
    "glossary":     ["glossary", "terms", "definitions"],
    "contact":      ["contact", "get-in-touch", "reach-us"],
}

# Schema.org types that declare "here is a thing we sell/offer".
COMMERCIAL_TYPES = {"Product", "Offer", "Service", "AggregateOffer", "ProductGroup"}

# Properties that carry a freshness signal.
FRESHNESS_KEYS = {"dateModified", "datePublished", "dateCreated", "uploadDate"}

# Below this fraction of digits in the sampled prose, nothing on the site is
# concrete enough to be lifted as a standalone fact.
DIGIT_DENSITY_FLOOR = 0.004

# A rationale is only worth printing if it can name the numbers behind it.
PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}


# ---------------------------------------------------------------------------
# Evidence loading and normalisation
# ---------------------------------------------------------------------------

def load_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def flatten_jsonld(node, out):
    """
    Walk a parsed JSON-LD value and collect every dict that has an @type.

    Real-world markup nests entities several ways: a top-level @graph array, a
    mainEntity pointing at an Organization, a founder pointing at a Person. We
    want all of them, because a rule like "is there a Product type anywhere"
    should not care how deeply the site buried it.
    """
    if isinstance(node, list):
        for item in node:
            flatten_jsonld(item, out)
        return
    if not isinstance(node, dict):
        return
    if "@type" in node:
        out.append(node)
    for value in node.values():
        if isinstance(value, (dict, list)):
            flatten_jsonld(value, out)


def type_names(node):
    """@type is a string on most sites and a list on a few. Normalise to a set."""
    raw = node.get("@type")
    if isinstance(raw, str):
        return {raw}
    if isinstance(raw, list):
        return {t for t in raw if isinstance(t, str)}
    return set()


def build_context(evidence, findings):
    """
    Turn the raw evidence file into the handful of derived structures every
    rule reads. Doing this once keeps each rule to a single readable condition.
    """
    pages = evidence.get("pages_checked", []) or []
    ok_pages = [p for p in pages if p.get("status_code") == 200]

    nodes = []
    parse_errors = 0
    for page in pages:
        for block in page.get("jsonld_raw", []) or []:
            try:
                flatten_jsonld(json.loads(block), nodes)
            except (json.JSONDecodeError, TypeError):
                # A block we cannot parse means this page's schema is UNKNOWN,
                # not absent. Rules that assert absence must stand down.
                parse_errors += 1

    types_present = set()
    for node in nodes:
        types_present |= type_names(node)

    org_node = next((n for n in nodes if "Organization" in type_names(n)), None)
    # Some sites use a subtype (LocalBusiness, Corporation) instead.
    if org_node is None:
        org_node = next(
            (n for n in nodes
             if type_names(n) & {"LocalBusiness", "Corporation", "NGO", "EducationalOrganization"}),
            None,
        )

    # Every URL we know to exist: crawled pages plus anything the sitemap lists.
    sitemap = evidence.get("sitemap", {}) or {}
    sitemap_urls = sitemap.get("urls", []) or []
    known_urls = [p.get("requested_url", "") for p in pages] + sitemap_urls

    path_groups = {p.get("type") for p in pages if p.get("type")}

    return {
        "evidence": evidence,
        "findings": findings,
        "suppressed": collect_suppressed(findings),
        "pages": pages,
        "ok_pages": ok_pages,
        "nodes": nodes,
        "parse_errors": parse_errors,
        "schema_reliable": parse_errors == 0,
        "types_present": types_present,
        "org_node": org_node,
        "path_groups": path_groups,
        "known_urls": known_urls,
        "sitemap": sitemap,
        "llms_txt": evidence.get("llms_txt", {}) or {},
        "coverage": evidence.get("coverage", {}) or {},
    }


def collect_suppressed(findings):
    """
    Gather the identifiers of problems the detection skills already reported,
    so we do not hand back the same advice wearing a different label.
    """
    out = set()
    for f in findings or []:
        for key in ("check_id", "rule_id", "id"):
            val = f.get(key)
            if isinstance(val, str):
                out.add(val.upper())
        title = f.get("title")
        if isinstance(title, str):
            out.add(title.strip().lower())
    return out


def is_suppressed(ctx, check_ids, keywords=()):
    """
    True when another skill has already covered this ground. We match on
    explicit check ids first, then fall back to keyword overlap with finding
    titles, because not every upstream skill will use our id vocabulary.
    """
    for cid in check_ids:
        if cid.upper() in ctx["suppressed"]:
            return True
    for title in ctx["suppressed"]:
        if all(k in title for k in keywords) and keywords:
            return True
    return False


# ---------------------------------------------------------------------------
# Coverage: how much of the site did we actually see?
# ---------------------------------------------------------------------------

def assess_coverage(ctx):
    """
    Absence claims are only as good as the crawl behind them. Work out whether
    "I did not find a pricing page" means "the site has none" or "I stopped
    looking after 12 pages".

    high   - we saw the whole link graph, or half of a published sitemap
    medium - partial view, but we have a denominator we can quote
    low    - we were truncated and cannot prove absence of anything
    """
    cov = ctx["coverage"]
    sitemap = ctx["sitemap"]
    sampled = len(ctx["pages"]) or cov.get("pages_sampled", 0)
    discovered = cov.get("links_discovered")
    sitemap_count = sitemap.get("url_count", 0) if sitemap.get("found") else 0

    if sitemap_count:
        ratio = sampled / sitemap_count if sitemap_count else 0
        level = "high" if ratio >= 0.5 or sitemap_count <= sampled else "medium"
        return {
            "level": level,
            "denominator": sitemap_count,
            "sampled": sampled,
            "basis": "sitemap",
            "phrase": f"the sitemap lists {sitemap_count} URLs",
        }

    if isinstance(discovered, int):
        if discovered <= sampled:
            return {
                "level": "high",
                "denominator": discovered,
                "sampled": sampled,
                "basis": "exhausted_links",
                "phrase": f"all {sampled} internal links from the homepage were crawled",
            }
        return {
            "level": "low",
            "denominator": discovered,
            "sampled": sampled,
            "basis": "truncated_links",
            "phrase": f"only {sampled} of {discovered} discovered links were crawled",
        }

    # No sitemap and no link count — we cannot tell a small site from a truncated
    # crawl. Treat as unknown and let the gate drop absence rules.
    return {
        "level": "unknown",
        "denominator": None,
        "sampled": sampled,
        "basis": "no_denominator",
        "phrase": f"{sampled} pages sampled; total page count unknown",
    }


def has_page_family(ctx, family):
    """Does any URL we know about look like a page of this kind?"""
    needles = PAGE_FAMILIES[family]
    for url in ctx["known_urls"]:
        low = (url or "").lower()
        if any(n in low for n in needles):
            return True
    for group in ctx["path_groups"]:
        low = (group or "").lower()
        if any(n in low for n in needles):
            return True
    return False





# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------
# Each rule takes the context and returns a recommendation dict or None.
# Rules are grouped by how reliable their evidence is:
#
#   key-absence  - we hold the object and the property is not on it.
#                  Unaffected by crawl size. Always safe to emit.
#   type-absence - no page we sampled declares this schema type.
#                  Weakened by a small sample, not invalidated.
#   path-absence - no page of this kind exists. Entirely dependent on
#                  coverage; dropped outright when coverage is low.
# ---------------------------------------------------------------------------

def rec(rid, title, rationale, priority, effort, confidence, kind, trigger):
    return OrderedDict([
        ("id", rid),
        ("title", title),
        ("rationale", rationale),
        ("priority", priority),
        ("effort", effort),
        ("confidence", confidence),
        ("evidence_class", kind),
        ("trigger", trigger),
    ])


def rule_sameas(ctx):
    """The strongest corroboration signal a site controls, and a two-line edit."""
    org = ctx["org_node"]
    if org is None or not ctx["schema_reliable"]:
        return None
    if org.get("sameAs"):
        return None
    if is_suppressed(ctx, ["ENT-SAMEAS", "SD-SAMEAS"], ("sameas",)):
        return None
    pages_with_org = sum(
        1 for p in ctx["ok_pages"]
        for b in (p.get("jsonld_raw") or [])
        if "Organization" in b
    )
    return rec(
        "P-002",
        "Add sameAs links to the Organization schema",
        f"An Organization node is present on {pages_with_org} of {len(ctx['ok_pages'])} crawled "
        "pages but declares no sameAs property, so the site links to no independent profile "
        "(LinkedIn, Crunchbase, GitHub, a registry listing). Assistants weight a fact more "
        "heavily when several unrelated sources agree on it; right now the only source asserting "
        "this entity is the site itself.",
        "high", "low", "high", "key-absence",
        {"field": "Organization.sameAs", "condition": "absent"},
    )


def rule_commercial_schema(ctx):
    """A company that never machine-declares what it sells."""
    if not ctx["schema_reliable"] or len(ctx["ok_pages"]) < 2:
        return None
    if ctx["types_present"] & COMMERCIAL_TYPES:
        return None
    if is_suppressed(ctx, ["SD-OFFER-MISSING", "SD-PRODUCT-MISSING"], ("product", "schema")):
        return None
    found = ", ".join(sorted(ctx["types_present"])) or "none"
    return rec(
        "P-003",
        "Declare what the business sells with Service, Product or Offer schema",
        f"Across {len(ctx['ok_pages'])} crawled pages the only schema.org types present are: "
        f"{found}. None of Service, Product or Offer appears, so nothing on the site states "
        "machine-readably what is actually being offered. A page describing each service with "
        "Service or Offer markup gives an assistant a structured answer to \"what do they do\".",
        "medium", "medium", "high", "type-absence",
        {"field": "jsonld.@type", "condition": f"no member of {sorted(COMMERCIAL_TYPES)}"},
    )


def rule_address_contact(ctx):
    """Location and contact are among the most-asked entity questions."""
    org = ctx["org_node"]
    if org is None or not ctx["schema_reliable"]:
        return None
    address = org.get("address")
    thin_address = (
        isinstance(address, dict)
        and not (set(address) - {"@type", "addressCountry"})
    )
    no_contact = "contactPoint" not in org and "telephone" not in org and "email" not in org
    if not (thin_address or no_contact):
        return None
    if is_suppressed(ctx, ["ENT-ADDRESS"], ("address",)):
        return None

    parts = []
    if thin_address:
        keys = ", ".join(k for k in address if k != "@type") or "no properties"
        parts.append(f"the PostalAddress node carries only {keys}")
    if no_contact:
        parts.append("the Organization node has no contactPoint, telephone or email")
    joined = "; ".join(parts)
    joined = joined[:1].upper() + joined[1:]  # not .capitalize(): it would flatten camelCase
    return rec(
        "P-006",
        "Complete the postal address and add a contactPoint",
        joined
        + ". \"Where are they based\" and \"how do I contact them\" are among the most common "
        "entity questions asked of assistants, and neither is currently answerable from the "
        "site's own structured data.",
        "medium", "low", "high", "key-absence",
        {"field": "Organization.address / Organization.contactPoint", "condition": "incomplete"},
    )


def rule_person_entity(ctx):
    """A founder nested inside an Organization rarely resolves as an entity."""
    if not ctx["schema_reliable"]:
        return None
    standalone = [n for n in ctx["nodes"] if "Person" in type_names(n) and n.get("url")]
    if standalone:
        return None
    nested_names = []
    for node in ctx["nodes"]:
        for key in ("founder", "employee", "author", "member"):
            val = node.get(key)
            if isinstance(val, dict) and val.get("name"):
                nested_names.append(val["name"])
    if not nested_names:
        return None
    if is_suppressed(ctx, ["ENT-PERSON"], ("person", "schema")):
        return None
    who = nested_names[0]
    return rec(
        "P-005",
        "Give key people their own resolvable Person entity",
        f"{who} appears only as a nested property inside another node, never as a standalone "
        "Person entity with its own url and sameAs links. Nested people are frequently dropped "
        "during entity resolution, so the person and the brand are not connected in the way an "
        "assistant can follow.",
        "low", "medium", "high", "key-absence",
        {"field": "Person node", "condition": "nested only, no standalone entity"},
    )


def rule_quotable_facts(ctx):
    """Prose with no numbers in it gives an assistant nothing to lift."""
    if is_suppressed(ctx, ["ENG-THIN", "CONTENT-VAGUE"], ("vague",)):
        return None

    total_chars = 0
    total_digits = 0
    
    for page in ctx["ok_pages"]:
        stats = page.get("text_stats")
        if stats:
            total_chars += stats.get("char_count", 0)
            total_digits += stats.get("digit_count", 0)
            
    if total_chars < 120:
        return None
        
    density = total_digits / total_chars
    if density >= DIGIT_DENSITY_FLOOR:
        return None

    return rec(
        "P-004",
        "Publish concrete, quotable fact statements",
        f"The sampled prose ({total_chars} characters) contains {total_digits} "
        "digits — essentially no dates, counts, sizes or measurable claims. Assistants quote "
        "self-contained factual lines; abstract positioning copy gives them nothing to lift. "
        "A single line such as \"Founded 2021; 40 engineers; serving 12 countries\" is more "
        "citable than a page of capability language.",
        "medium", "low", "high", "text-statistical",
        {"field": "prose digit density", "condition": f"< {DIGIT_DENSITY_FLOOR}"},
    )


def rule_freshness(ctx):
    """No date anywhere and no page that would naturally carry one."""
    if not ctx["schema_reliable"]:
        return None
    for node in ctx["nodes"]:
        if set(node) & FRESHNESS_KEYS:
            return None
    if has_page_family(ctx, "blog"):
        return None
    if is_suppressed(ctx, ["FRESH-STALE", "FRESH-NODATE"], ("stale",)):
        return None
    return rec(
        "P-009",
        "Add a changelog or insights section that updates on a real cadence",
        "No dateModified or datePublished appears in any structured data block, and no blog, "
        "news or changelog section exists among the pages found. Nothing signals that the site "
        "is actively maintained. A genuinely-updated page is the honest way to create that "
        "signal — padding dateModified on static pages is detectable and counterproductive.",
        "low", "medium", "high", "path-absence",
        {"field": "freshness signals", "condition": "absent site-wide"},
    )


def rule_case_studies(ctx):
    if has_page_family(ctx, "case_studies"):
        return None
    if is_suppressed(ctx, ["CORROB-WEAK"], ("case stud",)):
        return None
    return rec(
        "P-012",
        "Publish case studies or a named-clients page",
        "No case-study, customer or portfolio page was found. Named engagements are the most "
        "reusable corroboration a B2B site can create: they give other sites something specific "
        "to reference, and they answer \"who actually uses this\" — a question assistants are "
        "asked constantly and currently cannot answer from this domain.",
        "low", "medium", "high", "path-absence",
        {"field": "page families", "condition": "no case-study path"},
    )


def rule_comparison(ctx):
    if has_page_family(ctx, "comparison"):
        return None
    return rec(
        "P-010",
        "Add a comparison or alternatives page",
        "No comparison, versus or alternatives page was found. Comparison queries are among the "
        "highest-volume prompts assistants receive. When a brand does not publish its own "
        "comparison, assistants fall back to third-party review sites and competitor pages — "
        "which get cited instead, and frame the comparison unfavourably.",
        "low", "high", "high", "path-absence",
        {"field": "page families", "condition": "no comparison path"},
    )


def rule_llms_txt(ctx):
    llms = ctx["llms_txt"]
    if not llms:
        return None  # crawler did not check — reported as a pending check instead
    if llms.get("found"):
        return None
    return rec(
        "P-007",
        "Publish an llms.txt orientation file",
        "No /llms.txt was found. The file is a short, machine-readable index pointing crawlers "
        "at the canonical pages for the brand rather than making them infer structure from "
        "navigation. Adoption is still emerging, but the cost is a single static file.",
        "low", "low", "high", "key-absence",
        {"field": "llms_txt.found", "condition": "false"},
    )


def rule_sitemap(ctx):
    sitemap = ctx["sitemap"]
    robots = ctx["evidence"].get("robots_txt", {}) or {}
    if not sitemap:
        return None  # crawler did not check
    if sitemap.get("found") and sitemap.get("url_count", 0) > 0:
        if robots.get("sitemap_urls"):
            return None
        return rec(
            "P-008",
            "Reference the sitemap from robots.txt",
            f"A sitemap with {sitemap.get('url_count')} URLs is reachable, but robots.txt does "
            "not contain a Sitemap: directive pointing at it. Crawlers that check robots.txt "
            "first have to guess the location.",
            "low", "low", "high", "key-absence",
            {"field": "robots_txt.sitemap_urls", "condition": "empty"},
        )
    return rec(
        "P-008",
        "Publish an XML sitemap and reference it from robots.txt",
        "No sitemap was found via robots.txt or at /sitemap.xml. Without one, a crawler can only "
        "reach pages that happen to be linked from pages it already has, so anything more than "
        "two clicks from the homepage may never be discovered.",
        "medium", "low", "high", "key-absence",
        {"field": "sitemap.found", "condition": "false"},
    )


def rule_faq(ctx):
    """
    Only fires when the answerability skill has supplied unanswered questions.
    Without that input we have no principled basis for recommending an FAQ, so
    we stay quiet rather than guessing.
    """
    ans = ctx["evidence"].get("answerability") or {}
    total = ans.get("questions_tested")
    unanswered = ans.get("unanswered")
    if not isinstance(total, int) or not isinstance(unanswered, int) or total == 0:
        return None
    if unanswered / total < 0.4:
        return None
    if "FAQPage" in ctx["types_present"]:
        return None
    return rec(
        "P-001",
        "Publish an FAQ page marked up with FAQPage schema",
        f"{unanswered} of {total} simulated user questions had no self-contained answer anywhere "
        "in the crawled text. An FAQ page gives an assistant a pre-formed question-and-answer "
        "pair it can lift directly, rather than requiring it to synthesise one from scattered "
        "prose.",
        "high", "medium", "high", "absence",
        {"field": "answerability.unanswered", "condition": ">= 40% of questions"},
    )


KEY_ABSENCE_RULES = [rule_sameas, rule_address_contact, rule_person_entity,
                     rule_llms_txt, rule_sitemap]
TYPE_ABSENCE_RULES = [rule_commercial_schema, rule_quotable_facts]
PATH_ABSENCE_RULES = [rule_freshness, rule_case_studies, rule_comparison, rule_faq]


# ---------------------------------------------------------------------------
# Pending checks: be explicit about what we could not evaluate
# ---------------------------------------------------------------------------

def pending_checks(ctx):
    out = []
    if not ctx["sitemap"]:
        out.append("sitemap — field not present in the evidence file; "
                   "absence-based recommendations fall back to homepage link counting")
    if not ctx["llms_txt"]:
        out.append("llms.txt — field not present in the evidence file; llms.txt rule skipped")
    if not ctx["evidence"].get("answerability"):
        out.append("FAQ recommendation — requires answerability-simulation output, not supplied")
    if not ctx["coverage"]:
        out.append("coverage — crawler did not report links_discovered; "
                   "crawl completeness had to be inferred")
    if ctx["parse_errors"]:
        out.append(f"structured-data rules — {ctx['parse_errors']} JSON-LD block(s) failed to "
                   "parse, so schema presence is unknown rather than absent; "
                   "schema-absence rules were suppressed")
    if ctx["org_node"] is None:
        out.append("entity rules — no Organization node found, so sameAs, address and "
                   "contactPoint rules could not be evaluated")
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def generate(evidence, findings=None):
    ctx = build_context(evidence, findings or [])
    coverage = assess_coverage(ctx)

    results = []

    # Key-absence rules ignore coverage entirely: we are inspecting an object
    # we already hold, and no amount of extra crawling changes its properties.
    for rule in KEY_ABSENCE_RULES:
        r = rule(ctx)
        if r:
            results.append(r)

    # Type-absence rules are weakened, not invalidated, by a thin sample.
    for rule in TYPE_ABSENCE_RULES:
        r = rule(ctx)
        if not r:
            continue
        if coverage["level"] in ("low", "unknown"):
            r["priority"] = "low"
            r["confidence"] = "low"
            r["rationale"] += f" Coverage caveat: {coverage['phrase']}."
        results.append(r)

    # Path-absence rules are pure claims about what does not exist. On a
    # truncated crawl they are unprovable, so we drop them rather than emit a
    # recommendation a reader could immediately falsify.
    for rule in PATH_ABSENCE_RULES:
        r = rule(ctx)
        if not r:
            continue
        if coverage["level"] in ("low", "unknown"):
            continue
        if coverage["level"] == "medium":
            r["priority"] = "low"
            r["confidence"] = "medium"
        r["rationale"] += f" Coverage: {coverage['phrase']}."
        results.append(r)

    results.sort(key=lambda r: (PRIORITY_ORDER.get(r["priority"], 9), r["id"]))

    skipped = [
        rule.__name__ for rule in PATH_ABSENCE_RULES
        if coverage["level"] in ("low", "unknown") and rule(ctx)
    ]

    return OrderedDict([
        ("site", evidence.get("site")),
        ("coverage", OrderedDict([
            ("level", coverage["level"]),
            ("pages_sampled", coverage["sampled"]),
            ("denominator", coverage["denominator"]),
            ("basis", coverage["basis"]),
            ("statement", coverage["phrase"]),
        ])),
        ("proactive_recommendations", results),
        ("suppressed_by_coverage", skipped),
        ("pending_checks", pending_checks(ctx)),
    ])


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("evidence", help="evidence JSON from crawl-render-audit")
    ap.add_argument("--findings", help="JSON array of findings already reported", default=None)
    ap.add_argument("--out", help="write here instead of stdout", default=None)
    args = ap.parse_args()

    evidence = load_json(args.evidence)
    findings = []
    if args.findings:
        loaded = load_json(args.findings)
        findings = loaded if isinstance(loaded, list) else loaded.get("findings", [])

    result = generate(evidence, findings)
    text = json.dumps(result, indent=2)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        print(text)


if __name__ == "__main__":
    main()