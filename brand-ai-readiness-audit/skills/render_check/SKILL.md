---
name: render-gap-audit
description: >
  Detect content that is invisible to AI crawlers because it only appears
  after JavaScript executes — missing body text, client-injected JSON-LD,
  JS-rewritten titles and headings, and inconsistent rendering within a
  page template. Consumes the evidence file produced by crawl-render-audit
  and emits findings with evidence, severity, and prioritized fixes. Use
  when diagnosing why a brand's pages are missing, thin, or indistinguishable
  from one another in AI assistants.
license: MIT
allowed-tools:
  - Read
  - Bash
---

# Render-Gap Audit

## When to use

After `crawl-render-audit` has produced an evidence file. This skill performs
no network requests of its own — it only judges recorded facts.

The split is deliberate: the crawler records, this skill judges. Keeping
judgment out of the crawler means thresholds can be tuned, re-run, and
reviewed without re-crawling a site.

## Inputs

A crawler evidence JSON file containing a `render_check` object with a
`pages[]` array. Each page entry supplies `raw_text_len`,
`rendered_text_len`, `text_delta_ratio`, `raw_title`, `rendered_title`,
`h1_present_raw`, `h1_present_rendered`, `jsonld_blocks_raw`, and
`jsonld_blocks_rendered`.

## Procedure

1. Read the evidence file (path argument, or `-` for stdin).
2. If `render_check.status` is not `completed`, emit a single informational
   finding recording that JS dependency is unverified, and stop. Never infer
   a verdict from a check that did not run.
3. Group rendered pages by `page_type`. All subsequent checks report per
   template, not per page — one template defect is one finding with one fix,
   not N duplicates.
4. Run four checks: body-text gap, JSON-LD gap, title/h1 gap, and
   intra-template inconsistency.
5. If no gap was found, emit an explicit clean-pass note so "checked and
   fine" is distinguishable from "never checked."
6. Emit coverage and data-quality caveats (sample size, failed renders,
   serialization artifacts).
7. Sort findings by severity and emit the report.

## Severity thresholds

`text_delta_ratio` = fraction of visible text absent from the raw HTML.

| Band | Severity |
|---|---|
| >= 0.70 | critical |
| 0.40 – 0.70 | high |
| 0.15 – 0.40 | medium |
| < 0.15 | no finding (normal widget noise) |

A page whose raw text is under 250 characters is treated as critical once
the delta clears the medium band, regardless of ratio — absolute
unreadability matters more than proportion.

The low band is deliberately conservative. Cookie banners, lazy-loaded
footers, and chat widgets all inject small amounts of text after load
without hiding anything meaningful. Flagging those would be a false
positive.

Rendered text measuring up to 8% *shorter* than raw is treated as DOM
serialization noise, not content loss, and is recorded as an informational
caveat rather than a finding.

## Output

```json
{
  "skill": "render-gap-audit",
  "site": "https://example.com/",
  "analyzed_from": "<crawler timestamp>",
  "summary": { "total_findings": 6, "critical": 1, "high": 2, "medium": 1, "low": 0, "info": 2 },
  "findings": [
    {
      "id": "F-RG-001",
      "title": "Primary content on 'product' pages requires JavaScript to appear",
      "severity": "critical",
      "evidence": "Template 'product': sampled 2/2 page(s) ... 93% of visible content is JS-only.",
      "suggested_action": { "summary": "...", "priority": "critical", "detail": "..." },
      "affected_pages": ["..."]
    }
  ]
}
```

Finding IDs are namespaced `F-RG-###` so the orchestrator can merge findings
from sibling skills without collisions.

## Usage

```bash
python scripts/crawl.py https://example.com > evidence.json
python scripts/analyze_render.py evidence.json

# or piped
python scripts/crawl.py https://example.com | python scripts/analyze_render.py -
```

## Guardrails

Read-only. No network access, no authenticated actions, no site
modification. Deterministic: the same evidence file always produces the
same findings.