#!/usr/bin/env python3
"""
Simple site crawler for the AI-readiness audit.
Checks robots.txt rules, sitemap and llms.txt, probes multiple AI bot UAs
vs a browser, detects render gaps (raw HTML vs JS-rendered DOM), samples
pages, collects engagement facts and a link graph, and prints one JSON
evidence file. Makes no judgments — that's the analyzer skills' job:

  retrievability-checker  -> robots_txt, ua_probe, pages_checked, crawl_summary
  render_check            -> render_check, crawl_summary
  engagement_audit        -> pages_checked[].engagement / .links, link_graph
  recommendation          -> pages_checked[].jsonld_raw / .text_stats,
                             sitemap, llms_txt, coverage, robots_txt.sitemap_urls

Usage: python crawl.py https://example.com
"""

import asyncio
import datetime
import json
import re
import sys
from collections import defaultdict, deque
from urllib.parse import urljoin, urlparse

import httpx
from selectolax.parser import HTMLParser

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0"
OUR_UA = "BrandAuditBot/1.0 (+https://example.org/audit; read-only)"

# Feature 2: Full list of agents we probe at the door (was only GPTBot before)
PROBE_AGENTS = ["GPTBot", "PerplexityBot", "ClaudeBot"]

# Agents we check in robots.txt (broader list — includes citation bots)
ROBOTS_AGENTS = [
    "GPTBot", "OAI-SearchBot", "ChatGPT-User",
    "ClaudeBot", "Claude-User",
    "PerplexityBot", "CCBot", "Google-Extended",
]

MAX_PAGES = 12
TIMEOUT = 10.0
DELAY_SECONDS = 0.3  # politeness delay between page fetches

# Feature 1: Render-gap config
RENDER_SAMPLE_LIMIT = 5          # max pages to render with Playwright
RENDER_WAIT_MS = 3000            # wait for JS to settle after load
THIN_TEXT_THRESHOLD = 500        # raw text below this → worth rendering

# Sitemap config
SITEMAP_CHILD_LIMIT = 3          # child sitemaps followed from a sitemap index
SITEMAP_URL_CAP = 500            # URLs stored in the evidence file (count is uncapped)

# ---------------------------------------------------------------------------
# Engagement config
# ---------------------------------------------------------------------------

# Elements that are site chrome rather than page content. Stripping these is
# how we separate "this page has 3,000 characters" from "this page has 3,000
# characters of navigation and 180 of actual content".
BOILERPLATE_SELECTOR = (
    "nav, header, footer, aside, "
    '[role="navigation"], [role="banner"], [role="contentinfo"], '
    '[class*="nav"], [class*="menu"], [class*="footer"], [class*="header"], '
    '[id*="nav"], [id*="footer"], [id*="header"]'
)

# Preferred containers for the real content, in order of confidence. If none
# match we fall back to body-minus-boilerplate.
MAIN_SELECTORS = ["main", '[role="main"]', "article", "#content", "#main", ".content"]

# Link text that reads as an action rather than navigation. Matched on the
# whole normalised anchor text, not a substring, so "contact us" counts and
# "how we contact suppliers" does not.
ACTION_PHRASES = {
    "buy", "buy now", "shop", "shop now", "order", "order now", "add to cart",
    "get started", "start", "start free", "start now", "try", "try free",
    "try it free", "free trial", "sign up", "signup", "register", "join",
    "book", "book a demo", "book now", "request a demo", "get a demo",
    "request a quote", "get a quote", "contact us", "contact", "talk to us",
    "talk to sales", "schedule a call", "download", "subscribe", "apply",
    "apply now", "learn more", "see pricing", "view pricing", "get in touch",
}

# Class/id fragments that mark an element as a styled call-to-action even when
# its text is unusual ("Take me there", "Let's go").
CTA_CLASS_HINTS = ("btn", "button", "cta", "call-to-action")

# Things that sit between the visitor and the content on arrival. We only
# count them — whether a cookie banner is a problem depends on whether it
# blocks the page, which we cannot tell from raw HTML alone.
INTERSTITIAL_HINTS = (
    "cookie", "consent", "gdpr", "modal", "popup", "pop-up", "overlay",
    "newsletter", "subscribe-modal", "interstitial", "paywall", "age-gate",
)

BREADCRUMB_SELECTOR = (
    'nav[aria-label*="readcrumb"], '
    '[class*="breadcrumb"], [id*="breadcrumb"], '
    '[itemtype*="BreadcrumbList"]'
)

HEADING_TEXT_CAP = 120     # per-heading text kept in the outline
MAIN_SNIPPET_CHARS = 1500  # main-content excerpt; longer than the body snippet
                           # because answerability-simulation reads this one
LINK_SAMPLE_CAP = 40       # outbound links stored per page, for the graph

# ---------------------------------------------------------------------------
# Feature 5: Smarter fetch — categorizes failures instead of returning None
# ---------------------------------------------------------------------------
# OLD: any exception → return None → page silently dropped
# NEW: every fetch returns a dict with status, body, and failure_type
#      so the analyzer can distinguish "timed out" from "403 blocked"
#      from "DNS failed" — each means something different

async def fetch(client: httpx.AsyncClient, url: str, ua: str) -> dict:
    """
    Always returns a dict, never None.
    - On success: {"ok": True, "status_code": 200, "body": "...", ...}
    - On failure: {"ok": False, "failure_type": "timeout", ...}
    """
    try:
        resp = await client.get(
            url,
            headers={"User-Agent": ua},
            timeout=TIMEOUT,
            follow_redirects=True,
        )
        return {
            "ok": True,
            "status_code": resp.status_code,
            "body": resp.text,
            "content_length": len(resp.content),      # page size in bytes
            "final_url": str(resp.url),                # where we ended up after redirects
            "redirect_count": len(resp.history),       # how many hops
            "failure_type": None,
        }
    except httpx.TimeoutException:
        # Server didn't respond in time — could be overloaded or slow
        return {"ok": False, "failure_type": "timeout", "status_code": None, "body": "", "content_length": 0, "final_url": None, "redirect_count": 0}
    except httpx.ConnectError:
        # Couldn't even open a TCP connection — DNS failure, host down, port closed
        return {"ok": False, "failure_type": "connection_error", "status_code": None, "body": "", "content_length": 0, "final_url": None, "redirect_count": 0}
    except httpx.TooManyRedirects:
        # Redirect loop — the page never settles on a final URL
        return {"ok": False, "failure_type": "redirect_loop", "status_code": None, "body": "", "content_length": 0, "final_url": None, "redirect_count": 0}
    except httpx.HTTPError as e:
        # Catch-all for anything else (protocol errors, etc.)
        return {"ok": False, "failure_type": f"http_error:{type(e).__name__}", "status_code": None, "body": "", "content_length": 0, "final_url": None, "redirect_count": 0}


# ---------------------------------------------------------------------------
# Helpers for content & engagement facts
# ---------------------------------------------------------------------------

IGNORE_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico",
    ".pdf", ".zip", ".tar", ".gz", ".rar", ".7z",
    ".css", ".js", ".mjs", ".xml", ".json",
    ".mp3", ".mp4", ".wav", ".avi", ".mov", ".webm",
    ".woff", ".woff2", ".ttf", ".eot",
}


def compute_readability(text: str) -> dict:
    """Compute word count and deterministic Flesch reading ease score without external deps."""
    words = re.findall(r"\b[a-zA-Z0-9_\']+\b", text)
    word_count = len(words)
    if word_count == 0:
        return {"word_count": 0, "sentence_count": 0, "reading_ease_score": 100.0}

    sentences = re.split(r"[.!?]+", text)
    sentences = [s.strip() for s in sentences if s.strip()]
    sentence_count = max(len(sentences), 1)

    def count_syllables(w: str) -> int:
        w = w.lower()
        count = len(re.findall(r"[aeiouy]+", w))
        if w.endswith("e") and not w.endswith("le") and len(w) > 2:
            count = max(1, count - 1)
        return max(1, count)

    total_syllables = sum(count_syllables(w) for w in words)
    score = 206.835 - 1.015 * (word_count / sentence_count) - 84.6 * (total_syllables / word_count)
    score = round(max(0.0, min(100.0, score)), 1)

    return {
        "word_count": word_count,
        "sentence_count": sentence_count,
        "reading_ease_score": score,
    }


def internal_links(base_url: str, html: str) -> list:
    tree = HTMLParser(html)
    seen, links = set(), []
    parsed_base = urlparse(base_url)
    for a in tree.css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
            continue
        full = urljoin(base_url, href)
        parsed = urlparse(full)
        if parsed.scheme not in ("http", "https"):
            continue
        if parsed.netloc != parsed_base.netloc:
            continue
        path_lower = parsed.path.lower()
        if any(path_lower.endswith(ext) for ext in IGNORE_EXTENSIONS):
            continue
        clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        if parsed.query:
            clean_url += f"?{parsed.query}"
        if clean_url not in seen:
            seen.add(clean_url)
            links.append(clean_url)
    return links


# ---------------------------------------------------------------------------
# Engagement helpers (measure only — engagement_audit judges)
# ---------------------------------------------------------------------------

def _norm_url(url: str) -> str:
    """
    Canonical form for link-graph comparison.

    Without this, /about, /about/, and /about?utm_source=x are three different
    nodes, and every page looks like an orphan. Fragment and query are dropped
    and the trailing slash normalised; nothing else is touched.
    """
    if not url:
        return ""
    parsed = urlparse(url.split("#")[0])
    path = parsed.path.rstrip("/") or "/"
    return f"{parsed.scheme}://{parsed.netloc}{path}".lower()


def heading_outline(tree) -> list:
    """
    The page's headings in document order, with their levels.

    Engagement-audit reads this three ways: is there an h1 at all, does the
    first heading start at level 1, and does any level get skipped (h1 -> h3),
    which means the page is using heading tags for size rather than structure.
    We return the raw sequence and let it decide.
    """
    out = []
    for node in tree.css("h1, h2, h3, h4, h5, h6"):
        text = node.text(strip=True)
        if not text:
            continue
        out.append({
            "level": int(node.tag[1]),
            "text": text[:HEADING_TEXT_CAP],
        })
    return out


def heading_shape(outline: list) -> dict:
    """
    Cheap derived numbers so every downstream rule is not re-walking the list.
    Still no judgment: max_skip is a number, not a verdict.
    """
    if not outline:
        return {"count": 0, "h1_count": 0, "starts_at_level": None,
                "max_skip": 0, "levels_used": []}

    levels = [h["level"] for h in outline]
    max_skip = 0
    for prev, cur in zip(levels, levels[1:]):
        if cur > prev:
            max_skip = max(max_skip, cur - prev)

    return {
        "count": len(outline),
        "h1_count": levels.count(1),
        "starts_at_level": levels[0],
        "max_skip": max_skip,                 # 1 is normal, >=2 is a skipped level
        "levels_used": sorted(set(levels)),
    }


def extract_main_tree(html: str):
    """
    A second parse with the chrome removed.

    We re-parse rather than decompose the original because analyze_html still
    needs the full tree afterwards, and selectolax mutation is destructive.
    One extra parse per page is microseconds; a corrupted tree is a bug hunt.

    Strategy: prefer an explicit <main>/<article> container if the site has
    one. Otherwise take <body> and delete everything that looks like chrome.
    """
    tree = HTMLParser(html)
    for tag in tree.css("script, style, noscript"):
        tag.decompose()

    for selector in MAIN_SELECTORS:
        node = tree.css_first(selector)
        if node and len(node.text(strip=True) or "") > 200:
            return node, selector

    body = tree.body
    if body is None:
        return None, None
    for tag in body.css(BOILERPLATE_SELECTOR):
        tag.decompose()
    return body, "body_minus_chrome"


def classify_links(base_url: str, tree, main_node) -> dict:
    """
    Split a page's anchors four ways: internal vs external, and main-content
    vs chrome.

    The main-content split is the one that matters. A page whose only internal
    links are the site menu is a dead end — the visitor has nowhere to go that
    relates to what they just read. Counting total links hides that completely,
    because the menu inflates every page to the same number.
    """
    host = urlparse(base_url).netloc
    internal_all, external_all = set(), set()
    internal_main = set()

    # Compared as resolved URLs, so "/about" and "https://site/about" match.
    main_urls = set()
    if main_node is not None:
        for a in main_node.css("a[href]"):
            main_urls.add(urljoin(base_url, a.attributes.get("href") or "").split("#")[0])

    for a in tree.css("a[href]"):
        href = a.attributes.get("href") or ""
        full = urljoin(base_url, href).split("#")[0]
        if not full.startswith(("http://", "https://")):
            continue
        if urlparse(full).netloc == host:
            internal_all.add(full)
            if full in main_urls:
                internal_main.add(full)
        else:
            external_all.add(full)

    return {
        "internal_out": sorted(internal_all)[:LINK_SAMPLE_CAP],
        "internal_out_count": len(internal_all),
        "internal_in_main_count": len(internal_main),
        "external_out_count": len(external_all),
        "external_domains": sorted({urlparse(u).netloc for u in external_all})[:15],
    }


def count_ctas(main_node, tree) -> dict:
    """
    How many ways forward does this page offer?

    Counted inside main content first, because a "Contact" item in the top nav
    is navigation, not this page's call to action. A page can have twenty
    header links and still give the visitor nothing to do.
    """
    scope = main_node if main_node is not None else tree

    buttons = len(scope.css("button"))
    forms = len(tree.css("form"))            # forms often sit outside <main>
    submits = len(scope.css('input[type="submit"], input[type="button"]'))

    action_links = 0
    styled_links = 0
    samples = []
    for a in scope.css("a[href]"):
        text = " ".join((a.text(strip=True) or "").lower().split())
        classes = (a.attributes.get("class") or "").lower()
        is_action = text in ACTION_PHRASES
        is_styled = any(h in classes for h in CTA_CLASS_HINTS)
        if is_action:
            action_links += 1
        if is_styled:
            styled_links += 1
        if (is_action or is_styled) and len(samples) < 5 and text:
            samples.append(text[:60])

    return {
        "button_count": buttons,
        "form_count": forms,
        "submit_input_count": submits,
        "action_link_count": action_links,
        "styled_cta_link_count": styled_links,
        "total_cta_signals": buttons + forms + submits + action_links + styled_links,
        "cta_samples": samples,
    }


def orientation_signals(tree) -> dict:
    """
    Would a visitor dropped here cold know where they are?

    Breadcrumbs are the single clearest answer to that, which is exactly why
    they matter more for AI-referred traffic than for search traffic — an
    assistant links to the deep page, never the homepage.
    """
    crumb = tree.css_first(BREADCRUMB_SELECTOR)
    crumb_items = 0
    if crumb is not None:
        crumb_items = len(crumb.css("li")) or len(crumb.css("a"))

    html_node = tree.css_first("html")
    lang = (html_node.attributes.get("lang") if html_node else None) or None

    viewport = tree.css_first('meta[name="viewport"]')
    desc = tree.css_first('meta[name="description"]')
    og_title = tree.css_first('meta[property="og:title"]')

    return {
        "breadcrumb_present": crumb is not None,
        "breadcrumb_item_count": crumb_items,
        "html_lang": lang,
        "viewport_present": viewport is not None,
        "viewport_content": (viewport.attributes.get("content") if viewport else None),
        "meta_description_present": desc is not None,
        "meta_description_length": len((desc.attributes.get("content") or "")) if desc else 0,
        "og_title_present": og_title is not None,
    }


def friction_signals(tree) -> dict:
    """
    Things that sit between arrival and content.

    We record presence and count only. A cookie banner that scrolls away is
    fine; one that blocks the page is not, and raw HTML cannot tell us which.
    Flagging it here would be a false positive factory — so we hand the number
    over and let engagement-audit weigh it against page weight and script size.
    """
    hits = defaultdict(int)
    for node in tree.css("div, section, dialog, aside"):
        blob = (
            (node.attributes.get("class") or "") + " " +
            (node.attributes.get("id") or "")
        ).lower()
        for hint in INTERSTITIAL_HINTS:
            if hint in blob:
                hits[hint] += 1

    fixed_overlays = 0
    for node in tree.css("[style]"):
        style = (node.attributes.get("style") or "").lower()
        if "position:fixed" in style.replace(" ", "") and "z-index" in style:
            fixed_overlays += 1

    head_scripts = tree.css("head script[src]")
    blocking = sum(
        1 for s in head_scripts
        if "async" not in s.attributes and "defer" not in s.attributes
    )

    return {
        "interstitial_hits": dict(hits),
        "interstitial_total": sum(hits.values()),
        "dialog_element_count": len(tree.css("dialog")),
        "fixed_overlay_count": fixed_overlays,
        "head_script_count": len(head_scripts),
        "render_blocking_script_count": blocking,
        "stylesheet_count": len(tree.css('link[rel="stylesheet"]')),
    }


def media_signals(tree) -> dict:
    """
    Images and video without a text equivalent.

    Dual purpose: engagement-audit reads alt coverage as an accessibility and
    orientation signal, content-extractability reads the same numbers as
    "facts locked in non-text". Collected once, used twice.
    """
    imgs = tree.css("img")
    missing_alt = 0
    empty_alt = 0
    for img in imgs:
        alt = img.attributes.get("alt")
        if alt is None:
            missing_alt += 1
        elif not alt.strip():
            empty_alt += 1  # decorative, legitimate — kept separate on purpose

    return {
        "img_count": len(imgs),
        "img_missing_alt": missing_alt,
        "img_empty_alt": empty_alt,
        "video_count": len(tree.css("video, iframe[src*='youtube'], iframe[src*='vimeo']")),
        "canvas_count": len(tree.css("canvas")),
        "svg_count": len(tree.css("svg")),
        "table_count": len(tree.css("table")),
    }


def readability_stats(text: str) -> dict:
    """
    Rough prose-density numbers over MAIN content only.

    Not a readability score — those need syllable counting and are noisy on
    marketing copy. Average sentence length and word length are enough to
    separate plain prose from a wall of clause-stacked jargon, and they are
    defensible as evidence because anyone can recompute them.
    """
    if not text:
        return {"word_count": 0, "sentence_count": 0,
                "avg_sentence_words": 0.0, "avg_word_chars": 0.0,
                "long_sentence_count": 0}

    words = re.findall(r"[A-Za-z']+", text)
    sentences = [s for s in re.split(r"[.!?]+", text) if s.strip()]
    sent_lengths = [len(re.findall(r"[A-Za-z']+", s)) for s in sentences]

    return {
        "word_count": len(words),
        "sentence_count": len(sentences),
        "avg_sentence_words": round(sum(sent_lengths) / len(sent_lengths), 1) if sent_lengths else 0.0,
        "avg_word_chars": round(sum(len(w) for w in words) / len(words), 1) if words else 0.0,
        "long_sentence_count": sum(1 for n in sent_lengths if n > 30),
    }


def empty_engagement() -> dict:
    """
    Keys in lockstep with the engagement block above, for pages we could not
    read. Same reasoning as your existing empty_page_info comment: a missing
    key here is a KeyError two skills later.
    """
    return {
        "main_content_source": None,
        "main_text_length": 0,
        "main_text_snippet": "",
        "boilerplate_ratio": 0.0,
        "heading_outline": [],
        "heading_shape": heading_shape([]),
        "cta": {"button_count": 0, "form_count": 0, "submit_input_count": 0,
                "action_link_count": 0, "styled_cta_link_count": 0,
                "total_cta_signals": 0, "cta_samples": []},
        "orientation": {"breadcrumb_present": False, "breadcrumb_item_count": 0,
                        "html_lang": None, "viewport_present": False,
                        "viewport_content": None, "meta_description_present": False,
                        "meta_description_length": 0, "og_title_present": False},
        "friction": {"interstitial_hits": {}, "interstitial_total": 0,
                     "dialog_element_count": 0, "fixed_overlay_count": 0,
                     "head_script_count": 0, "render_blocking_script_count": 0,
                     "stylesheet_count": 0},
        "media": {"img_count": 0, "img_missing_alt": 0, "img_empty_alt": 0,
                  "video_count": 0, "canvas_count": 0, "svg_count": 0,
                  "table_count": 0},
        "readability": readability_stats(""),
    }


def empty_links() -> dict:
    return {"internal_out": [], "internal_out_count": 0,
            "internal_in_main_count": 0, "external_out_count": 0,
            "external_domains": []}


def build_link_graph(pages_checked: list, site_url: str, exhaustive: bool = False) -> dict:
    """
    Invert outbound links into inbound counts, and measure click depth.

    WHY THIS MATTERS FOR ENGAGEMENT
    A page nothing links to is a page no visitor reaches by browsing. It can
    still be linked by an assistant, which means it is exactly the page a cold
    visitor lands on — and exactly the page most likely to be a dead end.

    HONESTY CONSTRAINT
    Inbound counts are computed over the SAMPLED pages only. A page with zero
    inbound links inside a 12-page sample of a 400-page site is not an orphan;
    it is unmeasured. Every number below is therefore published alongside
    `scope`, and engagement-audit must not claim orphan status when
    `scope.is_partial` is true. Same discipline the recommendation engine
    already applies to absence claims. `exhaustive` is only True when the
    sample covers every URL the published sitemap lists.
    """
    by_norm = {}
    for p in pages_checked:
        by_norm[_norm_url(p["requested_url"])] = p

    inbound = defaultdict(set)
    edges = 0
    for p in pages_checked:
        src = _norm_url(p["requested_url"])
        for dst in (p.get("links") or {}).get("internal_out", []):
            ndst = _norm_url(dst)
            if ndst and ndst != src:
                inbound[ndst].add(src)
                edges += 1

    # Click depth from the homepage, BFS over the sampled graph only.
    home = _norm_url(site_url)
    depth = {home: 0}
    queue = deque([home])
    while queue:
        cur = queue.popleft()
        page = by_norm.get(cur)
        if not page:
            continue
        for dst in (page.get("links") or {}).get("internal_out", []):
            ndst = _norm_url(dst)
            if ndst in by_norm and ndst not in depth:
                depth[ndst] = depth[cur] + 1
                queue.append(ndst)

    nodes = []
    for p in pages_checked:
        norm = _norm_url(p["requested_url"])
        srcs = inbound.get(norm, set())
        nodes.append({
            "url": p["requested_url"],
            "inbound_count_in_sample": len(srcs),
            "inbound_samples": sorted(srcs)[:5],
            "outbound_internal_count": (p.get("links") or {}).get("internal_out_count", 0),
            "outbound_in_main_count": (p.get("links") or {}).get("internal_in_main_count", 0),
            "click_depth_from_home": depth.get(norm),   # None = not reachable in sample
        })

    sampled = len(pages_checked)
    return {
        "nodes": nodes,
        "edge_count": edges,
        "scope": {
            "basis": "full_sitemap" if exhaustive else "sampled_pages_only",
            "pages_in_graph": sampled,
            "is_partial": not exhaustive,
            "statement": (
                f"inbound counts computed across the {sampled} sampled pages only; "
                "a zero here means unmeasured, not orphaned, unless the crawl "
                "exhausted the site's URL inventory"
            ),
        },
    }


# ---------------------------------------------------------------------------
# HTML analysis
# ---------------------------------------------------------------------------

def analyze_html(html: str, base_url: str = "") -> dict:
    """Pull out the facts each finding needs from raw HTML."""
    tree = HTMLParser(html)

    # Count script bytes before stripping
    script_bytes = sum(len(s.text() or "") for s in tree.css("script"))

    # Extract JSON-LD blocks (the raw text, not just a boolean)
    jsonld_blocks = []
    for tag in tree.css('script[type="application/ld+json"]'):
        text = tag.text(strip=True)
        if text:
            jsonld_blocks.append(text)

    # Feature 7: Check if an <h1> tag exists in raw HTML
    h1_tag = tree.css_first("h1")
    h1_present = h1_tag is not None
    h1_text = h1_tag.text(strip=True) if h1_tag else None

    # H2 subheadings count
    h2_tags = tree.css("h2")
    h2_count = len(h2_tags)

    # CTA elements (buttons, inputs, button-styled links)
    cta_selectors = ["button", "input[type='button']", "input[type='submit']", "a[class*='btn']", "a[class*='cta']"]
    cta_nodes = set()
    for sel in cta_selectors:
        for node in tree.css(sel):
            cta_nodes.add(node)
    cta_count = len(cta_nodes)

    # Meta robots tag (noindex detection — added as a cheap bonus)
    meta_robots = None
    meta_tag = tree.css_first('meta[name="robots"]')
    if meta_tag:
        meta_robots = meta_tag.attributes.get("content", "")

    # Canonical URL
    canonical = None
    canon_tag = tree.css_first('link[rel="canonical"]')
    if canon_tag:
        canonical = canon_tag.attributes.get("href", "")

    # Outbound internal links from this page
    page_internal_links = internal_links(base_url, html) if base_url else []

    # Engagement facts need the intact tree, so gather them before stripping.
    main_node, main_source = extract_main_tree(html)
    main_text = main_node.text(separator=" ", strip=True) if main_node else ""
    outline = heading_outline(tree)
    links = classify_links(base_url, tree, main_node) if base_url else empty_links()
    engagement = {
        "main_content_source": main_source,
        "main_text_length": len(main_text),
        "main_text_snippet": main_text[:MAIN_SNIPPET_CHARS],
        "boilerplate_ratio": 0.0,   # filled below, once visible_text exists
        "heading_outline": outline,
        "heading_shape": heading_shape(outline),
        "cta": count_ctas(main_node, tree),
        "orientation": orientation_signals(tree),
        "friction": friction_signals(tree),
        "media": media_signals(tree),
        "readability": readability_stats(main_text),
    }

    # Strip scripts/styles before measuring visible text
    for tag in tree.css("script, style"):
        tag.decompose()
    visible_text = tree.body.text(separator=" ", strip=True) if tree.body else ""
    if visible_text:
        engagement["boilerplate_ratio"] = round(max(0.0, 1 - len(main_text) / len(visible_text)), 3)

    title = tree.css_first("title")

    readability = compute_readability(visible_text)

    return {
        "visible_text_length": len(visible_text),
        "visible_text_snippet": visible_text[:200],   # first 200 chars for quick inspection
        "script_size": script_bytes,
        "title": title.text(strip=True) if title else "",
        "h1_present": h1_present,                      # Feature 7
        "h1_text": h1_text,                            # Feature 7: the actual heading
        "h2_count": h2_count,
        "cta_count": cta_count,
        "has_structured_data": len(jsonld_blocks) > 0,
        "jsonld_block_count": len(jsonld_blocks),
        "jsonld_raw": jsonld_blocks,                   # full blocks for the analyzer
        "meta_robots": meta_robots,
        "canonical_url": canonical,
        "word_count": readability["word_count"],
        "reading_ease_score": readability["reading_ease_score"],
        "outbound_internal_links_count": len(page_internal_links),
        "outbound_internal_urls": page_internal_links,
        # Raw counts for recommendation's quotable-facts density rule
        "text_stats": {
            "char_count": len(visible_text),
            "digit_count": sum(c.isdigit() for c in visible_text),
        },
        "engagement": engagement,
        "links": links,
    }


def empty_page_info() -> dict:
    """
    Same keys as analyze_html(), for pages we could not read. A missing key
    here is a KeyError two skills later, so keep the two in lockstep.
    """
    return {
        "visible_text_length": 0, "visible_text_snippet": "", "script_size": 0, "title": "",
        "h1_present": False, "h1_text": None, "h2_count": 0, "cta_count": 0,
        "has_structured_data": False, "jsonld_block_count": 0, "jsonld_raw": [],
        "meta_robots": None, "canonical_url": None,
        "word_count": 0, "reading_ease_score": 0.0,
        "outbound_internal_links_count": 0, "outbound_internal_urls": [],
        "text_stats": {"char_count": 0, "digit_count": 0},
        "engagement": empty_engagement(),
        "links": empty_links(),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_robots(text: str, agents: list) -> dict:
    """Tiny robots.txt reader: 'allowed'/'blocked' per agent."""
    rules = defaultdict(list)
    current = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, _, value = line.partition(":")
        field, value = field.strip().lower(), value.strip()
        if field == "user-agent":
            current = [value]
        elif field == "disallow" and value:
            for agent in current:
                rules[agent].append(value)

    result = {}
    for agent in agents:
        blocked = rules.get(agent) or rules.get("*", [])
        result[agent] = "blocked" if "/" in blocked else "allowed"
    return result


def robots_sitemaps(text: str) -> list:
    """Every `Sitemap:` line in robots.txt (they apply regardless of user-agent group)."""
    out = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        field, _, value = line.partition(":")
        if field.strip().lower() == "sitemap" and value.strip():
            out.append(value.strip())
    return out


def sitemap_locs(xml: str) -> tuple:
    """Return (is_index, [loc, ...]) from a sitemap or sitemap-index body."""
    locs = [html_unescape(m).strip() for m in re.findall(r"<loc>\s*(.*?)\s*</loc>", xml, re.S | re.I)]
    return bool(re.search(r"<sitemapindex\b", xml, re.I)), locs


def html_unescape(s: str) -> str:
    return s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")


async def discover_sitemap(client: httpx.AsyncClient, site_url: str, declared: list) -> dict:
    """
    Find the sitemap via robots.txt, falling back to /sitemap.xml, and count
    its URLs. Follows up to SITEMAP_CHILD_LIMIT children of a sitemap index.
    """
    candidates = declared or [urljoin(site_url, "/sitemap.xml")]
    for sm_url in candidates:
        resp = await fetch(client, sm_url, OUR_UA)
        if not (resp["ok"] and resp["status_code"] == 200 and "<loc" in resp["body"].lower()):
            continue
        is_index, locs = sitemap_locs(resp["body"])
        urls = locs
        if is_index:
            urls = []
            for child in locs[:SITEMAP_CHILD_LIMIT]:
                child_resp = await fetch(client, child, OUR_UA)
                if child_resp["ok"] and child_resp["status_code"] == 200:
                    urls.extend(sitemap_locs(child_resp["body"])[1])
        return {
            "found": True,
            "source": "robots_txt" if declared else "default_path",
            "sitemap_url": sm_url,
            "is_index": is_index,
            "children_followed": min(len(locs), SITEMAP_CHILD_LIMIT) if is_index else 0,
            "children_total": len(locs) if is_index else 0,
            "url_count": len(urls),
            "urls": urls[:SITEMAP_URL_CAP],
        }
    return {"found": False, "source": None, "sitemap_url": None, "is_index": False,
            "children_followed": 0, "children_total": 0, "url_count": 0, "urls": []}


async def check_llms_txt(client: httpx.AsyncClient, site_url: str) -> dict:
    """Is /llms.txt served as a text file (not an HTML soft-404)?"""
    url = urljoin(site_url, "/llms.txt")
    resp = await fetch(client, url, OUR_UA)
    body = resp["body"] if resp["ok"] and resp["status_code"] == 200 else ""
    looks_html = body.lstrip()[:15].lower().startswith(("<!doctype", "<html"))
    found = bool(body.strip()) and not looks_html
    return {
        "found": found,
        "url": url,
        "status_code": resp["status_code"],
        "size_bytes": len(body.encode()) if found else 0,
    }


def page_type(url: str) -> str:
    path = urlparse(url).path.strip("/")
    return "homepage" if not path else "/" + path.split("/")[0] + "/"


# ---------------------------------------------------------------------------
# Feature 1: Render-gap check using Playwright
# ---------------------------------------------------------------------------
# WHY THIS EXISTS:
# Many modern sites (React SPAs, Next.js client-rendered, Vue apps) serve
# a near-empty HTML shell to the browser. The real content only appears
# after JavaScript runs. A simple HTTP GET (which is what most AI crawlers
# do) sees the empty shell — so the product name, price, description are
# all invisible to the AI even though they look fine to a human.
#
# HOW IT WORKS:
# 1. We already have the raw HTML from httpx (the "before JS" version)
# 2. We launch a headless Chromium via Playwright, load the same URL,
#    wait for JS to finish, then grab the DOM (the "after JS" version)
# 3. We compare: text length, title, h1, JSON-LD block count
# 4. The ratio (rendered - raw) / rendered = text_delta_ratio
#    - Near 0.0 → server-rendered, safe
#    - Above 0.7 → most content is JS-only, critical problem
#
# WHY SAMPLING:
# Playwright launches a real browser — each page takes 2-4 seconds.
# With a 5-minute audit budget and 12+ pages, we can't render everything.
# But rendering strategy is a SITE-LEVEL property (all product pages use
# the same template), so we pick one page per template type and generalize.
#
# GRACEFUL DEGRADATION:
# If Playwright isn't installed, we catch the ImportError, set
# render_check.status = "unavailable", and the rest of the audit
# continues without it — the skill never crashes.

async def render_page(browser, url: str) -> dict | None:
    """Load one URL in headless Chromium and extract the rendered DOM."""
    try:
        page = await browser.new_page()
        await page.goto(url, wait_until="networkidle", timeout=15000)
        # Extra wait for late-loading JS frameworks
        await page.wait_for_timeout(RENDER_WAIT_MS)

        # Get the fully rendered HTML
        rendered_html = await page.content()
        await page.close()

        # Analyze the rendered version the same way we analyze raw HTML
        info = analyze_html(rendered_html)
        return info
    except Exception as e:
        return None


def pick_render_targets(pages_checked: list) -> list:
    """
    Choose which pages to render with Playwright.
    Strategy: one page per unique page_type, prioritizing pages with
    suspiciously thin raw text (< THIN_TEXT_THRESHOLD chars), capped
    at RENDER_SAMPLE_LIMIT.
    """
    # Group pages by type
    by_type = defaultdict(list)
    for p in pages_checked:
        if p.get("status_code") == 200:
            by_type[p["type"]].append(p)

    targets = []
    # First pass: pick thin pages (most likely to have a render gap)
    for ptype, pages in by_type.items():
        thin = [p for p in pages if p["visible_text_length"] < THIN_TEXT_THRESHOLD]
        if thin:
            targets.append(thin[0])
        elif pages:
            targets.append(pages[0])

    # Cap at limit
    return targets[:RENDER_SAMPLE_LIMIT]


async def render_gap_check(pages_checked: list) -> dict:
    """
    Run Playwright on a sample of pages and compare raw vs rendered.
    Returns the full render_check section of the evidence file.
    """
    # Graceful degradation: if Playwright is not installed, say so and move on
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {
            "status": "unavailable",
            "reason": "playwright not installed — run: pip install playwright && playwright install chromium",
            "pages": [],
        }

    targets = pick_render_targets(pages_checked)
    if not targets:
        return {"status": "skipped", "reason": "no eligible pages to render", "pages": []}

    results = []

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)

            for page_data in targets:
                url = page_data["requested_url"]
                rendered = await render_page(browser, url)

                if rendered is None:
                    results.append({
                        "url": url,
                        "page_type": page_data["type"],
                        "render_failed": True,
                    })
                    continue

                raw_len = page_data["visible_text_length"]
                rendered_len = rendered["visible_text_length"]

                # text_delta_ratio: what fraction of the rendered text is MISSING from the raw HTML
                # Formula: (rendered - raw) / rendered
                # 0.0 = everything was already in raw HTML (good)
                # 0.93 = 93% of content only appears after JS (bad)
                if rendered_len > 0:
                    delta = round((rendered_len - raw_len) / rendered_len, 2)
                else:
                    delta = 0.0

                results.append({
                    "url": url,
                    "page_type": page_data["type"],
                    "raw_text_len": raw_len,
                    "rendered_text_len": rendered_len,
                    "text_delta_ratio": max(delta, 0.0),  # clamp: raw can be bigger due to noscript fallback
                    "raw_title": page_data["title"],
                    "rendered_title": rendered["title"],
                    "h1_present_raw": page_data["h1_present"],
                    "h1_present_rendered": rendered["h1_present"],
                    "jsonld_blocks_raw": page_data["jsonld_block_count"],
                    "jsonld_blocks_rendered": rendered["jsonld_block_count"],
                    "main_content_source": "client_fetch" if delta > 0.5 else "server",
                    "noscript_fallback_len": 0,  # TODO: extract <noscript> content length
                })

            await browser.close()
    except Exception as e:
        return {"status": "error", "reason": str(e), "pages": results}

    # Check if pages of the same type disagree on delta (means inconsistent rendering)
    type_deltas = defaultdict(list)
    for r in results:
        if "text_delta_ratio" in r:
            type_deltas[r["page_type"]].append(r["text_delta_ratio"])
    inconsistent = any(
        max(deltas) - min(deltas) > 0.3
        for deltas in type_deltas.values()
        if len(deltas) > 1
    )

    return {
        "status": "completed",
        "render_sampled": True,
        "pages_rendered": [r["url"] for r in results if not r.get("render_failed")],
        "pages": results,
        "template_inconsistent": inconsistent,
    }


# ---------------------------------------------------------------------------
# Main crawl
# ---------------------------------------------------------------------------

async def crawl(site_url: str) -> dict:
    async with httpx.AsyncClient() as client:

        # ---- 1. robots.txt ----
        robots_result = await fetch(client, urljoin(site_url, "/robots.txt"), OUR_UA)
        robots_text = robots_result["body"] if robots_result["ok"] and robots_result["status_code"] == 200 else ""
        rules = parse_robots(robots_text, ROBOTS_AGENTS) if robots_text else {}

        # ---- 2. UA probe on homepage — Feature 2: now 4 identities, not 2 ----
        # OLD CODE: only compared Chrome vs GPTBot
        # NEW CODE: we probe Chrome + every agent in PROBE_AGENTS
        #
        # WHY: A WAF (web application firewall) might allow GPTBot but block
        #       PerplexityBot, or vice versa. Testing only one bot and assuming
        #       the others are treated the same is a false-negative factory.
        #       Each probe costs one HTTP request — cheap.

        ua_results = []

        # First: the Chrome baseline (what a human sees)
        chrome = await fetch(client, site_url, BROWSER_UA)
        chrome_info = analyze_html(chrome["body"], site_url) if chrome["ok"] and chrome["status_code"] == 200 else None
        ua_results.append({
            "agent": "chrome_baseline",
            "status_code": chrome["status_code"],
            "page_size_bytes": chrome["content_length"],
            "text_length": chrome_info["visible_text_length"] if chrome_info else 0,
            "final_url": chrome["final_url"],
            "failure_type": chrome["failure_type"],
        })

        # Then: each AI bot
        for agent_name in PROBE_AGENTS:
            resp = await fetch(client, site_url, agent_name)
            info = analyze_html(resp["body"]) if resp["ok"] and resp["status_code"] == 200 else None
            ua_results.append({
                "agent": agent_name,
                "status_code": resp["status_code"],
                "page_size_bytes": resp["content_length"],
                "text_length": info["visible_text_length"] if info else 0,
                "final_url": resp["final_url"],
                "failure_type": resp["failure_type"],
            })

        # ---- 3. Sample pages: the homepage itself, then its internal links ----
        # The homepage is included so the link graph has a root for click depth
        # and engagement_audit can weigh homepage defects as high-importance.
        pages_checked = []
        discovered = []
        if chrome["ok"] and chrome["status_code"] == 200:
            home_norm = _norm_url(site_url)
            discovered = [u for u in internal_links(site_url, chrome["body"])
                          if _norm_url(u) != home_norm]
            for url in [site_url] + discovered[:MAX_PAGES - 1]:
                resp = await fetch(client, url, OUR_UA)
                await asyncio.sleep(DELAY_SECONDS)

                if resp["ok"] and resp["status_code"] == 200:
                    info = analyze_html(resp["body"], url)
                else:
                    info = empty_page_info()

                pages_checked.append({
                    "requested_url": url,
                    "final_url": resp["final_url"],
                    "redirect_count": resp["redirect_count"],
                    "status_code": resp["status_code"],
                    "failure_type": resp["failure_type"],       # Feature 5
                    "type": page_type(url),
                    "visible_text_length": info["visible_text_length"],
                    "script_size": info["script_size"],
                    "page_size_bytes": resp["content_length"],
                    "title": info["title"],
                    "h1_present": info["h1_present"],           # Feature 7
                    "h1_text": info["h1_text"],                 # Feature 7
                    "h2_count": info["h2_count"],
                    "cta_count": info["cta_count"],
                    "word_count": info["word_count"],
                    "reading_ease_score": info["reading_ease_score"],
                    "outbound_internal_links_count": info["outbound_internal_links_count"],
                    "outbound_internal_urls": info["outbound_internal_urls"],
                    "has_structured_data": info["has_structured_data"],
                    "jsonld_block_count": info["jsonld_block_count"],
                    "jsonld_raw": info["jsonld_raw"],
                    "meta_robots": info["meta_robots"],
                    "canonical_url": info["canonical_url"],
                    "text_stats": info["text_stats"],
                    "engagement": info["engagement"],
                    "links": info["links"],
                })

        # ---- 4. Site-level files: sitemap and llms.txt ----
        declared_sitemaps = robots_sitemaps(robots_text) if robots_text else []
        sitemap = await discover_sitemap(client, site_url, declared_sitemaps)
        llms_txt = await check_llms_txt(client, site_url)

        # ---- 5. Link graph — only claims full coverage if every sitemap URL was sampled ----
        sampled_norm = {_norm_url(p["requested_url"]) for p in pages_checked}
        exhaustive = (
            sitemap["found"]
            and 0 < sitemap["url_count"] <= SITEMAP_URL_CAP
            and all(_norm_url(u) in sampled_norm for u in sitemap["urls"])
        )
        link_graph = build_link_graph(pages_checked, site_url, exhaustive)

        # ---- 6. Render-gap check — Feature 1 ----
        render_result = await render_gap_check(pages_checked)

        # ---- 7. Assemble evidence file ----
        succeeded = sum(1 for p in pages_checked if p["status_code"] == 200)
        failed = len(pages_checked) - succeeded

        return {
            "site": site_url,
            "crawled_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "our_agent": OUR_UA,

            "robots_txt": {
                "found": bool(robots_text),
                "rules_by_agent": rules,
                "sitemap_urls": declared_sitemaps,
            },

            "ua_probe": {
                "url_tested": site_url,
                "results": ua_results,
            },

            "sitemap": sitemap,
            "llms_txt": llms_txt,

            "pages_checked": pages_checked,

            "link_graph": link_graph,

            "render_check": render_result,

            "coverage": {
                "pages_sampled": len(pages_checked),
                # homepage + every distinct internal link found on it
                "links_discovered": len(discovered) + 1 if pages_checked else 0,
            },

            "crawl_summary": {
                "pages_attempted": len(pages_checked),
                "pages_succeeded": succeeded,
                "pages_failed": failed,
                "pages_rendered": len(render_result.get("pages", [])),
            },
        }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print("Usage: python crawl.py https://example.com")
        sys.exit(1)
    result = asyncio.run(crawl(sys.argv[1]))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()