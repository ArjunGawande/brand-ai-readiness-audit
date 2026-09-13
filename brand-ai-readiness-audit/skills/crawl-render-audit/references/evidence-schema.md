# Evidence Schema Reference (`evidence.json`)

The `crawl-render-audit` skill produces a single neutral JSON evidence object documenting facts observed during the crawl. It contains no subjective ratings, severity scores, or remediation advice.

```json
{
  "site": "https://example.com",
  "crawled_at": "2026-09-13T12:00:00.000000+00:00Z",
  "our_agent": "BrandAuditBot/1.0 (+https://example.org/audit; read-only)",
  "robots_txt": {
    "found": true,
    "rules_by_agent": {
      "GPTBot": "allowed",
      "OAI-SearchBot": "allowed",
      "ChatGPT-User": "allowed",
      "ClaudeBot": "allowed",
      "Claude-User": "allowed",
      "PerplexityBot": "allowed",
      "CCBot": "allowed",
      "Google-Extended": "allowed"
    }
  },
  "ua_probe": {
    "url_tested": "https://example.com",
    "results": [
      {
        "agent": "chrome_baseline",
        "status_code": 200,
        "page_size_bytes": 61734,
        "text_length": 4329,
        "final_url": "https://example.com",
        "failure_type": null
      },
      {
        "agent": "GPTBot",
        "status_code": 200,
        "page_size_bytes": 61734,
        "text_length": 4329,
        "final_url": "https://example.com",
        "failure_type": null
      }
    ]
  },
  "pages_checked": [
    {
      "requested_url": "https://example.com/products/widget",
      "final_url": "https://example.com/products/widget",
      "redirect_count": 0,
      "status_code": 200,
      "failure_type": null,
      "type": "/products/",
      "visible_text_length": 1420,
      "script_size": 18200,
      "page_size_bytes": 45000,
      "title": "Widget - Example",
      "h1_present": true,
      "h1_text": "Widget Pro",
      "h2_count": 4,
      "cta_count": 2,
      "word_count": 260,
      "reading_ease_score": 64.2,
      "outbound_internal_links_count": 8,
      "outbound_internal_urls": [
        "https://example.com/about",
        "https://example.com/contact"
      ],
      "has_structured_data": true,
      "jsonld_block_count": 1,
      "jsonld_raw": ["{...}"],
      "meta_robots": "index, follow",
      "canonical_url": "https://example.com/products/widget"
    }
  ],
  "render_check": {
    "status": "completed | unavailable | skipped | error",
    "render_sampled": true,
    "pages_rendered": [
      "https://example.com/products/widget"
    ],
    "pages": [
      {
        "url": "https://example.com/products/widget",
        "page_type": "/products/",
        "raw_text_len": 1420,
        "rendered_text_len": 1450,
        "text_delta_ratio": 0.02,
        "raw_title": "Widget - Example",
        "rendered_title": "Widget - Example",
        "h1_present_raw": true,
        "h1_present_rendered": true,
        "jsonld_blocks_raw": 1,
        "jsonld_blocks_rendered": 1,
        "main_content_source": "server",
        "noscript_fallback_len": 0
      }
    ],
    "template_inconsistent": false
  },
  "crawl_summary": {
    "pages_attempted": 1,
    "pages_succeeded": 1,
    "pages_failed": 0,
    "pages_rendered": 1
  }
}
```
