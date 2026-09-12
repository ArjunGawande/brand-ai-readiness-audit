#!/usr/bin/env python3
"""Convenience root runner for the Brand AI Readiness Audit."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ORCHESTRATOR = ROOT / "skills" / "audit-orchestrator" / "scripts" / "run_audit.py"

if __name__ == "__main__":
    cmd = [sys.executable, str(ORCHESTRATOR)] + sys.argv[1:]
    result = subprocess.run(cmd)
    sys.exit(result.returncode)
