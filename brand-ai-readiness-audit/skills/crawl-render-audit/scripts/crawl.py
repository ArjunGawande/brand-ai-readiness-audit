#!/usr/bin/env python3
"""
Simple site crawler for the AI-readiness audit.
Checks robots.txt rules, probes GPTBot vs a browser, samples a few
pages, and prints one JSON evidence file. Makes no judgments —
that's the analyzer skill's job.

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

BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0"
GPTBOT_UA = "GPTBot"
OUR_UA = "BrandAuditBot/1.0 (+https://example.org/audit; read-only)"
CHECK_AGENTS = ["GPTBot", "PerplexityBot", "ClaudeBot"]
MAX_PAGES = 12
TIMEOUT = 10.0
DELAY_SECONDS = 0.3  # politeness delay between page fetches


def parse_robots(text: str, agents: list) -> dict:
    """Tiny robots.txt reader: True/False per agent, no external library."""
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
        result[agent] = "/" not in blocked
    return result


async def fetch(client: httpx.AsyncClient, url: str, ua: str):
    try:
        return await client.get(
            url, headers={"User-Agent": ua}, timeout=TIMEOUT, follow_redirects=True
        )
    except httpx.HTTPError:
        return None


def analyze_html(html: str) -> dict:
    """Pull out the few facts each finding needs."""
    tree = HTMLParser(html)
    script_bytes = sum(len(s.text() or "") for s in tree.css("script"))
    for tag in tree.css("script, style"):
        tag.decompose()
    visible_text = tree.body.text(separator=" ", strip=True) if tree.body else ""
    title = tree.css_first("title")
    return {
        "visible_text_length": len(visible_text),
        "script_size": script_bytes,
        "title": title.text(strip=True) if title else "",
        "has_structured_data": "application/ld+json" in html,
    }


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


async def crawl(site_url: str) -> dict:
    async with httpx.AsyncClient() as client:
        # 1. robots.txt
        robots_resp = await fetch(client, urljoin(site_url, "/robots.txt"), OUR_UA)
        robots_text = robots_resp.text if robots_resp and robots_resp.status_code == 200 else ""
        allowed = parse_robots(robots_text, CHECK_AGENTS)

        # 2. UA probe on homepage — same URL, two identities
        chrome_resp = await fetch(client, site_url, BROWSER_UA)
        gptbot_resp = await fetch(client, site_url, GPTBOT_UA)

        chrome_info = analyze_html(chrome_resp.text) if chrome_resp and chrome_resp.status_code == 200 else None
        gptbot_info = analyze_html(gptbot_resp.text) if gptbot_resp and gptbot_resp.status_code == 200 else None

        homepage_test = {
            "as_chrome": {
                "status_code": chrome_resp.status_code if chrome_resp else None,
                "text_length": chrome_info["visible_text_length"] if chrome_info else 0,
            },
            "as_gptbot": {
                "status_code": gptbot_resp.status_code if gptbot_resp else None,
                "text_length": gptbot_info["visible_text_length"] if gptbot_info else 0,
            },
        }

        # 3. sample a few pages, found via links on the homepage
        pages_checked = []
        if chrome_resp and chrome_resp.status_code == 200:
            for url in internal_links(site_url, chrome_resp.text)[:MAX_PAGES]:
                resp = await fetch(client, url, OUR_UA)
                await asyncio.sleep(DELAY_SECONDS)
                if resp is None:
                    continue
                info = analyze_html(resp.text) if resp.status_code == 200 else {
                    "visible_text_length": 0, "script_size": 0, "title": "", "has_structured_data": False
                }
                pages_checked.append({
                    "url": url,
                    "status_code": resp.status_code,
                    "type": page_type(url),
                    "visible_text_length": info["visible_text_length"],
                    "script_size": info["script_size"],
                    "has_structured_data": info["has_structured_data"],
                    "title": info["title"],
                })

        return {
            "site": site_url,
            "crawled_at": datetime.datetime.utcnow().isoformat() + "Z",
            "robots_txt": {
                "found": bool(robots_text),
                **{f"{a}_allowed": allowed[a] for a in CHECK_AGENTS},
            },
            "homepage_test": homepage_test,
            "pages_checked": pages_checked,
        }


def main():
    if len(sys.argv) < 2:
        print("Usage: python crawl.py https://example.com")
        sys.exit(1)
    result = asyncio.run(crawl(sys.argv[1]))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()