# Brand AI-Readiness Audit

**Is your website actually visible to AI assistants?**

ChatGPT, Claude, and Perplexity don't browse the web the way a human does —
they depend on crawlers that may be blocked, servers that may respond
differently to a bot than to a browser, and content that may only exist
after JavaScript runs. A page that looks perfect in Chrome can be
completely invisible to the model deciding whether to cite your brand.

This toolkit answers that question with evidence, not guesswork. Point it
at a URL and it tells you exactly what an AI crawler sees, whether it can
get in, whether the content is readable without executing JavaScript, and
whether a visitor landing cold on a deep page — the common case for an
AI-assistant link — can tell where they are and what to do next.

## How it works

One skill crawls the site once and writes a single evidence file. Every
other skill reads that file and turns it into findings or recommendations
— none of them touch the network. That split means the analysis is
deterministic: the same crawl always produces the same report, and any
step can be re-run or tuned without hitting the site again.

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

`audit-orchestrator` runs the whole pipeline in one command. Any skill can
also run standalone against a saved evidence file — useful for iterating
on a single check without re-crawling.

## What it checks

**Can AI bots reach the site?**
Fetches `/robots.txt` and evaluates policy for 8 named agents — GPTBot,
ClaudeBot, PerplexityBot, OAI-SearchBot, ChatGPT-User, Claude-User, CCBot,
and Google-Extended — distinguishing bots that feed live citations from
bots that feed model training, since blocking one is a very different
decision from blocking the other. Then it probes the homepage as each of
GPTBot, PerplexityBot, and ClaudeBot alongside a real browser, catching
the cases robots.txt alone can't: a WAF or bot-management layer that
serves a stripped or challenge response to a bot even though robots.txt
says it's allowed.

**Can it read the content without running JavaScript?**
Most AI crawlers don't execute JavaScript. This toolkit renders a sample
of pages with headless Chromium and diffs the result against the raw
HTML — comparing visible text, JSON-LD structured data, the page title,
and the h1 — so a React or Next.js app that quietly moved its content
behind a client-side fetch gets caught before an assistant ever notices
it's missing.

**Would a visitor (or the assistant summarizing for one) know what to do?**
An AI assistant links directly to a deep page, not your homepage. Fourteen
checks measure whether that landing page gives a cold visitor enough to
orient: a clear heading structure, a breadcrumb trail, a next action,
substantive content that isn't buried in navigation chrome, images with
alt text, and a mobile viewport — plus a link graph that flags pages
nothing else on the site links to.

**What would make the brand easier to cite correctly?**
Beyond fixing defects, the toolkit looks for what's missing that assistants
specifically reward: `sameAs` links that corroborate the brand's identity
against independent sources, Service/Product/Offer schema that states
machine-readably what's actually being sold, a complete address and
contact point, an `llms.txt` orientation file, comparison pages, and named
case studies. Every recommendation is tied to a real number from the
crawl — "3 of 3 crawled pages" — never a vague impression, and claims that
the crawl sample can't actually support (like "no pricing page exists
anywhere on the site") are automatically downgraded or dropped rather than
risk a false positive.

## The pipeline

| Skill | Role |
|---|---|
| [`crawl-render-audit`](brand-ai-readiness-audit/skills/crawl-render-audit/SKILL.md) | Crawls the site: robots.txt, bot-vs-browser probing, sitemap and `llms.txt` discovery, a page sample with full engagement and structured-data extraction, a link graph, and Playwright-based render comparison. The only skill that touches the network. |
| [`retrievability-checker`](brand-ai-readiness-audit/skills/retrievability-checker/SKILL.md) | Access and infrastructure findings: robots.txt blocks, server-level bot discrimination, broken links, noindex pages. |
| [`render_check`](brand-ai-readiness-audit/skills/render_check/SKILL.md) | JavaScript-dependency findings: content, structured data, titles, and headings that only appear after rendering. |
| [`engagement_audit`](brand-ai-readiness-audit/skills/engagement_audit/SKILL.md) | Cold-landing UX findings: headings, orientation, calls to action, content density, friction, and orphan pages. |
| [`recommendation`](brand-ai-readiness-audit/skills/recommendation/SKILL.md) | Proactive, coverage-gated suggestions for AI discoverability — entity corroboration, schema, `llms.txt`, comparison and case-study content. |
| [`audit-orchestrator`](brand-ai-readiness-audit/skills/audit-orchestrator/SKILL.md) | Entry point. Runs the pipeline end to end and merges everything into one report. |

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

Writes `audit_report.json` (merged findings + suggested actions) to the
current directory.

```bash
python run_audit.py https://example.com \
  --evidence-file evidence.json \
  --findings-file findings.json
```

## Run a single skill

Each analyzer is a pure function of the evidence file, so you can inspect,
tune, or re-run one step without re-crawling:

```bash
# crawl once, save the evidence
python skills/crawl-render-audit/scripts/crawl.py https://example.com > evidence.json

# run any analyzer against the same evidence file, as many times as you like
python skills/retrievability-checker/scripts/check_retrievability.py evidence.json
python skills/render_check/scripts/analyze_render.py evidence.json
python skills/engagement_audit/engagement_audit.py evidence.json
python skills/recommendation/scripts/recommend.py evidence.json --findings findings.json --out proactive.json
```

## Sample finding

```json
{
  "id": "F-RG-001",
  "title": "Primary content requires JavaScript to appear",
  "severity": "critical",
  "evidence": "Template 'product': sampled 2/2 pages, 93% of visible content is JS-only.",
  "suggested_action": {
    "summary": "Server-render the primary content, or provide a pre-rendered fallback.",
    "priority": "critical"
  },
  "affected_pages": ["https://example.com/products/widget"]
}
```

## Design principles

- **Measure, then judge, separately.** Only the crawler fetches pages;
  every analyzer is a deterministic function of the evidence file. Same
  input, same output, every time — which makes results reproducible and
  the whole pipeline testable offline.
- **Findings are defects; recommendations are additions.** A finding
  carries `evidence` and `severity` because something is broken. A
  recommendation carries `rationale`, `priority`, and `effort` because
  nothing is broken — the site simply doesn't have it yet. Keeping the two
  vocabularies apart keeps "no FAQ page" from being dressed up as a bug.
- **No claim outruns the evidence.** Absence claims ("no comparison page
  exists anywhere on the site") are only made when the crawl sample can
  actually support them — full sitemap coverage, or an exhausted homepage
  link graph. Otherwise they're downgraded or dropped.
