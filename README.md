# Brand AI-Readiness Audit

A read-only audit toolkit that answers one question about a website: **can
AI assistants (ChatGPT, Claude, Perplexity, and their crawlers) actually
reach it, read it, and cite it correctly?**

It's built as a marketplace of small skills. One skill crawls the site and
writes a single evidence file; every other skill reads that file and turns
it into findings (defects) or recommendations (additions) — none of them
touch the network themselves. The whole thing runs offline against a
crawl you did once, so results are deterministic and re-runnable.

```
crawl-render-audit  →  evidence.json
                          │
        ┌─────────────────┼─────────────────┐
        ▼                 ▼                 ▼
retrievability-checker  render_check   engagement_audit
        │                 │                 │
        └────────── merged findings ────────┘
                          │
                          ▼
                    recommendation
                          │
                          ▼
                  audit_report.json
```

`audit-orchestrator` runs all of the above in order and writes the final
report; you can also run any skill on its own against a saved evidence
file.

## Skills

| Skill | What it does | Reads |
|---|---|---|
| [`crawl-render-audit`](brand-ai-readiness-audit/skills/crawl-render-audit/SKILL.md) | The only skill that touches the network. Checks robots.txt, probes the homepage as GPTBot/PerplexityBot/ClaudeBot vs a browser, discovers the sitemap and `llms.txt`, samples up to 12 internal pages (headings, calls to action, structured data, link graph), and renders a sample with headless Chromium to compare raw vs JS-rendered content. | the live site |
| [`retrievability-checker`](brand-ai-readiness-audit/skills/retrievability-checker/SKILL.md) | Can AI bots get in and read something? Flags robots.txt blocks (by role: citation vs training bots), servers that treat bots differently from browsers, broken sampled links, and `noindex` pages. | `evidence.json` |
| [`render_check`](brand-ai-readiness-audit/skills/render_check/SKILL.md) | Is the content actually there without JavaScript? Compares raw HTML to the browser-rendered DOM for body text, JSON-LD, title, and h1, grouped by page template. | `evidence.json` |
| [`engagement_audit`](brand-ai-readiness-audit/skills/engagement_audit/SKILL.md) | Would a visitor landing cold on a deep page (the common case for an AI-assistant link) know where they are and what to do? 14 rules covering headings, breadcrumbs, calls to action, thin/boilerplate content, popups, mobile viewport, image alt text, and orphan pages. | `evidence.json` |
| [`recommendation`](brand-ai-readiness-audit/skills/recommendation/SKILL.md) | Proactive additions, not defects — `sameAs` corroboration links, Service/Product schema, a complete address, `llms.txt`, comparison and case-study pages, a freshness signal. Deduplicates against the findings the other skills already raised, and drops absence claims the crawl sample can't actually support. | `evidence.json` + merged findings |
| [`audit-orchestrator`](brand-ai-readiness-audit/skills/audit-orchestrator/SKILL.md) | Entry point. Runs the four skills above in order and merges their output into one `audit_report.json`. | — (orchestrates the rest) |
| [`freshness-corroboration`](brand-ai-readiness-audit/skills/freshness-corroboration/SKILL.md) | **Not implemented.** Documented for what it was meant to do, why it currently duplicates parts of `recommendation`, and what it would take to build. | — |

## Setup

Requires Python 3.9+.

```bash
cd brand-ai-readiness-audit
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate

pip install -r skills/crawl-render-audit/scripts/requirements.txt
python -m playwright install chromium
```

## Run a full audit

```bash
source venv/bin/activate
python run_audit.py https://example.com
```

Writes `audit_report.json` (merged findings + suggested actions) and
`proactive.json` (the raw recommendation output, including coverage info)
to the current directory.

To keep the intermediate evidence and findings files instead of having
them deleted when the run finishes:

```bash
python run_audit.py https://example.com \
  --evidence-file /tmp/evidence.json \
  --findings-file /tmp/findings.json
```

## Run one skill at a time

Useful for inspecting an intermediate step, or re-running just the
analysis after tweaking a threshold — none of these steps re-crawl the site.

```bash
# 1. crawl once, save the evidence
python skills/crawl-render-audit/scripts/crawl.py https://example.com > evidence.json

# 2. run any analyzer against the same evidence file
python skills/retrievability-checker/scripts/check_retrievability.py evidence.json
python skills/render_check/scripts/analyze_render.py evidence.json
python skills/engagement_audit/engagement_audit.py evidence.json
python skills/recommendation/scripts/recommend.py evidence.json --findings findings.json --out proactive.json
```

## Evidence file

`crawl-render-audit` is the single source of truth every other skill reads.
Top-level keys: `robots_txt`, `ua_probe`, `sitemap`, `llms_txt`,
`pages_checked` (each with `engagement`, `links`, `text_stats`, `jsonld_raw`),
`link_graph`, `render_check`, `coverage`, `crawl_summary`. Full shape is in
[`crawl-render-audit/SKILL.md`](brand-ai-readiness-audit/skills/crawl-render-audit/SKILL.md#output).

## Design conventions

- **Measure vs. judge.** Only `crawl-render-audit` fetches pages. Every
  analyzer is a pure function of the evidence file — same input, same
  output, every time. This is what makes the pipeline testable offline.
- **Findings vs. recommendations.** A finding is a defect: it has
  `evidence` and `severity`, and something is actually broken. A
  recommendation is an addition: it has `rationale`, `priority`, and
  `effort`, and nothing is broken — the site just doesn't have it yet.
  Keeping the vocabulary separate stops "no FAQ page" from being dressed
  up as a medium-severity bug.
- **Coverage gates absence claims.** `recommendation` only says "no X
  exists anywhere on the site" when the crawl sample can actually support
  that claim (full sitemap coverage, or an exhausted homepage link graph).
  Otherwise the claim is downgraded or dropped rather than risk a false
  positive a reader can disprove in one click.

## Known issues

- `audit-orchestrator` deletes the evidence/findings files when it
  finishes, even when you passed `--evidence-file`/`--findings-file` to
  keep them — capture your own copy if you need it (see "Run one skill at
  a time" above, or pass separate output paths and copy them out before
  the orchestrator's cleanup runs).
- `freshness-corroboration` is unimplemented and not called by the
  orchestrator; see its own SKILL.md for why and what overlaps with
  `recommendation`.
- Folder names and skill frontmatter `name:` don't always match
  (`render_check` ↔ `render-gap-audit`, `recommendation` ↔
  `proactive-recommendations`, `engagement_audit` ↔ `engagement-audit`).
  Each skill's own SKILL.md notes this under "Known issues" where relevant.
- The `recommendation` skill's `references/rules.md`, referenced from its
  SKILL.md as the full rule catalogue, doesn't exist yet.
