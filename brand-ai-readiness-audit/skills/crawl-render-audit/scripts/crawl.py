#!/usr/bin/env python3
"""
ENGAGEMENT ADDITIONS FOR crawl.py
=================================

Everything here is new. Paste each block into crawl.py at the marked location.
No existing behaviour changes except where explicitly noted.

Design rule kept from your existing code: the crawler MEASURES, it does not
JUDGE. Nothing below returns a boolean like "bad_hierarchy" or "no_cta" — it
returns counts and lists, and engagement-audit decides what they mean.

Why these fields and not others: an AI assistant drops a visitor onto a DEEP
page, not the homepage. So every check below asks the same question — if I
landed on this page alone, with no memory of the site, would I know where I
am, what this is, and what to do next?

MERGE ORDER
  1. Constants          -> top of file, after SNIPPET_CHARS
  2. Helper functions   -> above analyze_html()
  3. analyze_html()     -> insert the engagement block, return more keys
  4. empty_page_info()  -> add matching keys (keeps your lockstep discipline)
  5. crawl() loop       -> capture per-page outbound links
  6. crawl() post-pass  -> build the link graph
  7. Evidence file      -> add link_graph section
"""

import re
from collections import defaultdict, deque
from urllib.parse import urljoin, urlparse

from selectolax.parser import HTMLParser


# ===========================================================================
# 1. CONSTANTS  ->  paste after SNIPPET_CHARS in crawl.py
# ===========================================================================

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


# ===========================================================================
# 2. HELPERS  ->  paste above analyze_html() in crawl.py
# ===========================================================================

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

    main_hrefs = set()
    if main_node is not None:
        for a in main_node.css("a[href]"):
            main_hrefs.add(a.attributes.get("href") or "")

    for a in tree.css("a[href]"):
        href = a.attributes.get("href") or ""
        full = urljoin(base_url, href).split("#")[0]
        if not full.startswith(("http://", "https://")):
            continue
        if urlparse(full).netloc == host:
            internal_all.add(full)
            if href in main_hrefs:
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


# ===========================================================================
# 3. analyze_html()  ->  insert this block, then extend the return dict
# ===========================================================================
#
# Insert AFTER the canonical extraction and BEFORE the
# `for tag in tree.css("script, style"): tag.decompose()` line — the
# engagement helpers need the intact tree.
#
#     main_node, main_source = extract_main_tree(html)
#     main_text = main_node.text(separator=" ", strip=True) if main_node else ""
#     outline = heading_outline(tree)
#
#     engagement = {
#         "main_content_source": main_source,
#         "main_text_length": len(main_text),
#         "main_text_snippet": main_text[:MAIN_SNIPPET_CHARS],
#         "boilerplate_ratio": round(1 - len(main_text) / len(visible_text), 3)
#                              if visible_text else 0.0,
#         "heading_outline": outline,
#         "heading_shape": heading_shape(outline),
#         "cta": count_ctas(main_node, tree),
#         "orientation": orientation_signals(tree),
#         "friction": friction_signals(tree),
#         "media": media_signals(tree),
#         "readability": readability_stats(main_text),
#     }
#
# NOTE ON ORDER: visible_text is computed further down in your current code.
# Either move the visible_text computation above this block, or compute
# boilerplate_ratio after it. The ratio is the only line that depends on it.
#
# Then add to the return dict:
#
#     "engagement": engagement,
#     "links": None,   # filled in by the crawl loop, which knows the base URL
#
# links is deliberately not computed here: classify_links needs the page's
# own URL to tell internal from external, and analyze_html only receives HTML.


# ===========================================================================
# 4. empty_page_info()  ->  add matching keys
# ===========================================================================

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


# ===========================================================================
# 5. crawl() PAGE LOOP  ->  capture per-page links
# ===========================================================================
#
# Your loop currently does:
#
#     if resp["ok"] and resp["status_code"] == 200:
#         info = analyze_html(resp["body"])
#     else:
#         info = empty_page_info()
#
# Change to:
#
#     if resp["ok"] and resp["status_code"] == 200:
#         info = analyze_html(resp["body"])
#         main_node, _ = extract_main_tree(resp["body"])
#         info["links"] = classify_links(url, HTMLParser(resp["body"]), main_node)
#     else:
#         info = empty_page_info()
#         info["links"] = empty_links()
#
# and add to the pages_checked dict:
#
#     "engagement": info["engagement"],
#     "links": info["links"],
#
# This is the one genuinely new fetch-time cost, and it is zero network —
# just a second parse of HTML you already hold.


# ===========================================================================
# 6. POST-PASS  ->  build the link graph after the loop
# ===========================================================================

def build_link_graph(pages_checked: list, site_url: str) -> dict:
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
    `scope.is_partial` is true. Same discipline your recommendation engine
    already applies to absence claims.
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
            "basis": "sampled_pages_only",
            "pages_in_graph": sampled,
            "is_partial": True,
            "statement": (
                f"inbound counts computed across the {sampled} sampled pages only; "
                "a zero here means unmeasured, not orphaned, unless the crawl "
                "exhausted the site's URL inventory"
            ),
        },
    }


# ===========================================================================
# 7. EVIDENCE FILE  ->  add one section
# ===========================================================================
#
# After the page loop, before the return:
#
#     link_graph = build_link_graph(pages_checked, site_url)
#
# and in the returned dict, alongside "render_check":
#
#     "link_graph": link_graph,
#
# Nothing else in the evidence file changes shape, so every skill you have
# already written keeps working untouched.


# ===========================================================================
# WHAT ENGAGEMENT-AUDIT CAN NOW ASK
# ===========================================================================
#
#  Cold-landing orientation
#    heading_shape.h1_count == 0            -> page does not say what it is
#    heading_shape.max_skip >= 2            -> headings used as sizes, not structure
#    orientation.breadcrumb_present == False AND click_depth >= 2
#                                           -> deep page with no "you are here"
#    orientation.meta_description_present    -> snippet quality on referral
#
#  Dead ends
#    links.internal_in_main_count == 0      -> nowhere to go but the menu
#    link_graph inbound_count_in_sample == 0 -> nothing points here (gated on scope)
#
#  Nothing to do
#    cta.total_cta_signals == 0             -> no next action offered anywhere
#
#  Content buried in chrome
#    engagement.boilerplate_ratio > 0.8     -> mostly menu, little page
#    main_text_length < 300                 -> thin once chrome is removed
#
#  Friction on arrival
#    friction.interstitial_total            -> banners and modals present
#    friction.render_blocking_script_count  -> slow first paint
#    script_size + page_size_bytes          -> you already collect these
#
#  Mobile
#    orientation.viewport_present == False  -> not mobile-adapted at all
#
#  Comprehension
#    readability.avg_sentence_words > 25    -> dense prose
#    readability.long_sentence_count        -> how much of it
#
#  Non-text facts (shared with content-extractability)
#    media.img_missing_alt / media.img_count
#    media.canvas_count, media.video_count
#
#
# ONE TUNING NOTE
# MAX_PAGES = 12 is fine for the machine-side checks, which are template-level
# properties. The link graph is the one thing it genuinely limits: 12 nodes
# makes almost every page look orphaned. If the render budget allows, raise
# MAX_PAGES to 25-30 before shipping, or accept that orphan findings will be
# suppressed by the scope gate on most real sites.