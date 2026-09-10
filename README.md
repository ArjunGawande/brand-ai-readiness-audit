# crawl-render-audit

Read-only crawler for the brand AI-readiness audit marketplace. Given a URL,
it checks whether AI agents are allowed in, whether the server treats them
differently from a browser, and samples a handful of pages — then writes one
neutral evidence file. It makes no judgments; that's the analyzer skills' job.

## What it checks

- **robots.txt** — whether GPTBot, PerplexityBot, and ClaudeBot are allowed
- **UA probe** — fetches the homepage as a browser and as GPTBot, compares
  status code and visible text length
- **Page sample** — follows up to 12 internal links from the homepage and
  records, per page: status code, path group, visible text length, script
  size, title, and whether it has any JSON-LD structured data

## Requirements

- Python 3.9+

## Setup

```bash
cd skills/crawl-render-audit/scripts

python -m venv venv
.\venv\Scripts\Activate.ps1        # Windows: venv\Scripts\activate

pip install -r requirements.txt
```

## Run

```bash
python crawl.py https://example.com
```

Save the output instead of printing it:

```bash
python crawl.py https://example.com > evidence.json
```

Deactivate the environment when done:

```bash
deactivate
```

## Output

One JSON object, printed to stdout. Full field-by-field shape is documented
in `references/evidence-schema.md`.

```json
{
  "site": "https://example.com",
  "crawled_at": "2026-09-06T10:15:00Z",
  "robots_txt": {
    "found": true,
    "GPTBot_allowed": false,
    "PerplexityBot_allowed": true,
    "ClaudeBot_allowed": true
  },
  "homepage_test": {
    "as_chrome": { "status_code": 200, "text_length": 8420 },
    "as_gptbot": { "status_code": 403, "text_length": 145 }
  },
  "pages_checked": [
    {
      "url": "https://example.com/products/widget",
      "status_code": 200,
      "type": "/products/",
      "visible_text_length": 620,
      "script_size": 41000,
      "has_structured_data": false,
      "title": "Widget – Example"
    }
  ]
}
```

## Notes

- Fetches under its own name (`BrandAuditBot/1.0`) for the actual crawl —
  the GPTBot user-agent is only used for the single homepage probe, to check
  how the server reacts, never for the full crawl.
- Adds a short delay between page fetches to stay polite to the target server.
- Designed to hand its output straight to `retrievability-checker`, which
  turns these raw numbers into findings with severity and suggested fixes.
