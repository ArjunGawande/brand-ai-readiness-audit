---
name: engagement-audit
description: Judge whether a visitor (human or an AI assistant's cited link) landing cold on a sampled page can tell where they are, what the page is, and what to do next. Reads the engagement facts (headings, calls to action, breadcrumbs, popups, media, readability, link graph) that crawl-render-audit collects and turns them into findings. Touches no network. Use after crawl-render-audit has run.
license: MIT
allowed-tools: ["bash", "python"]
---

# Engagement audit

## When to use
After `crawl-render-audit` has produced an evidence file. This skill makes
no network requests — it only judges facts the crawler already measured.
It is the mirror image of `recommendation`: this skill reports defects
(finding + evidence + severity); recommendation reports additions
(rationale + priority + effort). The question behind every rule here is the
same one: an AI assistant drops a visitor onto a *deep* page, not the
homepage — would that visitor, with no memory of the rest of the site, know
where they are, what this is, and what to do next?

## Inputs
The evidence JSON from `crawl-render-audit`, specifically each page's
`engagement` block (`heading_shape`, `orientation`, `cta`, `friction`,
`media`, `readability`, `main_text_length`, `boilerplate_ratio`),
`links.internal_in_main_count`, and the `link_graph` (`nodes[].click_depth_from_home`,
`nodes[].inbound_count_in_sample`, `scope.is_partial`).

## Procedure
Runs 14 rules over pages that returned status 200, one finding per rule
listing every offending page:

| Rule | Fires when |
|---|---|
| ENG-001 | No `<h1>` on the page |
| ENG-002 | More than one `<h1>` |
| ENG-003 | Heading levels skip a step (e.g. h1 straight to h3) |
| ENG-004 | Page is 2+ clicks deep and has no breadcrumb |
| ENG-005 | No call to action anywhere (button, form, submit, action link) |
| ENG-006 | Main content has no internal links (nowhere to go but the menu) |
| ENG-007 | Over 85% of visible text is boilerplate (nav/header/footer) |
| ENG-008 | Main content is 1–149 characters (thin once chrome is stripped) |
| ENG-009 | No `<meta name="viewport">` tag |
| ENG-010 | 2+ cookie/consent/modal/popup elements detected |
| ENG-011 | 4+ head scripts without `async`/`defer` (render-blocking) |
| ENG-012 | Average over 28 words/sentence across 5+ sentences |
| ENG-013 | Over 50% of a page's images have no `alt` attribute |
| ENG-014 | Zero inbound links in the sampled link graph (skipped unless the graph covers the whole site) |

Severity comes from one matrix: **prevalence** (share of sampled pages
affected: ≥70% high, ≥30% medium, else low) × **importance** (high if the
homepage or a page ≤1 click from home is affected).

## Output
```json
{
  "site": "https://example.com",
  "audited_at": "ISO-8601 timestamp",
  "summary": {"total_findings": 7, "critical": 0, "high": 1, "medium": 6, "low": 0, "pages_sampled": 3},
  "findings": [
    {
      "id": "ENG-005",
      "title": "Pages offer no next action",
      "severity": "high",
      "evidence": "2 of 3 sampled pages contain no button, form, or action-worded link...",
      "affected_pages": ["https://example.com/technology"],
      "suggested_action": {"summary": "...", "priority": "high"}
    }
  ],
  "pending_checks": []
}
```

## Usage
```bash
python engagement_audit.py evidence.json
python engagement_audit.py evidence.json --out findings.json
```

## Known issues
- No `scripts/` subfolder — `engagement_audit.py` sits directly in the
  skill directory, unlike its siblings.
- `marketplace.json` lists this skill as `skills/engagement-audit`
  (hyphenated); the real path uses an underscore.
- If the crawler's `engagement` block is ever missing from a page (it
  shouldn't be, after the crawler restore), rules fall back to empty
  defaults and every 200 page would be falsely flagged for no h1, no CTA,
  and no viewport — check `pending_checks` for a warning when this happens.
