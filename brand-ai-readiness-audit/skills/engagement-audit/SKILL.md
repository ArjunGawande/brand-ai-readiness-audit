---
name: engagement-audit
description: >
  Audit on-site user and AI engagement from crawled page evidence. Evaluates
  heading hierarchy, presence of clear next-step calls-to-action (CTAs),
  navigation dead ends, orphan pages, and readability / jargon density. Emits
  actionable, evidence-backed findings with prioritized remediations.
license: MIT
allowed-tools:
  - Read
  - Bash
---

# Engagement Audit

## When to use

After `crawl-render-audit` has generated the website evidence file. This skill
performs no network requests of its own; it evaluates observed content structure,
navigation graph connectivity, and readability from recorded crawl data.

## Inputs

A crawler evidence JSON file produced by `crawl-render-audit` (file path or `-` for stdin)
containing `pages_checked[]`.

## Procedure

1. Parse the evidence JSON file.
2. If `pages_checked` is empty (e.g. due to an upfront WAF block), record an informational
   finding that engagement could not be audited due to lack of accessible pages.
3. **Heading Hierarchy Check**: Verify each sampled page has a primary `<h1>` and that
   substantive pages (> 300 words) provide `<h2>` subheadings to organize content.
4. **Call-to-Action (CTA) Check**: Identify key templates and pages that lack buttons,
   action inputs, or CTA links (`cta_count == 0`), leaving visitors with no clear next step.
5. **Dead-End Page Check**: Detect pages with zero outbound internal links
   (`outbound_internal_links_count == 0`), stranding users and automated agents.
6. **Orphan Page Check**: Build an internal reference graph from `outbound_internal_urls`
   to detect sampled pages with zero inbound links from other pages.
7. **Readability & Text Density Check**: Evaluate the Flesch Reading Ease score across
   pages. Flag dense, jargon-heavy text walls (score < 30) that impair human comprehension
   and AI content extraction.
8. Consolidate findings, assign unique namespaced IDs (`F-ENG-###`), and emit the report.

## Output Schema

```json
{
  "skill": "engagement-audit",
  "site": "https://example.com",
  "audited_at": "2026-09-13T12:00:00Z",
  "summary": {
    "total_findings": 2,
    "critical": 0,
    "high": 1,
    "medium": 1,
    "low": 0
  },
  "findings": [
    {
      "id": "F-ENG-001",
      "title": "Pages lack clear calls-to-action or next navigation steps",
      "severity": "medium",
      "evidence": "2 of 3 sampled pages have 0 CTA elements or action buttons.",
      "suggested_action": {
        "summary": "Add clear action buttons or prominent next-step links to guide users.",
        "priority": "medium"
      },
      "affected_pages": [
        "https://example.com/team"
      ]
    }
  ]
}
```

## Usage

```bash
python scripts/check_engagement.py path/to/evidence.json
```
or piped directly from the crawler:
```bash
python skills/crawl-render-audit/scripts/crawl.py https://example.com | python skills/engagement-audit/scripts/check_engagement.py -
```
