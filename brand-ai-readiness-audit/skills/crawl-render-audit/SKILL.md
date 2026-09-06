---
name: crawl-render-audit
description: Crawl a website read-only, check robots.txt rules for named AI agents, probe how the server responds to a browser vs an AI bot user-agent, and sample pages grouped by path. Emits one neutral evidence JSON file. Makes no judgments — use retrievability-checker to turn this evidence into findings. Use this first, before any other skill in the marketplace.
license: MIT
allowed-tools: ["bash", "python"]
---

# Crawl & render audit

## When to use
First step of every audit. Every other skill in this marketplace reads this
skill's output file and never fetches anything itself.

## Inputs
- A single URL (e.g. `https://example.com`)

## Procedure
1. Fetch `/robots.txt`, parse allow/disallow rules for named AI agents
   (GPTBot, PerplexityBot, ClaudeBot).
2. Fetch the homepage twice: once as a normal browser, once as GPTBot.
   Compare status code and visible text length.
3. Collect internal links from the homepage, sample up to 12 pages.
4. For each page, record status code, path group, visible text length,
   script size, title, and whether it contains any JSON-LD structured data.
5. Write everything to a single JSON object (see references/evidence-schema.md).

## Output
One JSON file with this shape:

```json
{
  "site": "string",
  "crawled_at": "ISO-8601 timestamp",
  "robots_txt": {
    "found": "bool",
    "GPTBot_allowed": "bool",
    "PerplexityBot_allowed": "bool",
    "ClaudeBot_allowed": "bool"
  },
  "homepage_test": {
    "as_chrome": { "status_code": "int|null", "text_length": "int" },
    "as_gptbot":  { "status_code": "int|null", "text_length": "int" }
  },
  "pages_checked": [
    {
      "url": "string",
      "status_code": "int",
      "type": "string, e.g. /products/",
      "visible_text_length": "int",
      "script_size": "int",
      "has_structured_data": "bool",
      "title": "string"
    }
  ]
}
```