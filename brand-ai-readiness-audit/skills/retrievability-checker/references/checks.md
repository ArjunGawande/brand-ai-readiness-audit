# Retrievability checks

Checks run in order against the crawler's evidence file. Every finding
cites specific numbers from that file. If a check finds nothing wrong, it
reports nothing — no problem is manufactured to fill the report.

---

## Check A (gate) — Was the whole crawl blocked?

**Reads:** `ua_probe.results[]` (the `chrome_baseline` entry), `crawl_summary`

If the browser-UA fetch of the homepage did not return 200 (or hit a
`failure_type` such as timeout, connection error, or redirect loop), the
audit never got in at all. In that case we emit one finding about
incomplete coverage and skip the page-level checks entirely.

**Why it gates:** listing granular findings from an empty `pages_checked`
array would imply coverage the audit doesn't have. One honest finding beats
a report that looks thorough but is built on nothing.

**Severity:** critical.

---

## Check B — robots.txt policy

**Reads:** `robots_txt.found`, `robots_txt.rules_by_agent.<agent>`

For each of 8 agents, if the site's stated policy disallows it, that's a
finding. The severity is the same (critical) but the *advice* differs by
role:

| Role | Agents | What blocking costs |
|---|---|---|
| citation | OAI-SearchBot, ChatGPT-User, Claude-User, PerplexityBot | The brand can never appear as a cited source in that assistant's answers |
| training | GPTBot, ClaudeBot, CCBot, Google-Extended | Content won't enter model training data — often a deliberate opt-out |

**Also flagged:** no robots.txt at all (`found: false`) — medium severity.

---

## Check C — Server-level block (UA probe differential)

**Reads:** `ua_probe.results[]` for `GPTBot`, `PerplexityBot`, `ClaudeBot`,
compared against the `chrome_baseline` entry; `robots_txt.rules_by_agent`

Two ways a server can refuse a bot even when robots.txt allows it:

- **Fetch failed** for that agent (`failure_type` set).
- **Different status code** — browser gets 200, the bot gets something else.
- **Same status, far less content** — both get 200, but the bot's response
  carries under `TEXT_RATIO_FLOOR` (0.2, i.e. 20%) of the browser's visible
  text. A 200 that's actually a challenge page or stripped shell.

**The most valuable case this catches:** robots.txt *allows* the agent but
the server refuses it anyway — the finding notes this contradiction
explicitly.

**Skipped when:** the browser baseline itself failed — that's Check A's job.

**Severity:** critical.

---

## Check D — Broken sampled links

**Reads:** `pages_checked[].failure_type`

Any sampled page that failed to fetch (grouped by failure type). Not a bot
issue, but affects overall crawl reliability.

**Severity:** medium.

---

## Check E — noindex pages

**Reads:** `pages_checked[].meta_robots`

Any sampled page whose `<meta name="robots">` content includes "noindex".

**Severity:** high.

---

## Not covered by this skill

JavaScript-dependent content (raw vs rendered text) was moved to
`render_check`, which reads the crawler's `render_check` block. This skill
does not duplicate that check.

---

## Thresholds, and why these numbers

| Constant | Value | Reasoning |
|---|---|---|
| `TEXT_RATIO_FLOOR` | 0.2 | Below 20% of the browser's text, a same-status response is almost certainly a challenge or stripped page, not a slightly different render |

These are deliberately conservative — each threshold is set where a human
reviewing the evidence would agree without argument.

---

## Determinism

No network calls, no randomness. The same evidence file produces identical
findings every run.
