#!/usr/bin/env python3
"""
Unit tests for engagement-audit skill (check_engagement.py).
Tests heading structure, CTAs, dead ends, orphan pages, readability, and schema.
"""

import json
import unittest
from check_engagement import analyze, check_headings, check_calls_to_action, check_dead_ends, check_orphan_pages, check_readability


class TestEngagementAudit(unittest.TestCase):

    def test_missing_h1_and_h2(self):
        pages = [
            {
                "requested_url": "https://example.com/no-h1",
                "status_code": 200,
                "h1_present": False,
                "h1_text": None,
                "h2_count": 0,
                "word_count": 50,
            },
            {
                "requested_url": "https://example.com/long-no-h2",
                "status_code": 200,
                "h1_present": True,
                "h1_text": "Good Title",
                "h2_count": 0,
                "word_count": 400,
            },
        ]
        findings = check_headings(pages)
        titles = [f["title"] for f in findings]
        self.assertIn("Pages missing primary <h1> heading", titles)
        self.assertIn("Substantive pages lack <h2> subheadings to break up content", titles)

    def test_calls_to_action(self):
        pages = [
            {"requested_url": "https://example.com/page1", "status_code": 200, "cta_count": 0},
            {"requested_url": "https://example.com/page2", "status_code": 200, "cta_count": 0},
            {"requested_url": "https://example.com/page3", "status_code": 200, "cta_count": 1},
        ]
        findings = check_calls_to_action(pages)
        self.assertEqual(len(findings), 1)
        self.assertIn("lack clear calls-to-action", findings[0]["title"])

    def test_dead_ends(self):
        pages = [
            {"requested_url": "https://example.com/dead-end", "status_code": 200, "outbound_internal_links_count": 0},
            {"requested_url": "https://example.com/has-links", "status_code": 200, "outbound_internal_links_count": 5},
        ]
        findings = check_dead_ends(pages)
        self.assertEqual(len(findings), 1)
        self.assertIn("Dead-end pages detected", findings[0]["title"])

    def test_orphan_pages(self):
        pages = [
            {
                "requested_url": "https://example.com/",
                "final_url": "https://example.com/",
                "status_code": 200,
                "type": "homepage",
                "outbound_internal_urls": ["https://example.com/about"],
            },
            {
                "requested_url": "https://example.com/about",
                "final_url": "https://example.com/about",
                "status_code": 200,
                "type": "/about/",
                "outbound_internal_urls": ["https://example.com/"],
            },
            {
                "requested_url": "https://example.com/orphan-article",
                "final_url": "https://example.com/orphan-article",
                "status_code": 200,
                "type": "/orphan-article/",
                "outbound_internal_urls": ["https://example.com/about"],
            },
        ]
        findings = check_orphan_pages(pages, "https://example.com")
        self.assertEqual(len(findings), 1)
        self.assertIn("https://example.com/orphan-article", findings[0]["affected_pages"])

    def test_readability_jargon(self):
        pages = [
            {
                "requested_url": "https://example.com/jargon",
                "status_code": 200,
                "word_count": 150,
                "reading_ease_score": 12.5,
            },
            {
                "requested_url": "https://example.com/clear",
                "status_code": 200,
                "word_count": 150,
                "reading_ease_score": 75.0,
            },
        ]
        findings = check_readability(pages)
        self.assertEqual(len(findings), 1)
        self.assertIn("Content readability is very low", findings[0]["title"])

    def test_analyze_empty_pages(self):
        result = analyze({"site": "https://blocked.com", "pages_checked": []})
        self.assertEqual(result["skill"], "engagement-audit")
        self.assertEqual(result["summary"]["total_findings"], 1)
        self.assertIn("could not be performed", result["findings"][0]["title"])

    def test_analyze_clean_page(self):
        clean_page = {
            "requested_url": "https://example.com/",
            "final_url": "https://example.com/",
            "status_code": 200,
            "type": "homepage",
            "h1_present": True,
            "h1_text": "Welcome to Our Platform",
            "h2_count": 3,
            "cta_count": 2,
            "word_count": 120,
            "reading_ease_score": 68.0,
            "outbound_internal_links_count": 4,
            "outbound_internal_urls": ["https://example.com/about"],
        }
        result = analyze({"site": "https://example.com", "pages_checked": [clean_page]})
        self.assertEqual(result["summary"]["total_findings"], 0)
        self.assertEqual(len(result["findings"]), 0)


if __name__ == "__main__":
    unittest.main()
