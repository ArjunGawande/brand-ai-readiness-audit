#!/usr/bin/env python3
"""Convenience root wrapper for the site crawler."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CRAWLER = ROOT / "skills" / "crawl-render-audit" / "scripts" / "crawl.py"

if __name__ == "__main__":
    cmd = [sys.executable, str(CRAWLER)] + sys.argv[1:]
    result = subprocess.run(cmd)
    sys.exit(result.returncode)
