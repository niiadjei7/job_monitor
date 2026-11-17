"""Oracle regression test.

Runs only when the private oracle data exists in tests/oracle/ (it is
gitignored, so CI skips this). The floors below are the agreement measured
when score.py was fixed on 2026-11-17; a change may raise them, never lower
them without a recorded reason.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import oracle_eval  # noqa: E402


@unittest.skipUnless(oracle_eval.POSTINGS_PATH.exists(), "private oracle data not present")
class OracleRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        profile, _ = oracle_eval.load_profile()
        cls.results, _ = oracle_eval.evaluate(oracle_eval.load_postings(), profile)

    def test_no_nontechnical_posting_is_notified(self):
        notified = [r["title"] for r in self.results if r["fit_decision"] == "not_applicable" and r["notified"]]
        self.assertEqual(notified, [])

    def test_rejects_stay_rare(self):
        notified = [r["title"] for r in self.results if r["fit_decision"] == "reject" and r["notified"]]
        self.assertLessEqual(len(notified), 2, notified)

    def test_matches_are_missed_only_for_location(self):
        # Location is a separate policy (NYC metro + US remote); every other
        # reason for missing a labelled match is a scoring regression.
        missed = [
            (r["title"], r["reasons"][:2])
            for r in self.results
            if r["fit_decision"] == "match"
            and not r["notified"]
            and "location filter" not in r["reasons"]
        ]
        self.assertEqual(missed, [])


if __name__ == "__main__":
    unittest.main()
