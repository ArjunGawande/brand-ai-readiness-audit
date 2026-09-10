---
name: retrievability-checker
description: Analyze the evidence file from crawl-render-audit for access and rendering problems — AI agents disallowed in robots.txt, servers refusing or stripping responses to bot user-agents, and pages that render near-empty without JavaScript. Emits findings with evidence, severity, and prioritized fixes. Touches no network; reads only the evidence file. Use after crawl-render-audit has run.
license: MIT
allowed-tools: ["bash", "python"]
---

# Retrievability checker

## When to use
Immediately after crawl-render-audit. This is the first analyzer in the
marketplace and gates the rest: if a site can't be reached or read, findings
about structured data or freshness are noise on top of a bigger root cause.

## Inputs
- Path to the evidence JSON produced by crawl-render-audit

## Procedure
1. Check whether the whole crawl was blocked. If so, emit one clear finding
   about incomplete coverage and skip page-level analysis.
2. Check robots.txt policy per agent, distinguishing citation-facing crawlers
   (blocking them removes the brand from answers) from training-only crawlers
   (blocking them may be deliberate).
3. Compare the homepage response to a browser vs to GPTBot. Flag differing
   status codes, or a matching status with far less content.
4. Flag pages whose content likely only exists after JavaScript runs. Report
   as a template-wide pattern only when 2+ pages of the same type show it.
5. Assign sequential ids, count severities, emit the report.

Detailed thresholds and rationale are in `references/checks.md`.

## Output
A findings JSON matching the shared marketplace shape: site, audited_at,
agent, summary counts by severity, and findings[] with id, title, severity,
evidence, and suggested_action. Zero findings is a valid result and is
reported with a notes field rather than manufactured problems.

## Usage
```bash
python scripts/check_retrievability.py evidence.json
```