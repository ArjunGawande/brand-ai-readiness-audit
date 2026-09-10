#!/usr/bin/env python3
"""
Simple site crawler for the AI-readiness audit.
Checks robots.txt rules, probes multiple AI bot UAs vs a browser,
detects render gaps (raw HTML vs JS-rendered DOM), samples pages,
and prints one JSON evidence file. Makes no judgments —
that's the analyzer skill's job.

Usage: python crawl.py https://example.com
"""

import asyncio
import datetime
import json
import re
import sys
from collections import defaultdict
from urllib.parse import urljoin, urlparse

import httpx
from selectolax.parser import HTMLParser

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0"
OUR_UA = "BrandAuditBot/1.0 (+https://example.org/audit; read-only)"

# Feature 2: Full list of agents we probe at the door (was only GPTBot before)
PROBE_AGENTS = ["GPTBot", "PerplexityBot", "ClaudeBot"]

# Agents we check in robots.txt (broader list — includes citation bots)
ROBOTS_AGENTS = [
    "GPTBot", "OAI-SearchBot", "ChatGPT-User",
    "ClaudeBot", "Claude-User",
    "PerplexityBot", "CCBot", "Google-Extended",
]

MAX_PAGES = 12
TIMEOUT = 10.0
DELAY_SECONDS = 0.3  # politeness delay between page fetches

# Feature 1: Render-gap config
RENDER_SAMPLE_LIMIT = 5          # max pages to render with Playwright
RENDER_WAIT_MS = 3000            # wait for JS to settle after load
THIN_TEXT_THRESHOLD = 500        # raw text below this → worth rendering

# ---------------------------------------------------------------------------
# Feature 5: Smarter fetch — categorizes failures instead of returning None
# ---------------------------------------------------------------------------
# OLD: any exception → return None → page silently dropped
# NEW: every fetch returns a dict with status, body, and failure_type
#      so the analyzer can distinguish "timed out" from "403 blocked"
#      from "DNS failed" — each means something different

async def fetch(client: httpx.AsyncClient, url: str, ua: str) -> dict:
    """
    Always returns a dict, never None.
    - On success: {"ok": True, "status_code": 200, "body": "...", ...}
    - On failure: {"ok": False, "failure_type": "timeout", ...}
    """
    try:
        resp = await client.get(
            url,
            headers={"User-Agent": ua},
            timeout=TIMEOUT,
            follow_redirects=True,
        )
        return {
            "ok": True,
            "status_code": resp.status_code,
            "body": resp.text,
            "content_length": len(resp.content),      # page size in bytes
            "final_url": str(resp.url),                # where we ended up after redirects
            "redirect_count": len(resp.history),       # how many hops
            "failure_type": None,
        }
    except httpx.TimeoutException:
        # Server didn't respond in time — could be overloaded or slow
        return {"ok": False, "failure_type": "timeout", "status_code": None, "body": "", "content_length": 0, "final_url": None, "redirect_count": 0}
    except httpx.ConnectError:
        # Couldn't even open a TCP connection — DNS failure, host down, port closed
        return {"ok": False, "failure_type": "connection_error", "status_code": None, "body": "", "content_length": 0, "final_url": None, "redirect_count": 0}
    except httpx.TooManyRedirects:
        # Redirect loop — the page never settles on a final URL
        return {"ok": False, "failure_type": "redirect_loop", "status_code": None, "body": "", "content_length": 0, "final_url": None, "redirect_count": 0}
    except httpx.HTTPError as e:
        # Catch-all for anything else (protocol errors, etc.)
        return {"ok": False, "failure_type": f"http_error:{type(e).__name__}", "status_code": None, "body": "", "content_length": 0, "final_url": None, "redirect_count": 0}


# ---------------------------------------------------------------------------
# HTML analysis
# ---------------------------------------------------------------------------

def analyze_html(html: str) -> dict:
    """Pull out the facts each finding needs from raw HTML."""
    tree = HTMLParser(html)

    # Count script bytes before stripping
    script_bytes = sum(len(s.text() or "") for s in tree.css("script"))

    # Extract JSON-LD blocks (the raw text, not just a boolean)
    jsonld_blocks = []
    for tag in tree.css('script[type="application/ld+json"]'):
        text = tag.text(strip=True)
        if text:
            jsonld_blocks.append(text)

    # Feature 7: Check if an <h1> tag exists in raw HTML
    # WHY: h1 is the primary heading — it tells a crawler "this is the main
    #       topic of the page" in one line. A page without an h1 forces the
    #       machine to guess what the page is about from surrounding text.
    #       Missing h1 is cheap to detect, easy to fix, and directly hurts
    #       whether an AI can extract a clear primary fact.
    h1_tag = tree.css_first("h1")
    h1_present = h1_tag is not None
    h1_text = h1_tag.text(strip=True) if h1_tag else None

    # Meta robots tag (noindex detection — added as a cheap bonus)
    meta_robots = None
    meta_tag = tree.css_first('meta[name="robots"]')
    if meta_tag:
        meta_robots = meta_tag.attributes.get("content", "")

    # Canonical URL
    canonical = None
    canon_tag = tree.css_first('link[rel="canonical"]')
    if canon_tag:
        canonical = canon_tag.attributes.get("href", "")

    # Strip scripts/styles before measuring visible text
    for tag in tree.css("script, style"):
        tag.decompose()
    visible_text = tree.body.text(separator=" ", strip=True) if tree.body else ""

    title = tree.css_first("title")

    return {
        "visible_text_length": len(visible_text),
        "visible_text_snippet": visible_text[:200],   # first 200 chars for quick inspection
        "script_size": script_bytes,
        "title": title.text(strip=True) if title else "",
        "h1_present": h1_present,                      # Feature 7
        "h1_text": h1_text,                            # Feature 7: the actual heading
        "has_structured_data": len(jsonld_blocks) > 0,
        "jsonld_block_count": len(jsonld_blocks),
        "jsonld_raw": jsonld_blocks,                   # full blocks for the analyzer
        "meta_robots": meta_robots,
        "canonical_url": canonical,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_robots(text: str, agents: list) -> dict:
    """Tiny robots.txt reader: 'allowed'/'blocked' per agent."""
    rules = defaultdict(list)
    current = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, _, value = line.partition(":")
        field, value = field.strip().lower(), value.strip()
        if field == "user-agent":
            current = [value]
        elif field == "disallow" and value:
            for agent in current:
                rules[agent].append(value)

    result = {}
    for agent in agents:
        blocked = rules.get(agent) or rules.get("*", [])
        result[agent] = "blocked" if "/" in blocked else "allowed"
    return result


def page_type(url: str) -> str:
    path = urlparse(url).path.strip("/")
    return "homepage" if not path else "/" + path.split("/")[0] + "/"


def internal_links(base_url: str, html: str) -> list:
    tree = HTMLParser(html)
    seen, links = set(), []
    for a in tree.css("a[href]"):
        full = urljoin(base_url, a.attributes.get("href") or "")
        full = full.split("#")[0]
        if urlparse(full).netloc == urlparse(base_url).netloc and full not in seen:
            seen.add(full)
            links.append(full)
    return links


# ---------------------------------------------------------------------------
# Feature 1: Render-gap check using Playwright
# ---------------------------------------------------------------------------
# WHY THIS EXISTS:
# Many modern sites (React SPAs, Next.js client-rendered, Vue apps) serve
# a near-empty HTML shell to the browser. The real content only appears
# after JavaScript runs. A simple HTTP GET (which is what most AI crawlers
# do) sees the empty shell — so the product name, price, description are
# all invisible to the AI even though they look fine to a human.
#
# HOW IT WORKS:
# 1. We already have the raw HTML from httpx (the "before JS" version)
# 2. We launch a headless Chromium via Playwright, load the same URL,
#    wait for JS to finish, then grab the DOM (the "after JS" version)
# 3. We compare: text length, title, h1, JSON-LD block count
# 4. The ratio (rendered - raw) / rendered = text_delta_ratio
#    - Near 0.0 → server-rendered, safe
#    - Above 0.7 → most content is JS-only, critical problem
#
# WHY SAMPLING:
# Playwright launches a real browser — each page takes 2-4 seconds.
# With a 5-minute audit budget and 12+ pages, we can't render everything.
# But rendering strategy is a SITE-LEVEL property (all product pages use
# the same template), so we pick one page per template type and generalize.
#
# GRACEFUL DEGRADATION:
# If Playwright isn't installed, we catch the ImportError, set
# render_check.status = "unavailable", and the rest of the audit
# continues without it — the skill never crashes.

async def render_page(browser, url: str) -> dict | None:
    """Load one URL in headless Chromium and extract the rendered DOM."""
    try:
        page = await browser.new_page()
        await page.goto(url, wait_until="networkidle", timeout=15000)
        # Extra wait for late-loading JS frameworks
        await page.wait_for_timeout(RENDER_WAIT_MS)

        # Get the fully rendered HTML
        rendered_html = await page.content()
        await page.close()

        # Analyze the rendered version the same way we analyze raw HTML
        info = analyze_html(rendered_html)
        return info
    except Exception as e:
        return None


def pick_render_targets(pages_checked: list) -> list:
    """
    Choose which pages to render with Playwright.
    Strategy: one page per unique page_type, prioritizing pages with
    suspiciously thin raw text (< THIN_TEXT_THRESHOLD chars), capped
    at RENDER_SAMPLE_LIMIT.
    """
    # Group pages by type
    by_type = defaultdict(list)
    for p in pages_checked:
        if p.get("status_code") == 200:
            by_type[p["type"]].append(p)

    targets = []
    # First pass: pick thin pages (most likely to have a render gap)
    for ptype, pages in by_type.items():
        thin = [p for p in pages if p["visible_text_length"] < THIN_TEXT_THRESHOLD]
        if thin:
            targets.append(thin[0])
        elif pages:
            targets.append(pages[0])

    # Cap at limit
    return targets[:RENDER_SAMPLE_LIMIT]


async def render_gap_check(pages_checked: list) -> dict:
    """
    Run Playwright on a sample of pages and compare raw vs rendered.
    Returns the full render_check section of the evidence file.
    """
    # Graceful degradation: if Playwright is not installed, say so and move on
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {
            "status": "unavailable",
            "reason": "playwright not installed — run: pip install playwright && playwright install chromium",
            "pages": [],
        }

    targets = pick_render_targets(pages_checked)
    if not targets:
        return {"status": "skipped", "reason": "no eligible pages to render", "pages": []}

    results = []

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)

            for page_data in targets:
                url = page_data["requested_url"]
                rendered = await render_page(browser, url)

                if rendered is None:
                    results.append({
                        "url": url,
                        "page_type": page_data["type"],
                        "render_failed": True,
                    })
                    continue

                raw_len = page_data["visible_text_length"]
                rendered_len = rendered["visible_text_length"]

                # text_delta_ratio: what fraction of the rendered text is MISSING from the raw HTML
                # Formula: (rendered - raw) / rendered
                # 0.0 = everything was already in raw HTML (good)
                # 0.93 = 93% of content only appears after JS (bad)
                if rendered_len > 0:
                    delta = round((rendered_len - raw_len) / rendered_len, 2)
                else:
                    delta = 0.0

                results.append({
                    "url": url,
                    "page_type": page_data["type"],
                    "raw_text_len": raw_len,
                    "rendered_text_len": rendered_len,
                    "text_delta_ratio": max(delta, 0.0),  # clamp: raw can be bigger due to noscript fallback
                    "raw_title": page_data["title"],
                    "rendered_title": rendered["title"],
                    "h1_present_raw": page_data["h1_present"],
                    "h1_present_rendered": rendered["h1_present"],
                    "jsonld_blocks_raw": page_data["jsonld_block_count"],
                    "jsonld_blocks_rendered": rendered["jsonld_block_count"],
                    "main_content_source": "client_fetch" if delta > 0.5 else "server",
                    "noscript_fallback_len": 0,  # TODO: extract <noscript> content length
                })

            await browser.close()
    except Exception as e:
        return {"status": "error", "reason": str(e), "pages": results}

    # Check if pages of the same type disagree on delta (means inconsistent rendering)
    type_deltas = defaultdict(list)
    for r in results:
        if "text_delta_ratio" in r:
            type_deltas[r["page_type"]].append(r["text_delta_ratio"])
    inconsistent = any(
        max(deltas) - min(deltas) > 0.3
        for deltas in type_deltas.values()
        if len(deltas) > 1
    )

    return {
        "status": "completed",
        "render_sampled": True,
        "pages_rendered": [r["url"] for r in results if not r.get("render_failed")],
        "pages": results,
        "template_inconsistent": inconsistent,
    }


# ---------------------------------------------------------------------------
# Main crawl
# ---------------------------------------------------------------------------

async def crawl(site_url: str) -> dict:
    async with httpx.AsyncClient() as client:

        # ---- 1. robots.txt ----
        robots_result = await fetch(client, urljoin(site_url, "/robots.txt"), OUR_UA)
        robots_text = robots_result["body"] if robots_result["ok"] and robots_result["status_code"] == 200 else ""
        rules = parse_robots(robots_text, ROBOTS_AGENTS) if robots_text else {}

        # ---- 2. UA probe on homepage — Feature 2: now 4 identities, not 2 ----
        # OLD CODE: only compared Chrome vs GPTBot
        # NEW CODE: we probe Chrome + every agent in PROBE_AGENTS
        #
        # WHY: A WAF (web application firewall) might allow GPTBot but block
        #       PerplexityBot, or vice versa. Testing only one bot and assuming
        #       the others are treated the same is a false-negative factory.
        #       Each probe costs one HTTP request — cheap.

        ua_results = []

        # First: the Chrome baseline (what a human sees)
        chrome = await fetch(client, site_url, BROWSER_UA)
        chrome_info = analyze_html(chrome["body"]) if chrome["ok"] and chrome["status_code"] == 200 else None
        ua_results.append({
            "agent": "chrome_baseline",
            "status_code": chrome["status_code"],
            "page_size_bytes": chrome["content_length"],
            "text_length": chrome_info["visible_text_length"] if chrome_info else 0,
            "final_url": chrome["final_url"],
            "failure_type": chrome["failure_type"],
        })

        # Then: each AI bot
        for agent_name in PROBE_AGENTS:
            resp = await fetch(client, site_url, agent_name)
            info = analyze_html(resp["body"]) if resp["ok"] and resp["status_code"] == 200 else None
            ua_results.append({
                "agent": agent_name,
                "status_code": resp["status_code"],
                "page_size_bytes": resp["content_length"],
                "text_length": info["visible_text_length"] if info else 0,
                "final_url": resp["final_url"],
                "failure_type": resp["failure_type"],
            })

        # ---- 3. Sample pages from homepage links ----
        pages_checked = []
        if chrome["ok"] and chrome["status_code"] == 200:
            for url in internal_links(site_url, chrome["body"])[:MAX_PAGES]:
                resp = await fetch(client, url, OUR_UA)
                await asyncio.sleep(DELAY_SECONDS)

                if resp["ok"] and resp["status_code"] == 200:
                    info = analyze_html(resp["body"])
                else:
                    info = {
                        "visible_text_length": 0, "script_size": 0, "title": "",
                        "h1_present": False, "h1_text": None,
                        "has_structured_data": False, "jsonld_block_count": 0, "jsonld_raw": [],
                        "meta_robots": None, "canonical_url": None, "visible_text_snippet": "",
                    }

                pages_checked.append({
                    "requested_url": url,
                    "final_url": resp["final_url"],
                    "redirect_count": resp["redirect_count"],
                    "status_code": resp["status_code"],
                    "failure_type": resp["failure_type"],       # Feature 5
                    "type": page_type(url),
                    "visible_text_length": info["visible_text_length"],
                    "script_size": info["script_size"],
                    "page_size_bytes": resp["content_length"],
                    "title": info["title"],
                    "h1_present": info["h1_present"],           # Feature 7
                    "h1_text": info["h1_text"],                 # Feature 7
                    "has_structured_data": info["has_structured_data"],
                    "jsonld_block_count": info["jsonld_block_count"],
                    "jsonld_raw": info["jsonld_raw"],
                    "meta_robots": info["meta_robots"],
                    "canonical_url": info["canonical_url"],
                })

        # ---- 4. Render-gap check — Feature 1 ----
        render_result = await render_gap_check(pages_checked)

        # ---- 5. Assemble evidence file ----
        succeeded = sum(1 for p in pages_checked if p["status_code"] == 200)
        failed = len(pages_checked) - succeeded

        return {
            "site": site_url,
            "crawled_at": datetime.datetime.now(datetime.UTC).isoformat() + "Z",
            "our_agent": OUR_UA,

            "robots_txt": {
                "found": bool(robots_text),
                "rules_by_agent": rules,
            },

            "ua_probe": {
                "url_tested": site_url,
                "results": ua_results,
            },

            "pages_checked": pages_checked,

            "render_check": render_result,

            "crawl_summary": {
                "pages_attempted": len(pages_checked),
                "pages_succeeded": succeeded,
                "pages_failed": failed,
                "pages_rendered": len(render_result.get("pages", [])),
            },
        }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print("Usage: python crawl.py https://example.com")
        sys.exit(1)
    result = asyncio.run(crawl(sys.argv[1]))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()