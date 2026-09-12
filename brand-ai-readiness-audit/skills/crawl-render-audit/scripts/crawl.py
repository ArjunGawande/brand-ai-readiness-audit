#!/usr/bin/env python3
"""
Simple site crawler for the AI-readiness audit.
Checks robots.txt rules, probes multiple AI bot UAs vs a browser,
detects render gaps (raw HTML vs JS-rendered DOM), discovers the site's own
URL inventory via sitemap, samples pages, and prints one JSON evidence file.
Makes no judgments — that's the analyzer skill's job.

Usage: python crawl.py https://example.com
"""

import asyncio
import datetime
import json
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

# Feature 8: Sitemap config
SITEMAP_URL_CAP = 1000           # keep the evidence file a sane size
SITEMAP_INDEX_CAP = 5            # how many child sitemaps to expand

# Feature 10: how much visible text to keep per page.
# WAS 200 — too short for any downstream statistic to mean anything.
SNIPPET_CHARS = 600


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
# Feature 9: text_stats — full-text numbers, computed BEFORE truncation
# ---------------------------------------------------------------------------
# WHY THIS EXISTS:
# We keep only a snippet of each page's text, because storing full page text
# would bloat the evidence file. But downstream skills want to ask things like
# "how concrete is this site's prose?" — and computing that from a truncated
# snippet is measuring noise, not the page.
#
# Computing the statistics here, while we still hold the full text, costs
# nothing and hands downstream skills a stable number instead of a guess.
#
# digit_density is the useful one: prose with no digits has no dates, counts,
# prices or measurable claims — nothing an AI can lift and quote as a fact.

def text_stats(visible_text: str) -> dict:
    """Compute statistics over the FULL visible text, before truncation."""
    if not visible_text:
        return {
            "char_count": 0, "digit_count": 0, "digit_density": 0.0,
            "sentence_count": 0, "longest_paragraph": 0,
        }

    digits = sum(1 for c in visible_text if c.isdigit())
    sentences = (visible_text.count(".") + visible_text.count("!")
                 + visible_text.count("?"))
    # selectolax joins block elements with the separator, so a double space is
    # a rough proxy for a paragraph boundary.
    paragraphs = [seg.strip() for seg in visible_text.split("  ") if seg.strip()]

    return {
        "char_count": len(visible_text),
        "digit_count": digits,
        "digit_density": round(digits / len(visible_text), 5),
        "sentence_count": sentences,
        "longest_paragraph": max((len(p) for p in paragraphs), default=0),
    }


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
        "visible_text_snippet": visible_text[:SNIPPET_CHARS],   # Feature 10
        "text_stats": text_stats(visible_text),                 # Feature 9
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


def empty_page_info() -> dict:
    """
    What we record for a page we could not read. Kept as a function so it can
    never be mutated by accident, and so its keys stay in lockstep with
    analyze_html — a missing key here is a KeyError 200 lines later.
    """
    return {
        "visible_text_length": 0,
        "visible_text_snippet": "",
        "text_stats": text_stats(""),
        "script_size": 0,
        "title": "",
        "h1_present": False,
        "h1_text": None,
        "has_structured_data": False,
        "jsonld_block_count": 0,
        "jsonld_raw": [],
        "meta_robots": None,
        "canonical_url": None,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Feature 8a: robots.txt now also yields the Sitemap: directive.
# WHY: the old parser read user-agent and disallow and silently dropped every
#      other field. Sitemap: was one of the dropped ones — and it is the most
#      valuable line in the file for us, because it hands over the site's own
#      list of every page it wants crawled. That list is what turns
#      "I didn't find a pricing page" into "the sitemap lists 34 URLs and none
#      of them is pricing" — a provable claim instead of a guess.

def parse_robots(text: str, agents: list) -> tuple:
    """
    Tiny robots.txt reader.
    Returns (rules_by_agent, sitemap_urls).
    """
    rules = defaultdict(list)
    sitemap_urls = []
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
        elif field == "sitemap" and value:
            # Sitemap directives are global — they are NOT scoped to the
            # preceding user-agent block, so collect them unconditionally.
            sitemap_urls.append(value)

    result = {}
    for agent in agents:
        blocked = rules.get(agent) or rules.get("*", [])
        result[agent] = "blocked" if "/" in blocked else "allowed"

    return result, sitemap_urls


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
# Feature 8b: Sitemap discovery
# ---------------------------------------------------------------------------
# WHY THIS EXISTS:
# We sample at most MAX_PAGES pages. On a 400-page site that is 3% of it.
# Any downstream claim of the form "the site has no X page" is then a guess —
# and a guess anyone can disprove in one click.
#
# A sitemap fixes this without costing crawl budget: we do not FETCH the listed
# pages, we only read their URLs. Knowing a page exists is enough to stop us
# claiming it does not.
#
# SITEMAP INDEXES:
# Large sites publish a sitemap whose entries point at OTHER sitemaps rather
# than at pages. Treating that index as a page list would report "4 URLs" for
# a site with 40,000. We detect the <sitemapindex> wrapper and expand a
# bounded number of children.

def _extract_locs(xml_text: str) -> list:
    """Pull <loc> values out of sitemap XML."""
    tree = HTMLParser(xml_text)
    return [node.text(strip=True) for node in tree.css("loc") if node.text(strip=True)]


async def fetch_sitemap(client, site_url: str, from_robots: list) -> dict:
    """
    Try every sitemap named in robots.txt, then /sitemap.xml as a fallback.
    Returns a dict describing what we found — never raises.
    """
    candidates = list(from_robots) + [urljoin(site_url, "/sitemap.xml")]

    for candidate in candidates:
        resp = await fetch(client, candidate, OUR_UA)
        if not resp["ok"] or resp["status_code"] != 200:
            continue

        body = resp["body"]
        if "<loc" not in body.lower():
            continue

        locs = _extract_locs(body)
        if not locs:
            continue

        # Detect an index by its wrapper tag, not by guessing from .xml
        # suffixes — some sites legitimately serve pages at .xml paths.
        if "<sitemapindex" in body.lower():
            page_urls = []
            for child in locs[:SITEMAP_INDEX_CAP]:
                child_resp = await fetch(client, child, OUR_UA)
                if child_resp["ok"] and child_resp["status_code"] == 200:
                    page_urls.extend(_extract_locs(child_resp["body"]))
            return {
                "found": True,
                "source": candidate,
                "is_index": True,
                "child_sitemaps": len(locs),
                "child_sitemaps_expanded": min(len(locs), SITEMAP_INDEX_CAP),
                "url_count": len(page_urls),
                "urls": page_urls[:SITEMAP_URL_CAP],
                "truncated": len(page_urls) > SITEMAP_URL_CAP or len(locs) > SITEMAP_INDEX_CAP,
            }

        return {
            "found": True,
            "source": candidate,
            "is_index": False,
            "url_count": len(locs),
            "urls": locs[:SITEMAP_URL_CAP],
            "truncated": len(locs) > SITEMAP_URL_CAP,
        }

    return {
        "found": False, "source": None, "is_index": False,
        "url_count": 0, "urls": [], "truncated": False,
    }


# ---------------------------------------------------------------------------
# Feature 8c: llms.txt
# ---------------------------------------------------------------------------
# A short machine-readable index pointing AI crawlers at a site's canonical
# content instead of making them infer structure from navigation. One request;
# we record presence and size, and leave judgment to the analyzer.

async def fetch_llms_txt(client, site_url: str) -> dict:
    url = urljoin(site_url, "/llms.txt")
    resp = await fetch(client, url, OUR_UA)
    if resp["ok"] and resp["status_code"] == 200 and resp["body"].strip():
        body = resp["body"]
        # Guard against SPA catch-all routing: a site that serves index.html
        # for every unknown path would otherwise register a phantom llms.txt.
        if "<html" in body[:500].lower():
            return {"found": False, "url": url, "byte_length": 0,
                    "link_count": 0, "note": "served HTML, not a text file"}
        return {
            "found": True,
            "url": url,
            "byte_length": len(body.encode("utf-8")),
            "link_count": body.count("]("),   # markdown links, the usual format
        }
    return {"found": False, "url": url, "byte_length": 0, "link_count": 0}


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
    except Exception:
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
                    # Keep the SIGNED value too. A consistently negative delta
                    # means an element is removed on hydration — real signal
                    # that the clamp above would otherwise hide completely.
                    "text_delta_signed": delta,
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
    site_url = site_url.strip()
    if not site_url.startswith(("http://", "https://")):
        site_url = f"https://{site_url}"

    async with httpx.AsyncClient() as client:

        # ---- 1. robots.txt ----
        robots_result = await fetch(client, urljoin(site_url, "/robots.txt"), OUR_UA)
        robots_text = robots_result["body"] if robots_result["ok"] and robots_result["status_code"] == 200 else ""
        # Feature 8a: now returns a tuple — rules AND any Sitemap: directives
        if robots_text:
            rules, robots_sitemaps = parse_robots(robots_text, ROBOTS_AGENTS)
        else:
            rules, robots_sitemaps = {}, []

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

        # ---- 3. Sitemap + llms.txt — Feature 8 ----
        # Done before page sampling, so the sitemap can widen what we sample.
        sitemap_data = await fetch_sitemap(client, site_url, robots_sitemaps)
        llms_data = await fetch_llms_txt(client, site_url)

        # ---- 4. Sample pages ----
        # Feature 8d: capture the DENOMINATOR, not just the numerator.
        # "3 pages checked" cannot distinguish a 3-page site from a truncated
        # crawl of a 300-page one, and without that distinction every
        # downstream "the site has no X page" claim is unfounded.
        pages_checked = []
        links_discovered = 0
        candidate_urls = []

        if chrome["ok"] and chrome["status_code"] == 200:
            homepage_links = internal_links(site_url, chrome["body"])
            links_discovered = len(homepage_links)
            candidate_urls = list(homepage_links)

            # If the sitemap knows about pages the homepage does not link to,
            # add them to the sample. A page two clicks deep is exactly what an
            # AI crawler misses — and exactly what we want to have looked at.
            if sitemap_data["found"]:
                seen = set(candidate_urls)
                for url in sitemap_data["urls"]:
                    if urlparse(url).netloc == urlparse(site_url).netloc and url not in seen:
                        seen.add(url)
                        candidate_urls.append(url)

        for url in candidate_urls[:MAX_PAGES]:
            resp = await fetch(client, url, OUR_UA)
            await asyncio.sleep(DELAY_SECONDS)

            if resp["ok"] and resp["status_code"] == 200:
                info = analyze_html(resp["body"])
            else:
                info = empty_page_info()

            pages_checked.append({
                "requested_url": url,
                "final_url": resp["final_url"],
                "redirect_count": resp["redirect_count"],
                "status_code": resp["status_code"],
                "failure_type": resp["failure_type"],       # Feature 5
                "type": page_type(url),
                "visible_text_length": info["visible_text_length"],
                "visible_text_snippet": info["visible_text_snippet"],
                "text_stats": info["text_stats"],           # Feature 9
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

        # ---- 5. Render-gap check — Feature 1 ----
        render_result = await render_gap_check(pages_checked)

        # ---- 6. Assemble evidence file ----
        succeeded = sum(1 for p in pages_checked if p["status_code"] == 200)
        failed = len(pages_checked) - succeeded

        # How much of the site did we actually see? The analyzer reads this to
        # decide whether "no X page exists" is provable or a guess.
        urls_known = max(links_discovered, sitemap_data["url_count"], len(pages_checked))

        return {
            "site": site_url,
            "crawled_at": datetime.datetime.now(datetime.UTC).isoformat(),
            "our_agent": OUR_UA,

            "robots_txt": {
                "found": bool(robots_text),
                "rules_by_agent": rules,
                "sitemap_urls": robots_sitemaps,            # Feature 8a
            },

            "sitemap": sitemap_data,                        # Feature 8b
            "llms_txt": llms_data,                          # Feature 8c

            "ua_probe": {
                "url_tested": site_url,
                "results": ua_results,
            },

            "pages_checked": pages_checked,

            "render_check": render_result,

            "coverage": {                                   # Feature 8d
                "links_discovered": links_discovered,
                "urls_known": urls_known,
                "pages_sampled": len(pages_checked),
                "sitemap_url_count": sitemap_data["url_count"],
                "discovery_method": "sitemap" if sitemap_data["found"] else "homepage_links",
            },

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