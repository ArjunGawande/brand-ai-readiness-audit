#!/usr/bin/env python3
"""
Retrievability checker for the AI-readiness audit.
Reads the evidence file produced by crawl-render-audit (the Playwright
render-gap version) and decides whether AI agents can actually reach and
read this site. Touches no network itself.

Usage: python check_retrievability.py evidence.json
"""

import datetime
import json
import sys
from collections import defaultdict

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Agents we probe at the server (matches crawler's PROBE_AGENTS)
PROBE_AGENTS = ["GPTBot", "PerplexityBot", "ClaudeBot"]

# Agents we read from robots.txt (matches crawler's ROBOTS_AGENTS), tagged
# by what blocking each one actually costs the brand.
#   "citation" = feeds live answers; blocking removes the brand from them
#   "training" = feeds model training; blocking can be a deliberate opt-out
AGENT_ROLES = {
    "GPTBot": "training",
    "OAI-SearchBot": "citation",
    "ChatGPT-User": "citation",
    "ClaudeBot": "training",
    "Claude-User": "citation",
    "PerplexityBot": "citation",
    "CCBot": "training",
    "Google-Extended": "training",
}

# Bot response must keep at least this share of the browser's text,
# otherwise we treat it as a stripped/challenge response.
TEXT_RATIO_FLOOR = 0.2

# text_delta_ratio from the crawler's render-gap check is now handled by
# the dedicated render-gap-audit skill.

# ---------------------------------------------------------------------------
# Finding helper
# ---------------------------------------------------------------------------

def finding(title: str, severity: str, evidence: str, action: str) -> dict:
    return {
        "id": None,
        "title": title,
        "severity": severity,
        "evidence": evidence,
        "suggested_action": {"summary": action, "priority": severity},
    }


# ---------------------------------------------------------------------------
# Check A (gate) — was the whole crawl blocked?
# ---------------------------------------------------------------------------

def check_full_block(data: dict) -> list:
    ua_by_agent = {r["agent"]: r for r in data.get("ua_probe", {}).get("results", [])}
    chrome = ua_by_agent.get("chrome_baseline", {})
    status = chrome.get("status_code")
    failure = chrome.get("failure_type")

    if status == 200 and failure is None:
        return []

    summary = data.get("crawl_summary", {})

    if failure:
        reason = {
            "timeout": "the server did not respond in time",
            "connection_error": "a connection could not be established (DNS, host, or port issue)",
            "redirect_loop": "the page never settled on a final URL (redirect loop)",
        }.get(failure, f"a low-level HTTP error occurred ({failure})")
        evidence = (
            f"Standard browser-UA fetch of the homepage failed: {reason} "
            f"(failure_type: {failure})."
        )
    else:
        evidence = (
            f"Homepage returned status {status} to a standard browser user-agent. "
            f"{summary.get('pages_succeeded', 0)} of {summary.get('pages_attempted', 0)} "
            f"sampled pages succeeded."
        )

    return [finding(
        "Site could not be crawled — audit coverage is incomplete",
        "critical",
        evidence,
        "Confirm the site is reachable and not behind a bot-protection layer that "
        "rejects non-browser clients generally (common with enterprise WAFs). If so, "
        "AI crawlers are blocked by the same mechanism regardless of robots.txt. "
        "Re-run the audit from an allowlisted source for full coverage.",
    )]


# ---------------------------------------------------------------------------
# Check B — robots.txt policy
# ---------------------------------------------------------------------------

def check_robots(data: dict) -> list:
    robots = data.get("robots_txt", {})
    results = []

    if not robots.get("found"):
        return [finding(
            "No robots.txt found",
            "medium",
            "robots_txt.found is false — no /robots.txt served. Crawlers fall back "
            "to default behaviour, and the site has no stated policy on AI access.",
            "Add a robots.txt that explicitly states which crawlers may access the "
            "site. Silence is ambiguous; an explicit allow is not.",
        )]

    rules = robots.get("rules_by_agent", {})
    for agent, role in AGENT_ROLES.items():
        if rules.get(agent) != "blocked":
            continue
        if role == "citation":
            results.append(finding(
                f"{agent} disallowed in robots.txt",
                "critical",
                f"rules_by_agent.{agent} is 'blocked'. {agent} is a citation-facing "
                f"crawler — blocking it means this brand cannot appear as a cited "
                f"source in that assistant's answers.",
                f"Remove the {agent} disallow rule, or narrow it to genuinely private "
                f"paths. Blocking a citation crawler removes the brand from answers "
                f"entirely, which is rarely the intended outcome.",
            ))
        else:
            results.append(finding(
                f"{agent} disallowed in robots.txt",
                "critical",
                f"rules_by_agent.{agent} is 'blocked'. {agent} is a training crawler "
                f"— content here will not enter that model's training data.",
                f"If blocking {agent} was a deliberate opt-out of model training, no "
                f"change is needed — but confirm the same rule isn't also blocking a "
                f"citation-facing crawler, which is a different and usually "
                f"unintended trade-off.",
            ))

    return results


# ---------------------------------------------------------------------------
# Check C — server-level block (UA probe differential)
# ---------------------------------------------------------------------------

def check_ua_probe(data: dict) -> list:
    ua_by_agent = {r["agent"]: r for r in data.get("ua_probe", {}).get("results", [])}
    chrome = ua_by_agent.get("chrome_baseline")
    if not chrome or chrome.get("status_code") != 200:
        return []  # handled by check_full_block

    chrome_text = chrome.get("text_length", 0)
    rules = data.get("robots_txt", {}).get("rules_by_agent", {})
    results = []

    for agent in PROBE_AGENTS:
        r = ua_by_agent.get(agent)
        if not r:
            continue

        status = r.get("status_code")
        failure = r.get("failure_type")
        text_len = r.get("text_length", 0)
        robots_says = rules.get(agent)

        contradiction = ""
        if robots_says == "allowed":
            contradiction = (
                f" robots.txt allows {agent}, so this is likely an unintended "
                f"infrastructure block rather than a deliberate policy decision."
            )

        if failure:
            results.append(finding(
                f"{agent} fetch failed at the server",
                "critical",
                f"Requesting the homepage as {agent} resulted in {failure}, while "
                f"the same request as a browser succeeded (200, {chrome_text} chars).",
                f"Investigate why {agent} specifically cannot connect — check "
                f"WAF/CDN rules and any per-user-agent rate limiting.",
            ))
            continue

        if status != 200:
            results.append(finding(
                f"Server returns {status} to {agent} while browsers get 200",
                "critical",
                f"Homepage returned 200 to a browser ({chrome_text} chars) but "
                f"{status} to {agent}.{contradiction}",
                "Check WAF/CDN bot-management rules (e.g. a 'block AI bots' toggle) "
                "and allowlist this crawler if the brand wants it citing them.",
            ))
        elif chrome_text > 0 and text_len < chrome_text * TEXT_RATIO_FLOOR:
            pct = round(text_len / chrome_text * 100)
            results.append(finding(
                f"Server serves substantially less content to {agent}",
                "critical",
                f"Both got status 200, but the browser got {chrome_text} chars "
                f"while {agent} got {text_len} ({pct}%).{contradiction} A matching "
                f"status with this little content usually indicates a challenge or "
                f"stripped-down response.",
                "Inspect what the server actually returns to this user-agent — a "
                "200 carrying a challenge page looks healthy in logs but is "
                "invisible to the assistant.",
            ))

    return results




# ---------------------------------------------------------------------------
# Check E — broken internal links (uses per-page failure_type)
# ---------------------------------------------------------------------------

def check_broken_links(data: dict) -> list:
    pages = data.get("pages_checked", [])
    broken = [p for p in pages if p.get("failure_type")]
    if not broken:
        return []

    by_type = defaultdict(list)
    for p in broken:
        by_type[p["failure_type"]].append(p)
    breakdown = ", ".join(f"{len(v)} {k}" for k, v in by_type.items())
    example = broken[0]

    return [finding(
        "Some internal links sampled from the homepage failed to load",
        "medium",
        f"{len(broken)} of {len(pages)} sampled pages failed to fetch ({breakdown}). "
        f"Example: {example.get('requested_url')} ({example.get('failure_type')}).",
        "Fix or remove broken internal links. A page a crawler cannot reach cannot "
        "be read or cited, regardless of its own content quality.",
    )]


# ---------------------------------------------------------------------------
# Check F — explicit noindex directive
# ---------------------------------------------------------------------------

def check_noindex(data: dict) -> list:
    pages = data.get("pages_checked", [])
    flagged = [p for p in pages if p.get("meta_robots") and "noindex" in p["meta_robots"].lower()]
    if not flagged:
        return []

    urls = ", ".join(p["requested_url"] for p in flagged[:5])
    return [finding(
        f"{len(flagged)} sampled page(s) explicitly opt out of indexing",
        "high",
        f"meta_robots contains 'noindex' on: {urls}.",
        "Remove the noindex directive from any page that should be discoverable, "
        "unless it was set deliberately (e.g. thank-you pages, internal tools).",
    )]


# ---------------------------------------------------------------------------
# Assemble
# ---------------------------------------------------------------------------

def analyze(data: dict) -> dict:
    coverage_notes = []

    blocked = check_full_block(data)
    if blocked:
        # A total block short-circuits page-level checks — one clear finding
        # beats a pile of granular ones built on no data.
        findings = blocked + check_robots(data)
    else:
        findings = (
            check_robots(data)
            + check_ua_probe(data)
            + check_broken_links(data)
            + check_noindex(data)
        )

    for i, f in enumerate(findings, start=1):
        f["id"] = f"F-{i:03d}"

    counts = {"critical": 0, "high": 0, "medium": 0}
    for f in findings:
        if f["severity"] in counts:
            counts[f["severity"]] += 1

    result = {
        "site": data.get("site", ""),
        "audited_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "agent": "retrievability-checker",
        "summary": {"total_findings": len(findings), **counts},
        "findings": findings,
    }

    if coverage_notes:
        result["coverage_notes"] = coverage_notes
    if not findings:
        result["notes"] = "No retrievability problems detected in the checked evidence."

    return result


def main():
    if len(sys.argv) < 2:
        print("Usage: python check_retrievability.py evidence.json")
        sys.exit(1)

    try:
        with open(sys.argv[1], encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        print(f"Error: no such file: {sys.argv[1]}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Error: {sys.argv[1]} is not valid JSON ({e})")
        sys.exit(1)

    print(json.dumps(analyze(data), indent=2))


if __name__ == "__main__":
    main()