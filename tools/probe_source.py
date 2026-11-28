#!/usr/bin/env python3
"""Check whether a job source answers from this machine, without alerting.

Runs one fetcher against one board and prints what came back: how many
postings, how many requests, every HTTP status seen, and a few parsed rows.
Nothing is written to state.json, the tracker or Discord.

The reason it exists: Workday answered from a home connection but rejected
GitHub's runners with HTTP 429, and that was only found after weeks of red
runs. The source-probe workflow runs this script on GitHub's runners before a
new source is added to companies.yaml.

    python tools/probe_source.py --ats icims --host nursing-emory.icims.com
"""

import argparse
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import job_monitor  # noqa: E402

DEFAULT_TITLE_TERMS = ["rn", "registered nurse", "nurse resident", "nurse residency"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--ats", required=True, choices=sorted(job_monitor.KNOWN_ID_FETCHERS))
    parser.add_argument("--host", required=True)
    parser.add_argument("--title-term", action="append", dest="title_terms")
    parser.add_argument("--max-details", type=int, default=3)
    args = parser.parse_args(argv)

    statuses = Counter()
    real_request = job_monitor._request

    def counting_request(method, url, **kwargs):
        response = real_request(method, url, **kwargs)
        statuses[response.status_code] += 1
        return response

    job_monitor._request = counting_request
    source = {
        "name": f"probe {args.host}",
        "ats": args.ats,
        "host": args.host,
        "keyword_categories": {"Probe": {"title": args.title_terms or DEFAULT_TITLE_TERMS}},
        "max_details_per_run": args.max_details,
    }
    runner = "GitHub Actions" if os.environ.get("GITHUB_ACTIONS") else "local"
    print(f"probe: {args.ats} {args.host} from {runner}")
    try:
        jobs = job_monitor.FETCHERS[args.ats](source, known_ids=[])
    except Exception as exc:  # report, then fail the step
        print(f"FAILED: {type(exc).__name__}: {exc}")
        print(f"HTTP statuses: {dict(statuses)}")
        return 1

    detailed = [job for job in jobs if job[5]]
    print(f"postings returned: {len(jobs)}  with structured details: {len(detailed)}")
    print(f"HTTP statuses: {dict(statuses)}")
    for job_id, title, url, location, context, description in detailed:
        print(f"- {job_id} | {title} | {location or '<no location>'} | {context} | {len(description)} chars")
    blocked = any(code in (403, 429) for code in statuses)
    if blocked or not jobs:
        print("RESULT: blocked or empty" if blocked else "RESULT: no postings")
        return 1
    print("RESULT: ok")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
