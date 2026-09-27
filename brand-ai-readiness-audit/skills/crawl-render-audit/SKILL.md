---
name: crawl-render-audit
description: Crawl a website read-only — robots.txt rules for named AI agents, a browser-vs-bot probe of the homepage, sitemap and llms.txt discovery, a sample of internal pages with engagement/structured-data facts and a link graph, and a raw-vs-JS-rendered comparison via Playwright. Emits one neutral evidence JSON file; makes no judgments. Use this first, before any other skill in the marketplace.
license: MIT
allowed-tools: ["bash", "python"]
---

# Crawl & render audit

## When to use
First step of every audit. Every other skill in this marketplace reads this
skill's `evidence.json` and never fetches anything itself — this is the only
skill that makes network requests.

## Inputs
- A single URL (e.g. `https://example.com`)

## Procedure
1. Fetch `/robots.txt`; parse allow/disallow rules for 8 named agents
   (GPTBot, OAI-SearchBot, ChatGPT-User, ClaudeBot, Claude-User,
   PerplexityBot, CCBot, Google-Extended) and collect any `Sitemap:` lines.
2. Probe the homepage as a browser and as each of GPTBot, PerplexityBot,
   ClaudeBot; record status code and visible text length for each.
3. Sample up to `MAX_PAGES` (12) internal pages, starting with the homepage
   itself, then its internal links. For each: status, redirects, title,
   headings, JSON-LD blocks, meta robots/canonical, word/digit counts, and
   the full `engagement` block (main-content extraction, heading shape,
   calls to action, orientation signals, friction/interstitials, media,
   readability) and `links` (internal-in-main vs chrome, external domains).
4. Discover the sitemap (via robots.txt or `/sitemap.xml`, following a
   sitemap index up to 3 children) and check `/llms.txt`.
5. Build a link graph over the sampled pages: inbound counts, click depth
   from the homepage. Marked non-partial only when the sample provably
   covers every URL the sitemap lists.
6. Render a sample of pages with headless Chromium (Playwright) and compare
   raw vs rendered text length, title, h1, and JSON-LD count, grouped by
   page type. Degrades gracefully (`status: "unavailable"`) if Playwright
   isn't installed.
7. Write everything to one JSON object (see Output below).

## Output
```json
{
  "site": "string",
  "crawled_at": "ISO-8601 timestamp",
  "our_agent": "string",
  "robots_txt": {
    "found": "bool",
    "rules_by_agent": {"GPTBot": "allowed|blocked", "...": "..."},
    "sitemap_urls": ["string"]
  },
  "ua_probe": {"url_tested": "string", "results": [{"agent": "chrome_baseline|GPTBot|...", "status_code": "int|null", "text_length": "int", "failure_type": "string|null"}]},
  "sitemap": {"found": "bool", "url_count": "int", "urls": ["string"]},
  "llms_txt": {"found": "bool", "url": "string", "status_code": "int|null"},
  "pages_checked": [
    {
      "requested_url": "string", "status_code": "int", "type": "homepage|/path/",
      "visible_text_length": "int", "h1_present": "bool", "jsonld_raw": ["string"],
      "meta_robots": "string|null", "canonical_url": "string|null",
      "text_stats": {"char_count": "int", "digit_count": "int"},
      "engagement": {"heading_shape": {}, "cta": {}, "orientation": {}, "friction": {}, "media": {}, "readability": {}, "main_text_length": "int", "boilerplate_ratio": "float"},
      "links": {"internal_out": ["string"], "internal_in_main_count": "int", "external_domains": ["string"]}
    }
  ],
  "link_graph": {"nodes": [{"url": "string", "inbound_count_in_sample": "int", "click_depth_from_home": "int|null"}], "scope": {"is_partial": "bool"}},
  "render_check": {"status": "completed|unavailable|error|skipped", "pages": [{"url": "string", "page_type": "string", "text_delta_ratio": "float", "raw_title": "string", "rendered_title": "string"}]},
  "coverage": {"pages_sampled": "int", "links_discovered": "int"},
  "crawl_summary": {"pages_attempted": "int", "pages_succeeded": "int", "pages_failed": "int", "pages_rendered": "int"}
}
```

Consumers: `retrievability-checker` reads `robots_txt`, `ua_probe`,
`pages_checked`, `crawl_summary`. `render_check` reads `render_check`.
`engagement_audit` reads `pages_checked[].engagement`/`.links` and
`link_graph`. `recommendation` reads `pages_checked[].jsonld_raw`/
`.text_stats`, `sitemap`, `llms_txt`, `coverage`, `robots_txt.sitemap_urls`.

## Usage
```bash
python scripts/crawl.py https://example.com > evidence.json
```

## Dependencies
`httpx`, `selectolax`, `playwright` (+ `playwright install chromium`) — see
`scripts/requirements.txt`.
