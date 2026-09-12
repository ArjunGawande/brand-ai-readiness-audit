#!/usr/bin/env python3
"""Run the crawler and automatically pass its evidence to the retrievability checker."""

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
CRAWLER_PATH = ROOT / "skills" / "crawl-render-audit" / "scripts" / "crawl.py"
CHECKER_PATH = ROOT / "skills" / "retrievability-checker" / "scripts" / "check_retrievability.py"
RENDER_CHECK_PATH = ROOT / "skills" / "render_check" / "scripts" / "analyze_render.py"
RECOMMEND_PATH = ROOT / "skills" / "recommendation" / "scripts" / "recommend.py"


def resolve_python() -> str:
    """Prefer the project venv Python when present, otherwise use the active interpreter."""
    candidate_dirs = [
        ROOT / "skills" / "crawl-render-audit" / "scripts" / "venv",
        ROOT / "venv",
    ]

    for base in candidate_dirs:
        bin_dir = base / ("Scripts" if sys.platform.startswith("win") else "bin")
        python_name = "python.exe" if sys.platform.startswith("win") else "python"
        candidate = bin_dir / python_name
        if candidate.exists():
            return str(candidate)

    return sys.executable


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the crawl-render-audit skill and immediately analyze the generated evidence."
    )
    parser.add_argument("url", help="The target site to audit, for example https://example.com")
    parser.add_argument(
        "--evidence-file",
        default=str(ROOT / "evidence.json"),
        help="Where to write the crawler's JSON output (default: brand-ai-readiness-audit/evidence.json)",
    )
    parser.add_argument(
        "--findings-file",
        default=str(ROOT / "findings.json"),
        help="Where to write the checker JSON output (default: brand-ai-readiness-audit/findings.json)",
    )
    args = parser.parse_args()

    python_executable = resolve_python()
    evidence_path = Path(args.evidence_file).resolve()
    findings_path = Path(args.findings_file).resolve()

    target_url = args.url.strip()
    if not target_url.startswith(("http://", "https://")):
        target_url = f"https://{target_url}"

    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    findings_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"[1/4] Running crawler for {target_url}...")
    crawl_result = subprocess.run(
        [python_executable, str(CRAWLER_PATH), target_url],
        capture_output=True,
        text=True,
        check=False,
    )

    if crawl_result.stdout.strip():
        evidence_path.write_text(crawl_result.stdout, encoding="utf-8")
        print(f"Saved crawler evidence to: {evidence_path}")
    else:
        print("Crawler produced no stdout output.", file=sys.stderr)

    if crawl_result.stderr:
        print(crawl_result.stderr, file=sys.stderr)

    if crawl_result.returncode != 0:
        print(f"Crawler exited with code {crawl_result.returncode}.", file=sys.stderr)
        return crawl_result.returncode

    print(f"[2/4] Running retrievability check against {evidence_path}...")
    check_result = subprocess.run(
        [python_executable, str(CHECKER_PATH), str(evidence_path)],
        capture_output=True,
        text=True,
        check=False,
    )

    if check_result.returncode != 0:
        print(f"Retrievability check exited with code {check_result.returncode}.", file=sys.stderr)
        print(check_result.stderr, file=sys.stderr)
        return check_result.returncode

    print(f"[3/4] Running render gap check against {evidence_path}...")
    render_result = subprocess.run(
        [python_executable, str(RENDER_CHECK_PATH), str(evidence_path)],
        capture_output=True,
        text=True,
        check=False,
    )

    if render_result.returncode != 0:
        print(f"Render gap check exited with code {render_result.returncode}.", file=sys.stderr)
        print(render_result.stderr, file=sys.stderr)
        return render_result.returncode

    try:
        retrievability_data = json.loads(check_result.stdout) if check_result.stdout.strip() else {}
    except json.JSONDecodeError:
        print("Failed to parse retrievability findings as JSON.", file=sys.stderr)
        print(check_result.stdout, file=sys.stderr)
        retrievability_data = {}

    try:
        render_data = json.loads(render_result.stdout) if render_result.stdout.strip() else {}
    except json.JSONDecodeError:
        print("Failed to parse render gap findings as JSON.", file=sys.stderr)
        print(render_result.stdout, file=sys.stderr)
        render_data = {}

    combined_findings = retrievability_data.get("findings", []) + render_data.get("findings", [])
    
    summary1 = retrievability_data.get("summary", {})
    summary2 = render_data.get("summary", {})
    
    combined_summary = {
        "total_findings": summary1.get("total_findings", 0) + summary2.get("total_findings", 0),
        "critical": summary1.get("critical", 0) + summary2.get("critical", 0),
        "high": summary1.get("high", 0) + summary2.get("high", 0),
        "medium": summary1.get("medium", 0) + summary2.get("medium", 0),
        "low": summary1.get("low", 0) + summary2.get("low", 0),
        "info": summary1.get("info", 0) + summary2.get("info", 0),
    }

    combined_notes = retrievability_data.get("coverage_notes", []) + render_data.get("coverage_notes", [])

    combined = {
        "site": retrievability_data.get("site") or render_data.get("site"),
        "audited_at": retrievability_data.get("audited_at") or render_data.get("analyzed_from"),
        "skills_run": ["retrievability-checker", "render-gap-audit"],
        "summary": combined_summary,
        "findings": combined_findings,
    }
    if combined_notes:
        combined["coverage_notes"] = combined_notes

    findings_path.write_text(json.dumps(combined, indent=2), encoding="utf-8")
    print(f"Saved combined findings to: {findings_path}")

    proactive_path = ROOT / "proactive.json"
    print(f"[4/4] Generating proactive recommendations...")
    rec_result = subprocess.run(
        [python_executable, str(RECOMMEND_PATH), str(evidence_path), "--findings", str(findings_path), "--out", str(proactive_path)],
        capture_output=True,
        text=True,
        check=False,
    )

    if rec_result.returncode != 0:
        print(f"Recommendation engine exited with code {rec_result.returncode}.", file=sys.stderr)
        print(rec_result.stderr, file=sys.stderr)
        return rec_result.returncode
    
    print(f"Saved proactive recommendations to: {proactive_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
