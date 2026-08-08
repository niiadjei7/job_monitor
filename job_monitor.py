#!/usr/bin/env python3
"""
job_monitor.py

Checks Greenhouse / Lever / Ashby job boards for companies listed in
companies.yaml, diffs against previously-seen postings in state.json,
and pings a Discord webhook for anything new.

Environment variables required:
  DISCORD_WEBHOOK_URL  - Discord incoming webhook URL

Usage:
  python job_monitor.py
"""

import json
import os
import sys
import time
from pathlib import Path

import requests
import yaml

CONFIG_PATH = Path("companies.yaml")
STATE_PATH = Path("state.json")
REQUEST_TIMEOUT = 15
USER_AGENT = "job-monitor-bot/1.0 (personal use)"


def load_config():
    if not CONFIG_PATH.exists():
        print(f"ERROR: {CONFIG_PATH} not found.", file=sys.stderr)
        sys.exit(1)
    with open(CONFIG_PATH) as f:
        data = yaml.safe_load(f)
    return data.get("companies", [])


def load_state():
    if not STATE_PATH.exists():
        return {}
    with open(STATE_PATH) as f:
        return json.load(f)


def save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def fetch_greenhouse(slug):
    """Returns list of (job_id, title, url) for a Greenhouse board."""
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    jobs = resp.json().get("jobs", [])
    return [
        (str(job["id"]), job["title"], job.get("absolute_url", ""))
        for job in jobs
    ]


def fetch_lever(slug):
    """Returns list of (job_id, title, url) for a Lever board."""
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    jobs = resp.json()
    return [
        (job["id"], job.get("text", "Untitled"), job.get("hostedUrl", ""))
        for job in jobs
    ]


def fetch_ashby(slug):
    """Returns list of (job_id, title, url) for an Ashby board."""
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    jobs = resp.json().get("jobs", [])
    return [
        (job["id"], job.get("title", "Untitled"), job.get("jobUrl", ""))
        for job in jobs
    ]


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
}


def matches_keywords(title, keywords):
    if not keywords:
        return True
    title_lower = title.lower()
    return any(kw.lower() in title_lower for kw in keywords)


def send_discord_notification(webhook_url, company_name, new_jobs):
    if not new_jobs:
        return

    lines = [f"**{len(new_jobs)} new posting(s) at {company_name}**"]
    for title, url in new_jobs[:10]:  # cap per-message to stay under Discord limits
        lines.append(f"- [{title}]({url})" if url else f"- {title}")
    if len(new_jobs) > 10:
        lines.append(f"...and {len(new_jobs) - 10} more.")

    payload = {"content": "\n".join(lines)}
    resp = requests.post(webhook_url, json=payload, timeout=REQUEST_TIMEOUT)
    if resp.status_code >= 300:
        print(f"WARN: Discord webhook returned {resp.status_code}: {resp.text}", file=sys.stderr)


def main():
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        print("ERROR: DISCORD_WEBHOOK_URL environment variable not set.", file=sys.stderr)
        sys.exit(1)

    companies = load_config()
    state = load_state()
    any_errors = False

    for company in companies:
        name = company.get("name", company.get("slug", "unknown"))
        ats = company.get("ats")
        slug = company.get("slug")
        keywords = company.get("keywords", [])

        fetcher = FETCHERS.get(ats)
        if not fetcher:
            print(f"WARN: unknown ats '{ats}' for {name}, skipping.", file=sys.stderr)
            continue

        state_key = f"{ats}:{slug}"
        seen_ids = set(state.get(state_key, []))

        try:
            jobs = fetcher(slug)
        except Exception as exc:
            print(f"WARN: failed to fetch {name} ({ats}/{slug}): {exc}", file=sys.stderr)
            any_errors = True
            continue

        current_ids = {job_id for job_id, _, _ in jobs}
        new_jobs = [
            (title, url)
            for job_id, title, url in jobs
            if job_id not in seen_ids and matches_keywords(title, keywords)
        ]

        if new_jobs:
            print(f"{name}: {len(new_jobs)} new matching posting(s).")
            send_discord_notification(webhook_url, name, new_jobs)
        else:
            print(f"{name}: no new matching postings.")

        # Always update seen_ids to the full current set, regardless of
        # keyword filtering, so a keyword change later doesn't cause a
        # flood of "new" postings that were actually already live.
        state[state_key] = sorted(current_ids)

        time.sleep(1)  # be polite between requests

    save_state(state)

    if any_errors:
        sys.exit(2)


if __name__ == "__main__":
    main()
