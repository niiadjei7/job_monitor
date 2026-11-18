#!/usr/bin/env python3
"""Measure Job Monitor's notification decision against the labelled oracle.

The oracle is a set of real postings, each labelled ``match``, ``borderline``,
``reject`` or ``not_applicable`` under the oracle rubric. Its data is private
(it includes postings from the owner's own application history), so it lives in
``tests/oracle/``, which git ignores, never in the public repository.

Commands
--------
export   Convert the labelled CSV into ``tests/oracle/postings.jsonl``::

             python tests/oracle_eval.py export "<path to oracle csv>"

eval     Run every posting through ``job_monitor.evaluate_job`` and report
         agreement::

             python tests/oracle_eval.py eval [--profile PATH] [--details]

         The profile defaults to ``tests/oracle/profile.yaml`` when it exists,
         then ``profile.example.yaml``.

Agreement rules: a ``match`` should be notified; a ``reject`` or
``not_applicable`` should not; a ``borderline`` may go either way and is
reported separately. Rows labelled from the title alone (no description text)
are reported separately because description-based rules cannot be judged on
them.
"""

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ORACLE_DIR = ROOT / "tests" / "oracle"
POSTINGS_PATH = ORACLE_DIR / "postings.jsonl"

sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

import job_monitor  # noqa: E402
from score import score_threshold  # noqa: E402

FIELDS = [
    "posting_id", "source", "company", "title", "location", "department",
    "employment_type", "canonical_url", "description_text", "line_of_work",
    "role_family", "fit_decision", "reason_codes_semicolon", "human_note",
    "labeler", "label_status", "review_tier",
]
SHOULD_NOTIFY = {"match": True, "reject": False, "not_applicable": False}


def label_basis(row):
    if not row.get("description_text", "").strip() or "no description" in row.get("labeler", ""):
        return "title_only"
    return "description"


def export(csv_path):
    with open(csv_path, encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    ORACLE_DIR.mkdir(parents=True, exist_ok=True)
    with open(POSTINGS_PATH, "w", encoding="utf-8") as out:
        for row in rows:
            record = {field: row.get(field, "") for field in FIELDS}
            record["label_basis"] = label_basis(row)
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
    counts = Counter((r["fit_decision"]) for r in rows)
    print(f"wrote {len(rows)} postings to {POSTINGS_PATH}: {dict(counts)}")


def load_postings(path=POSTINGS_PATH):
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def load_profile(path=None):
    candidates = [Path(path)] if path else [ORACLE_DIR / "profile.yaml", ROOT / "profile.example.yaml"]
    for candidate in candidates:
        if candidate.exists():
            with open(candidate, encoding="utf-8") as handle:
                return yaml.safe_load(handle) or {}, candidate
    return {}, None


def source_configs():
    """Map source name -> merged source config; fall back to a generic config."""
    old = Path.cwd()
    try:
        import os

        os.chdir(ROOT)
        sources = job_monitor.load_config()
    finally:
        os.chdir(old)
    by_name = {s.get("name"): s for s in sources if isinstance(s, dict)}
    generic = next(
        (s for s in sources if isinstance(s, dict) and s.get("keyword_categories")), {}
    )
    return by_name, generic


def evaluate(postings, profile):
    by_name, generic = source_configs()
    threshold = score_threshold(profile)
    results = []
    for row in postings:
        source = by_name.get(row["source"], generic)
        notified, result, signal = job_monitor.evaluate_job(
            source,
            profile,
            threshold,
            row["title"],
            row["location"],
            row.get("department", ""),
            row.get("description_text", ""),
        )
        reasons = job_monitor._rejection_reasons(result, threshold, signal)
        if not job_monitor.matches_location(row["location"], source.get("location_filter")):
            reasons = ["location filter"] + [r for r in reasons if r != "location"]
        results.append({**row, "notified": notified, "score": result["score"], "reasons": reasons})
    return results, threshold


def summarize(results):
    summary = {}
    for basis in ("description", "title_only", "all"):
        rows = [r for r in results if basis == "all" or r["label_basis"] == basis]
        table = {}
        for label in ("match", "borderline", "reject", "not_applicable"):
            group = [r for r in rows if r["fit_decision"] == label]
            table[label] = (sum(r["notified"] for r in group), len(group))
        summary[basis] = table
    return summary


def print_report(results, threshold, profile_path, details=False):
    print(f"profile: {profile_path or '(none)'}   threshold: {threshold}")
    summary = summarize(results)
    print(f"\n{'basis':<12} {'label':<15} {'notified':>9} {'total':>6}")
    for basis, table in summary.items():
        for label, (notified, total) in table.items():
            if total:
                print(f"{basis:<12} {label:<15} {notified:>9} {total:>6}")
    disagreements = [
        r for r in results
        if r["fit_decision"] in SHOULD_NOTIFY and r["notified"] != SHOULD_NOTIFY[r["fit_decision"]]
    ]
    correct = sum(
        1 for r in results
        if r["fit_decision"] in SHOULD_NOTIFY and r["notified"] == SHOULD_NOTIFY[r["fit_decision"]]
    )
    judged = sum(1 for r in results if r["fit_decision"] in SHOULD_NOTIFY)
    print(f"\nagreement (match/reject/not_applicable): {correct}/{judged}")
    missed = Counter()
    for r in results:
        if r["fit_decision"] == "match" and not r["notified"]:
            missed.update({r["reasons"][0] if r["reasons"] else "unknown": 1})
    print("first reason a match was missed:", dict(missed.most_common()))
    if details:
        print("\nDisagreements:")
        for r in sorted(disagreements, key=lambda r: (r["fit_decision"], r["label_basis"])):
            print(
                f"- [{r['fit_decision']}/{r['label_basis']}] notified={r['notified']} "
                f"score={r['score']} {r['company']} | {r['title']} | {r['location']} "
                f"| {'; '.join(r['reasons'][:3])}"
            )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    exp = sub.add_parser("export")
    exp.add_argument("csv_path")
    ev = sub.add_parser("eval")
    ev.add_argument("--profile")
    ev.add_argument("--details", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "export":
        export(args.csv_path)
        return 0
    if not POSTINGS_PATH.exists():
        print(f"no oracle at {POSTINGS_PATH}; run export first", file=sys.stderr)
        return 1
    profile, profile_path = load_profile(args.profile)
    results, threshold = evaluate(load_postings(), profile)
    print_report(results, threshold, profile_path, args.details)
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
