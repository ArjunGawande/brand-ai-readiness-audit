---
name: crawl-render-audit
description: Crawl a website read-only, check robots.txt rules for named AI agents, probe how the server responds to browser vs AI bot user-agents, evaluate render gaps via browser, and sample pages. Emits one neutral evidence JSON file. Makes no judgments — consumed by retrievability-checker, render-gap-audit, and engagement-audit.
license: MIT
allowed-tools: ["bash", "python"]
---

# Crawl & render audit

## When to use
First step of every audit. Downstream analyzer skills (`retrievability-checker`,
`render-gap-audit`, and `engagement-audit`) read this skill's evidence file and
perform no network requests themselves.

## Inputs
- A single URL (e.g. `https://example.com`)

## Procedure
1. Fetch `/robots.txt`, parse allow/disallow rules for AI agents.
2. Probe the homepage across multiple user-agents (`chrome_baseline`, `GPTBot`, `PerplexityBot`, `ClaudeBot`).
3. Collect internal links from the homepage, sampling up to 12 pages with politeness delays.
4. For each page, record status code, path group, visible text, script size, headings (H1/H2), CTAs, readability score, link connectivity, and JSON-LD structured data.
5. Sample pages for headless browser render-gap comparison.
6. Write everything to a single neutral JSON evidence object (see `references/evidence-schema.md`).

## Output
One JSON file adhering to `references/evidence-schema.md`. Example top-level structure:

```json
{
  "site": "https://example.com",
  "crawled_at": "ISO-8601 timestamp",
  "our_agent": "BrandAuditBot/1.0 (+https://example.org/audit; read-only)",
  "robots_txt": { "found": true, "rules_by_agent": {} },
  "ua_probe": { "url_tested": "https://example.com", "results": [] },
  "pages_checked": [],
  "render_check": { "status": "completed | unavailable | skipped | error", "pages": [] },
  "crawl_summary": { "pages_attempted": 0, "pages_succeeded": 0, "pages_failed": 0, "pages_rendered": 0 }
}
```