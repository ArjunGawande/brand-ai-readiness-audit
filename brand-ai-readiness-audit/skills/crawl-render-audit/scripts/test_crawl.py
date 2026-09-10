#!/usr/bin/env python3
"""
Unit tests for crawl-render-audit (crawl.py).
Tests extraction, robot parsing, link discovery, and schema conformance.
"""

import json
import os
import unittest
from crawl import analyze_html, parse_robots, page_type, internal_links, pick_render_targets


class TestCrawlRenderAudit(unittest.TestCase):

    def test_analyze_html_basic(self):
        sample_html = """
        <!DOCTYPE html>
        <html>
        <head>
            <title>Test Page Title</title>
            <meta name="robots" content="noindex, follow">
            <link rel="canonical" href="https://example.com/canonical-test">
            <script>var x = "some script";</script>
            <style>body { color: red; }</style>
            <script type="application/ld+json">
            {"@context": "https://schema.org", "@type": "Organization", "name": "TestCorp"}
            </script>
        </head>
        <body>
            <h1>Welcome to TestCorp</h1>
            <p>This is visible body content for testing AI discoverability.</p>
        </body>
        </html>
        """
        result = analyze_html(sample_html)

        self.assertEqual(result["title"], "Test Page Title")
        self.assertTrue(result["h1_present"])
        self.assertEqual(result["h1_text"], "Welcome to TestCorp")
        self.assertEqual(result["meta_robots"], "noindex, follow")
        self.assertEqual(result["canonical_url"], "https://example.com/canonical-test")
        self.assertTrue(result["has_structured_data"])
        self.assertEqual(result["jsonld_block_count"], 1)
        self.assertIn("TestCorp", result["jsonld_raw"][0])
        self.assertIn("Welcome to TestCorp", result["visible_text_snippet"])
        self.assertNotIn("var x", result["visible_text_snippet"])
        self.assertNotIn("color: red", result["visible_text_snippet"])
        self.assertGreater(result["script_size"], 0)
        self.assertGreater(result["visible_text_length"], 0)

    def test_analyze_html_empty_and_missing_tags(self):
        empty_html = "<html><body></body></html>"
        result = analyze_html(empty_html)

        self.assertEqual(result["title"], "")
        self.assertFalse(result["h1_present"])
        self.assertIsNone(result["h1_text"])
        self.assertIsNone(result["meta_robots"])
        self.assertIsNone(result["canonical_url"])
        self.assertFalse(result["has_structured_data"])
        self.assertEqual(result["jsonld_block_count"], 0)
        self.assertEqual(result["jsonld_raw"], [])
        self.assertEqual(result["visible_text_length"], 0)

    def test_parse_robots(self):
        robots_txt = """
        User-agent: *
        Disallow: /private/

        User-agent: GPTBot
        Disallow: /

        User-agent: ClaudeBot
        Allow: /
        """
        agents = ["GPTBot", "ClaudeBot", "PerplexityBot"]
        rules = parse_robots(robots_txt, agents)

        self.assertEqual(rules["GPTBot"], "blocked")
        self.assertEqual(rules["ClaudeBot"], "allowed")
        self.assertEqual(rules["PerplexityBot"], "allowed")

    def test_page_type(self):
        self.assertEqual(page_type("https://example.com"), "homepage")
        self.assertEqual(page_type("https://example.com/"), "homepage")
        self.assertEqual(page_type("https://example.com/products/item123"), "/products/")
        self.assertEqual(page_type("https://example.com/about-us"), "/about-us/")

    def test_internal_links(self):
        base_url = "https://example.com"
        html = """
        <html>
        <body>
            <a href="/products/1">Product 1</a>
            <a href="https://example.com/about">About</a>
            <a href="https://otherdomain.com/link">External</a>
            <a href="/products/1#section2">Product 1 Anchor</a>
            <a href="mailto:info@example.com">Mail</a>
            <a href="tel:+1234567890">Phone</a>
        </body>
        </html>
        """
        links = internal_links(base_url, html)
        self.assertIn("https://example.com/products/1", links)
        self.assertIn("https://example.com/about", links)
        self.assertNotIn("https://otherdomain.com/link", links)
        # Should deduplicate without hash
        self.assertEqual(links.count("https://example.com/products/1"), 1)

    def test_pick_render_targets(self):
        pages = [
            {"requested_url": "https://example.com/", "type": "homepage", "visible_text_length": 1500, "status_code": 200},
            {"requested_url": "https://example.com/products/1", "type": "/products/", "visible_text_length": 200, "status_code": 200},
            {"requested_url": "https://example.com/products/2", "type": "/products/", "visible_text_length": 800, "status_code": 200},
            {"requested_url": "https://example.com/about", "type": "/about/", "visible_text_length": 1200, "status_code": 200},
            {"requested_url": "https://example.com/broken", "type": "/broken/", "visible_text_length": 0, "status_code": 404},
        ]
        targets = pick_render_targets(pages)
        target_urls = [t["requested_url"] for t in targets]

        self.assertIn("https://example.com/products/1", target_urls)
        self.assertIn("https://example.com/", target_urls)
        self.assertIn("https://example.com/about", target_urls)
        self.assertNotIn("https://example.com/broken", target_urls)

    def test_evidence_schema_compatibility(self):
        fixture_path = os.path.join(os.path.dirname(__file__), "evidence.json")
        self.assertTrue(os.path.exists(fixture_path), "evidence.json fixture must exist")

        with open(fixture_path, "r", encoding="utf-8") as f:
            evidence = json.load(f)

        # Core root keys
        self.assertIn("site", evidence)
        self.assertIn("crawled_at", evidence)
        self.assertIn("our_agent", evidence)
        self.assertIn("robots_txt", evidence)
        self.assertIn("ua_probe", evidence)
        self.assertIn("pages_checked", evidence)
        self.assertIn("render_check", evidence)
        self.assertIn("crawl_summary", evidence)

        # Robots.txt structure
        self.assertIn("found", evidence["robots_txt"])
        self.assertIn("rules_by_agent", evidence["robots_txt"])

        # UA probe structure
        self.assertIn("results", evidence["ua_probe"])
        probe_agents = [r["agent"] for r in evidence["ua_probe"]["results"]]
        self.assertIn("chrome_baseline", probe_agents)
        self.assertIn("GPTBot", probe_agents)


if __name__ == "__main__":
    unittest.main()
