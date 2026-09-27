---
name: proactive-recommendations
description: Generate proactive improvements that strengthen a website's AI discoverability and on-site engagement — additions like entity corroboration links, service schema, quotable fact statements, comparison pages, FAQ markup and llms.txt — that go beyond repairing detected defects. Reads the crawl evidence file and the already-assembled findings, and emits a separate recommendations list that never overlaps with them. Use this whenever an AI-readiness or brand-visibility audit needs suggestions that are additions rather than fixes, whenever an audit came back clean and still needs actionable advice, or whenever you are about to write a "finding" for something that is not actually broken.
license: MIT
allowed-tools: Read, Bash
---

# Proactive Recommendations

## When to use

Run this as the **last** step of an AI-readiness audit, after every detection
skill has finished and the findings list is assembled.

It exists to solve a specific failure. The rubric rewards suggestions that go
beyond the problems found, but if the only slot an audit has for advice is the
findings array, advice gets dressed up as a defect. You end up emitting
"Site has no FAQ page — severity: medium", which is not a defect: a site is not
broken for lacking an FAQ. A reader sees a false positive, and detection
accuracy suffers for something that was never a detection problem.

Giving additions their own skill and their own report section removes the
pressure to invent defects.

The vocabulary difference is load-bearing. Findings carry `evidence` and
`severity`. Recommendations carry `rationale`, `priority` and `effort`. A
proactive item has no severity because nothing is broken, and no evidence
because there is no defect to evidence. Keeping the words apart makes it
structurally impossible to confuse the two.

## Inputs

| Input | Required | Source |
|---|---|---|
| `evidence.json` | yes | `crawl-render-audit` output |
| `findings.json` | no, strongly preferred | the assembled findings array from all detection skills |

Without `findings.json` the skill still runs, but it cannot deduplicate, so it
may repeat advice a detection skill already gave.

This skill performs **no network access**. Every recommendation is a pure
function of the evidence file, so the same input always produces the same
output — which is what makes it testable offline and safe to re-run.

## Procedure

1. Confirm every other audit skill has completed and their findings are assembled.
2. Run the engine:

   ```bash
   python scripts/recommend.py evidence.json --findings findings.json --out proactive.json
   ```

3. Read `coverage.level` in the output. If it is `low` or `unknown`, the crawl
   could not see enough of the site to support absence claims, and the engine
   has dropped those rules. `crawl-render-audit` emits the `sitemap`,
   `llms_txt` and `coverage` blocks this needs directly — if `coverage` is
   still coming back `unknown`, check that you're running the current
   crawler (`skills/crawl-render-audit/scripts/crawl.py`), not an older
   evidence file.
4. Read `pending_checks`. Each entry is a rule that could not be evaluated.
   Surface these in the report rather than dropping them silently: "we did not
   check this" is honest, and quietly omitting a check reads as a miss.
5. Hand `proactive_recommendations` to the entrypoint skill for merging.

## Output

A sibling array to `findings`, never merged into it:

```json
{
  "proactive_recommendations": [
    {
      "id": "P-002",
      "title": "Add sameAs links to the Organization schema",
      "rationale": "An Organization node is present on 3 of 3 crawled pages but declares no sameAs property...",
      "priority": "high",
      "effort": "low",
      "confidence": "high",
      "evidence_class": "key-absence",
      "trigger": {"field": "Organization.sameAs", "condition": "absent"}
    }
  ],
  "coverage": {"level": "high", "pages_sampled": 3, "denominator": 3, "basis": "exhausted_links"},
  "suppressed_by_coverage": [],
  "pending_checks": []
}
```

`trigger` names the exact evidence field behind each item. It is not required
by the report schema, but it makes every recommendation traceable back to
something in the crawl rather than to an assistant's improvisation — which is
the difference between a rule engine and a plausible-sounding guess.

