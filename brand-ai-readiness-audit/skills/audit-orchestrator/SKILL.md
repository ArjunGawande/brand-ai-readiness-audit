---
name: audit-orchestrator
description: Run the full brand AI-readiness audit end to end — crawl a site, run every analyzer skill against the evidence, merge their findings, run the recommendation engine, and write one combined report. Use this when the user just wants "an audit" of a URL and doesn't need to inspect intermediate steps.
license: MIT
allowed-tools: ["bash", "python"]
---

# Audit orchestrator

## When to use
The single entry point for a full audit. It performs no analysis itself —
it only runs the other skills in order and combines what they return. Use
the individual skills directly only when you need to inspect or re-run one
step in isolation.

## Inputs
- A single URL (`https` is assumed if no scheme is given)
- Optional `--evidence-file` / `--findings-file` to keep the intermediate
  JSON instead of the default temp location

## Procedure
Runs each step as a subprocess, in order, stopping at the first non-zero
exit code:

1. `crawl-render-audit/scripts/crawl.py <url>` → evidence JSON
2. `retrievability-checker/scripts/check_retrievability.py <evidence>`
3. `render_check/scripts/analyze_render.py <evidence>`
4. `engagement_audit/engagement_audit.py <evidence>`
5. `recommendation/scripts/recommend.py <evidence> --findings <findings> --out proactive.json`

Steps 2–4 each return a findings report; the orchestrator concatenates their
`findings` arrays and sums their severity counts. It does no deduplication,
scoring, or weighting of its own — that's each analyzer's job. Step 5's
`proactive_recommendations` become the report's `suggested_actions`.

`freshness-corroboration` is not called (see that skill's own SKILL.md).

## Output
`audit_report.json` in the current directory:

```json
{
  "site": "https://example.com",
  "audited_at": "ISO-8601 timestamp",
  "skills_run": ["retrievability-checker", "render-gap-audit", "engagement-audit"],
  "summary": {"total_findings": 9, "critical": 1, "high": 3, "medium": 5, "low": 0, "info": 1},
  "findings": [ /* merged from steps 2-4 */ ],
  "suggested_actions": [ /* from step 5's proactive_recommendations */ ]
}
```

`proactive.json` (step 5's raw output, including `coverage` and
`pending_checks`) is also written alongside it.

## Usage
```bash
python scripts/run_audit.py https://example.com
python scripts/run_audit.py https://example.com --evidence-file /tmp/evidence.json --findings-file /tmp/findings.json
```

## Known issues
- Deletes the evidence/findings files when it finishes, even when you asked
  it to write them to a specific path with `--evidence-file`/`--findings-file`.
- Reads each analyzer's `coverage_notes`; engagement_audit actually emits
  `pending_checks`, so those notes are silently dropped from the merged report.
- `skills_run` names the render skill `render-gap-audit` (its frontmatter
  name), not `render_check` (its folder name).
