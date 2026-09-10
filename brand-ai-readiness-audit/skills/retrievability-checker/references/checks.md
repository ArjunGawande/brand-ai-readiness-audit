# Retrievability checks

Four checks, run in order against the crawler's evidence file. Every finding
cites specific numbers from that file. If a check finds nothing wrong, it
reports nothing — no problem is manufactured to fill the report.

---

## Check 0 (gate) — Was the whole crawl blocked?

**Reads:** `crawl_status`, `homepage_test.as_chrome.status_code`

If the homepage returned anything other than 200 to a *browser* user-agent,
the audit never got in at all. In that case we emit one finding about
incomplete coverage and skip the page-level checks entirely.

**Why it gates:** listing granular findings from an empty `pages_checked`
array would imply coverage the audit doesn't have. One honest finding beats
a report that looks thorough but is built on nothing.

**Note it makes explicitly:** a 403 to a browser UA usually means a
bot-protection layer rejecting all non-browser clients (TLS fingerprinting,
missing browser headers), not a rule aimed at AI crawlers specifically.
Those are different problems and the evidence says so rather than conflating
them.

**Severity:** critical.

---

## Check 1 — robots.txt policy

**Reads:** `robots_txt.found`, `robots_txt.<agent>_allowed`

For each agent, if the site's stated policy disallows it, that's a finding.
The severity is the same but the *advice* differs by what the agent does:

| Agent | Role | What blocking it costs |
|---|---|---|
| PerplexityBot | citation | The brand can never appear as a cited source in that assistant's answers |
| GPTBot | training | Content won't enter model training data — often a deliberate opt-out |
| ClaudeBot | training | Same as GPTBot |

**Why the distinction matters:** blocking a training crawler is a defensible
business decision. Blocking a citation crawler is usually an accident — the
site owner pasted a "block AI scrapers" snippet and didn't realize it also
removed them from live answers. The suggested action reflects which situation
they're in rather than telling everyone to unblock everything.

**Also flagged:** no robots.txt at all (`found: false`) — medium severity.
Not broken, but the site has no stated policy, which is ambiguous.

---

## Check 2 — Server-level block

**Reads:** `homepage_test.as_chrome` vs `homepage_test.as_gptbot`

robots.txt is a request. Some servers go further and refuse bots at the door.
Two ways that shows up:

**2a. Different status codes** — browser gets 200, GPTBot gets 403. Outright
refusal at the infrastructure layer.

**2b. Same status, far less content** — both get 200, but the bot's response
carries under 20% of the browser's visible text. A 200 that's actually a
challenge page or stripped shell. This one is easy to miss in server logs,
because everything looks healthy.

**The most valuable case this catches:** robots.txt *allows* the agent but
the server refuses it anyway. Policy and infrastructure disagree, which
almost always means the site owner doesn't know it's happening. The finding
calls this contradiction out explicitly.

**Skipped when:** the browser baseline itself failed — that's Check 0's job.

**Severity:** critical.

---

## Check 3 — JavaScript-dependent pages

**Reads:** `pages_checked[].visible_text_length`, `.script_size`, `.type`

A page qualifies as JS-dependent when **both** are true:

- visible text under 500 characters
- script bytes more than 10× the visible text

Most AI crawlers don't execute JavaScript. A page meeting both conditions
looks complete to a human and near-empty to the crawler.

**Pattern vs one-off:** findings are grouped by `type` (the URL's first path
segment, e.g. `/products/`). The severity depends on how many pages of that
type are affected:

| Pages affected | Severity | What it claims |
|---|---|---|
| 2 or more of the same type | high | Template-wide problem, e.g. "2 of 2 sampled /products/ pages" |
| Exactly 1 | medium | Reports the single page, and states explicitly that this is not yet established as a pattern |

**Why the split:** the claim "0 of 12 product pages have X" is far stronger
than "this one page has X," and only the first justifies a template-level fix
recommendation. Claiming a pattern from one sample is exactly the kind of
false positive the rubric penalizes.

---

## Thresholds, and why these numbers

| Constant | Value | Reasoning |
|---|---|---|
| `TEXT_RATIO_FLOOR` | 0.2 | Below 20% of the browser's text, a same-status response is almost certainly a challenge or stripped page, not a slightly different render |
| `JS_HEAVY_RATIO` | 10 | Normal content pages run roughly 1:1 to 5:1 script-to-text. 10:1 with thin text is a strong client-rendering signal |
| `THIN_TEXT_CHARS` | 500 | Under ~500 characters there isn't enough for an assistant to extract a usable fact, regardless of cause |
| `PATTERN_MIN_PAGES` | 2 | The minimum needed to say a problem repeats across a template rather than affecting one page |

These are deliberately conservative — the rubric weighs false positives
heavily, so each threshold is set where a human reviewing the evidence would
agree without argument.

---

## Determinism

No network calls, no randomness, no time-dependent logic beyond the
`audited_at` timestamp. The same evidence file produces identical findings
every run, which is what makes the marketplace's output reproducible and
testable against fixtures.