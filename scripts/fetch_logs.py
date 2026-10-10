"""Utility to fetch the latest GitHub Actions workflow run and logs."""

import os
import json
import httpx
from dotenv import load_dotenv

load_dotenv()

def fetch_latest_run_logs(output_file: str = "latest-run.txt"):
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        print("ERROR: GITHUB_TOKEN not found in .env")
        return False

    repo = "Luc-gitnoob/JobHunter"
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": "JobHunter",
        "Accept": "application/vnd.github+json",
    }

    with httpx.Client(headers=headers, timeout=30.0) as client:
        # 1. Get latest workflow run
        runs_url = f"https://api.github.com/repos/{repo}/actions/runs?per_page=1"
        resp = client.get(runs_url)
        resp.raise_for_status()
        runs_data = resp.json()

        if not runs_data.get("workflow_runs"):
            print("No workflow runs found.")
            return False

        latest_run = runs_data["workflow_runs"][0]
        run_id = latest_run["id"]
        status = latest_run["status"]
        conclusion = latest_run["conclusion"]
        print(f"Latest Run: ID={run_id} | Status={status} | Conclusion={conclusion} | CreatedAt={latest_run.get('created_at')}")

        # 2. Get jobs for this run
        jobs_url = latest_run["jobs_url"]
        resp = client.get(jobs_url)
        resp.raise_for_status()
        jobs_data = resp.json()

        if not jobs_data.get("jobs"):
            print("No jobs found in this run.")
            return False

        job = jobs_data["jobs"][0]
        job_id = job["id"]
        print(f"Job: {job['name']} (ID={job_id}) | Status={job['status']}")

        # 3. Request log URL (GitHub sends 302 redirect with presigned SAS URL)
        log_api_url = f"https://api.github.com/repos/{repo}/actions/jobs/{job_id}/logs"
        resp = client.get(log_api_url, follow_redirects=False)
        
        if resp.status_code == 302:
            redirect_url = resp.headers.get("location")
            # Fetch directly from blob storage without GitHub Authorization header
            log_resp = httpx.get(redirect_url, timeout=60.0)
            log_content = log_resp.text
        elif resp.status_code == 200:
            log_content = resp.text
        else:
            resp.raise_for_status()
            return False

    with open(output_file, "w", encoding="utf-8") as f:
        f.write(log_content)

    print(f"Successfully fetched and saved {len(log_content.splitlines())} lines to {output_file}!")
    return True

if __name__ == "__main__":
    fetch_latest_run_logs()
