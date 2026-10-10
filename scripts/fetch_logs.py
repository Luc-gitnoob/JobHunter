"""Utility to fetch GitHub Actions workflow runs and audit logs for weekly reviews."""

import os
import re
from datetime import datetime, timezone, timedelta
from typing import Optional
import httpx
from dotenv import load_dotenv

load_dotenv()

REPO = "Luc-gitnoob/JobHunter"


def _get_headers() -> dict:
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        raise ValueError("GITHUB_TOKEN not found in .env")
    return {
        "Authorization": f"Bearer {token}",
        "User-Agent": "JobHunter",
        "Accept": "application/vnd.github+json",
    }


def list_recent_runs(days: int = 7, limit: int = 30) -> list[dict]:
    """List workflow runs created in the last N days."""
    headers = _get_headers()
    since_date = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    url = f"https://api.github.com/repos/{REPO}/actions/runs?created=>={since_date}&per_page={limit}"

    with httpx.Client(headers=headers, timeout=30.0) as client:
        resp = client.get(url)
        resp.raise_for_status()
        data = resp.json()
        return data.get("workflow_runs", [])


def fetch_run_logs_by_id(run_id: int, output_file: Optional[str] = None) -> str:
    """Download logs for a specific workflow run ID."""
    headers = _get_headers()

    with httpx.Client(headers=headers, timeout=30.0) as client:
        # Get jobs for this run
        jobs_url = f"https://api.github.com/repos/{REPO}/actions/runs/{run_id}/jobs"
        resp = client.get(jobs_url)
        resp.raise_for_status()
        jobs_data = resp.json()

        if not jobs_data.get("jobs"):
            raise ValueError(f"No jobs found for run {run_id}")

        job = jobs_data["jobs"][0]
        job_id = job["id"]

        # Request log URL (302 redirect with presigned SAS URL)
        log_api_url = f"https://api.github.com/repos/{REPO}/actions/jobs/{job_id}/logs"
        resp = client.get(log_api_url, follow_redirects=False)

        if resp.status_code == 302:
            redirect_url = resp.headers.get("location")
            log_resp = httpx.get(redirect_url, timeout=60.0)
            log_content = log_resp.text
        elif resp.status_code == 200:
            log_content = resp.text
        else:
            resp.raise_for_status()
            return ""

    if output_file:
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(log_content)
        print(f"Saved {len(log_content.splitlines())} lines to {output_file}")

    return log_content


def fetch_latest_run_logs(output_file: str = "latest-run.txt") -> bool:
    """Download logs for the most recent run."""
    runs = list_recent_runs(days=7, limit=1)
    if not runs:
        print("No recent workflow runs found.")
        return False

    latest = runs[0]
    print(f"Latest Run: ID={latest['id']} | Status={latest['status']} | Conclusion={latest['conclusion']} | CreatedAt={latest['created_at']}")
    fetch_run_logs_by_id(latest["id"], output_file=output_file)
    return True


def audit_weekly_logs(days: int = 7) -> dict:
    """
    Audit all runs in the past N days.
    Extracts all dispatched roles, scores, filtered roles, and error signals.
    """
    runs = list_recent_runs(days=days)
    print(f"Found {len(runs)} workflow runs in the last {days} days.")

    audit_report = {
        "days": days,
        "total_runs": len(runs),
        "dispatched_jobs": [],
        "filtered_unscored": [],
        "errors": [],
    }

    for run in runs:
        run_id = run["id"]
        created = run["created_at"]
        conclusion = run.get("conclusion")
        if conclusion != "success":
            audit_report["errors"].append({"run_id": run_id, "created": created, "conclusion": conclusion})
            continue

        try:
            log_text = fetch_run_logs_by_id(run_id)
            for line in log_text.splitlines():
                # Extract PASS notifications
                if "PASS" in line and ">=" in line:
                    audit_report["dispatched_jobs"].append({"run_id": run_id, "date": created, "log": line.strip()})
                elif "DISQUALIFIED" in line or "BELOW THRESHOLD" in line:
                    audit_report["filtered_unscored"].append({"run_id": run_id, "date": created, "log": line.strip()})
        except Exception as e:
            audit_report["errors"].append({"run_id": run_id, "created": created, "error": str(e)})

    return audit_report


if __name__ == "__main__":
    fetch_latest_run_logs()
