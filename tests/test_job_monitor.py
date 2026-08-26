import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

import job_monitor


class FakeResponse:
    def __init__(self, data=None, content=b"", content_type="application/json", status=200):
        self._data = data
        self.content = content or (json.dumps(data).encode("utf-8") if data is not None else b"")
        self.headers = {"Content-Type": content_type}
        self.status_code = status
        self.text = self.content.decode("utf-8", errors="replace")

    def json(self):
        return self._data if self._data is not None else json.loads(self.content)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class AdapterTests(unittest.TestCase):
    def test_all_requested_platforms_are_registered(self):
        expected = {
            "greenhouse",
            "lever",
            "ashby",
            "workday",
            "smartrecruiters",
            "workable",
            "recruitee",
            "jobvite",
            "linkedin",
            "indeed",
            "ziprecruiter",
        }
        self.assertEqual(expected, set(job_monitor.FETCHERS))

    @patch("job_monitor._get_json")
    def test_greenhouse_lever_and_ashby_retain_locations(self, get_json):
        get_json.side_effect = [
            {
                "jobs": [
                    {
                        "id": 1,
                        "title": "Engineer",
                        "absolute_url": "https://greenhouse.test/1",
                        "location": {"name": "New York, NY"},
                    }
                ]
            },
            [
                {
                    "id": "2",
                    "text": "Developer",
                    "hostedUrl": "https://lever.test/2",
                    "categories": {"location": "Jersey City, NJ"},
                    "workplaceType": "hybrid",
                }
            ],
            {
                "jobs": [
                    {
                        "id": "3",
                        "title": "Cloud Engineer",
                        "jobUrl": "https://ashby.test/3",
                        "location": "Remote - US",
                        "isRemote": True,
                    }
                ]
            },
        ]

        self.assertEqual(
            job_monitor.fetch_greenhouse({"slug": "acme"})[0][3], "New York, NY"
        )
        self.assertEqual(
            job_monitor.fetch_lever({"slug": "acme"})[0][3],
            "Jersey City, NJ, hybrid",
        )
        self.assertEqual(
            job_monitor.fetch_ashby({"slug": "acme"})[0][3],
            "Remote - US, Remote",
        )

    @patch("job_monitor._get_json")
    def test_smartrecruiters_paginates(self, get_json):
        get_json.side_effect = [
            {
                "content": [
                    {
                        "id": "1",
                        "name": "Engineer",
                        "location": {"city": "New York", "region": "NY"},
                    }
                ],
                "totalFound": 2,
            },
            {
                "content": [{"id": "2", "name": "Developer"}],
                "totalFound": 2,
            },
        ]

        jobs = job_monitor.fetch_smartrecruiters({"slug": "acme"})

        self.assertEqual(
            jobs,
            [
                (
                    "1",
                    "Engineer",
                    "https://jobs.smartrecruiters.com/acme/1",
                    "New York, NY",
                ),
                ("2", "Developer", "https://jobs.smartrecruiters.com/acme/2", ""),
            ],
        )
        self.assertEqual(get_json.call_count, 2)

    @patch("job_monitor._get_json")
    def test_workable_uses_shortcode_and_public_url(self, get_json):
        get_json.return_value = {
            "jobs": [
                {
                    "shortcode": "ABC123",
                    "title": "Platform Engineer",
                    "url": "https://apply.workable.com/j/ABC123",
                    "country": "United States",
                    "locations": [
                        {"country": "United States", "countryCode": "US"}
                    ],
                    "telecommuting": True,
                }
            ]
        }

        jobs = job_monitor.fetch_workable({"slug": "acme"})

        self.assertEqual(
            jobs,
            [
                (
                    "ABC123",
                    "Platform Engineer",
                    "https://apply.workable.com/j/ABC123",
                    "United States, US, Remote",
                )
            ],
        )

    @patch("job_monitor._get_json")
    def test_recruitee_ignores_non_published_offers(self, get_json):
        get_json.return_value = {
            "offers": [
                {
                    "id": 10,
                    "title": "Backend Engineer",
                    "slug": "backend-engineer",
                    "careers_url": "https://jobs.acme.test/o/backend-engineer",
                    "status": "published",
                    "location": "Newark, NJ",
                },
                {"id": 11, "title": "Draft", "status": "draft"},
            ]
        }

        jobs = job_monitor.fetch_recruitee({"slug": "acme"})

        self.assertEqual(
            jobs,
            [
                (
                    "10",
                    "Backend Engineer",
                    "https://jobs.acme.test/o/backend-engineer",
                    "Newark, NJ",
                )
            ],
        )

    @patch("job_monitor.requests.post")
    def test_workday_paginates_and_builds_public_urls(self, post):
        post.side_effect = [
            FakeResponse(
                {
                    "total": 2,
                    "jobPostings": [
                        {
                            "title": "Engineer",
                            "externalPath": "/job/one",
                            "locationsText": "New York, NY",
                        }
                    ],
                }
            ),
            FakeResponse(
                {
                    "total": 0,
                    "jobPostings": [
                        {"title": "Developer", "externalPath": "/job/two"}
                    ],
                }
            ),
        ]
        source = {
            "ats": "workday",
            "host": "acme.wd5.myworkdayjobs.com",
            "tenant": "acme",
            "site": "Careers",
            "page_size": 1,
            "search_text": "junior",
        }

        jobs = job_monitor.fetch_workday(source)

        self.assertEqual(
            jobs,
            [
                (
                    "/job/one",
                    "Engineer",
                    "https://acme.wd5.myworkdayjobs.com/Careers/job/one",
                    "New York, NY",
                ),
                (
                    "/job/two",
                    "Developer",
                    "https://acme.wd5.myworkdayjobs.com/Careers/job/two",
                    "",
                ),
            ],
        )
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args_list[0].kwargs["json"]["searchText"], "junior")


class FeedTests(unittest.TestCase):
    def test_parses_rss_atom_and_jobvite_style_xml(self):
        rss = b"""
        <rss><channel><item><guid>rss-1</guid><title>RSS Engineer</title>
        <link>https://example.test/rss-1</link></item></channel></rss>
        """
        atom = b"""
        <feed xmlns="http://www.w3.org/2005/Atom"><entry><id>atom-1</id>
        <title>Atom Engineer</title><link href="https://example.test/atom-1" /></entry></feed>
        """
        jobvite = b"""
        <jobs><job><requisition-id>req-1</requisition-id><job-title>Jobvite Engineer</job-title>
        <detail-url>https://example.test/req-1</detail-url></job></jobs>
        """

        self.assertEqual(
            job_monitor._parse_xml_jobs(rss),
            [("rss-1", "RSS Engineer", "https://example.test/rss-1", "")],
        )
        self.assertEqual(
            job_monitor._parse_xml_jobs(atom),
            [("atom-1", "Atom Engineer", "https://example.test/atom-1", "")],
        )
        self.assertEqual(
            job_monitor._parse_xml_jobs(jobvite),
            [("req-1", "Jobvite Engineer", "https://example.test/req-1", "")],
        )

    def test_parses_common_json_feed_shapes(self):
        data = {
            "results": [
                {
                    "jobId": 123,
                    "jobTitle": "Cloud Engineer",
                    "jobUrl": "https://example.test/123",
                    "location": "Remote - US",
                }
            ]
        }
        self.assertEqual(
            job_monitor._parse_json_jobs(data),
            [("123", "Cloud Engineer", "https://example.test/123", "Remote - US")],
        )

    @patch("job_monitor.requests.get")
    def test_feed_url_can_come_from_a_secret(self, get):
        get.return_value = FakeResponse(
            content=b"<rss><channel><item><guid>1</guid><title>Engineer</title></item></channel></rss>",
            content_type="application/rss+xml",
        )
        source = {
            "platform": "indeed",
            "feed_url_env": "TEST_APPROVED_FEED_URL",
        }

        with patch.dict(os.environ, {"TEST_APPROVED_FEED_URL": "https://feed.test/token"}):
            jobs = job_monitor.fetch_authorized_feed(source)

        self.assertEqual(jobs[0][:2], ("1", "Engineer"))
        self.assertEqual(get.call_args.args[0], "https://feed.test/token")


class ZipRecruiterTests(unittest.TestCase):
    def test_maps_simple_config_to_the_advertised_schema(self):
        source = {
            "query": "software engineer",
            "location": "Philadelphia, PA",
            "radius": 25,
        }
        schema = {
            "properties": {
                "search": {"type": "string"},
                "location": {"type": "string"},
                "radius_miles": {"type": "number"},
            },
            "required": ["search"],
        }

        self.assertEqual(
            job_monitor._ziprecruiter_arguments(source, schema),
            {
                "search": "software engineer",
                "location": "Philadelphia, PA",
                "radius_miles": 25,
            },
        )

    def test_parses_structured_mcp_results(self):
        result = {
            "structuredContent": {
                "jobs": [
                    {
                        "job_id": "zip-1",
                        "title": "Python Developer",
                        "job_url": "https://zip.test/zip-1",
                        "location": "New York, NY",
                    }
                ]
            }
        }
        self.assertEqual(
            job_monitor._parse_mcp_jobs(result),
            [
                (
                    "zip-1",
                    "Python Developer",
                    "https://zip.test/zip-1",
                    "New York, NY",
                )
            ],
        )


class ConfigurationTests(unittest.TestCase):
    def test_checked_in_config_excludes_unavailable_sources(self):
        sources = job_monitor.load_config()
        enabled_sources = [
            source for source in sources if source.get("enabled", True) is not False
        ]
        enabled_providers = {
            source.get("ats") or source.get("platform") for source in enabled_sources
        }

        self.assertTrue(enabled_sources)
        self.assertTrue(
            enabled_providers.isdisjoint(
                {"jobvite", "linkedin", "indeed", "ziprecruiter"}
            )
        )
        for source in enabled_sources:
            keywords = {keyword.lower() for keyword in source.get("keywords", [])}
            self.assertTrue(keywords)
            exclude_keywords = {
                keyword.lower() for keyword in source.get("exclude_keywords", [])
            }
            self.assertTrue({"senior", "staff", "principal", "manager"}.issubset(exclude_keywords))
            location_filter = source.get("location_filter", {})
            self.assertTrue(location_filter.get("include"))
            self.assertTrue(location_filter.get("include_remote"))
            self.assertEqual(
                {term.lower() for term in location_filter.get("remote_include", [])},
                {"united states", "usa", "us"},
            )
            self.assertFalse(location_filter.get("allow_unknown"))

        configured_names = {source["name"] for source in enabled_sources}
        self.assertTrue(
            {
                "Palantir",
                "Confido",
                "Giga",
                "Handshake",
                "WHOOP",
                "Lightfield",
                "Traba",
                "Leidos",
                "Globus Medical",
                "Acrisure",
            }.issubset(configured_names)
        )

    def test_load_config_combines_companies_and_searches(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "companies.yaml"
            config_path.write_text(
                "source_defaults:\n  exclude_keywords: [senior]\n"
                "companies:\n  - {name: Acme, ats: greenhouse, slug: acme}\n"
                "searches:\n  - {name: Search, platform: ziprecruiter, id: search}\n",
                encoding="utf-8",
            )
            with patch.object(job_monitor, "CONFIG_PATH", config_path):
                sources = job_monitor.load_config()

        self.assertEqual([source["name"] for source in sources], ["Acme", "Search"])
        self.assertEqual(sources[0]["exclude_keywords"], ["senior"])
        self.assertEqual(sources[1]["exclude_keywords"], ["senior"])

    def test_explicit_id_keeps_multiple_searches_separate(self):
        one = {"id": "backend-philly", "platform": "ziprecruiter"}
        two = {"id": "frontend-remote", "platform": "ziprecruiter"}
        self.assertNotEqual(
            job_monitor._state_key(one, "ziprecruiter"),
            job_monitor._state_key(two, "ziprecruiter"),
        )


class EndToEndTests(unittest.TestCase):
    @patch("job_monitor.requests.post")
    def test_discord_backfill_is_sent_in_complete_chunks(self, post):
        post.return_value = FakeResponse(status=204)
        jobs = [
            (f"Engineer {index}", f"https://jobs.test/{index}")
            for index in range(21)
        ]

        with patch("job_monitor.time.sleep") as sleep:
            job_monitor.send_discord_notification(
                "https://discord.test/hook", "Acme", jobs
            )

        self.assertEqual(post.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        delivered = "\n".join(
            call.kwargs["json"]["content"] for call in post.call_args_list
        )
        self.assertIn("Engineer 0", delivered)
        self.assertIn("Engineer 20", delivered)

    def test_keyword_matching_uses_word_boundaries_for_job_levels(self):
        self.assertTrue(
            job_monitor.matches_keywords("Software Engineer I", ["software engineer i"])
        )
        self.assertFalse(
            job_monitor.matches_keywords("Software Engineer II", ["software engineer i"])
        )
        self.assertTrue(
            job_monitor.matches_keywords("Junior DevOps Engineer", ["junior"])
        )
        self.assertTrue(
            job_monitor.matches_keywords(
                "Databricks Full Stack Developer", ["developer", "full stack"]
            )
        )
        self.assertFalse(
            job_monitor.matches_keywords(
                "Senior Software Engineer", ["engineer"], ["senior", "staff"]
            )
        )
        self.assertFalse(
            job_monitor.matches_keywords(
                "Staff/Lead Platform Engineer", ["engineer"], ["staff", "lead"]
            )
        )
        self.assertFalse(
            job_monitor.matches_keywords(
                "Engineering Manager", ["engineering"], ["manager"]
            )
        )

    def test_location_filter_keeps_ny_nj_and_generic_us_remote_jobs(self):
        location_filter = {
            "include": [
                "New York",
                "New York, NY",
                "New York City",
                "Brooklyn",
                "New Jersey",
                "NJ",
                "Newark",
            ],
            "include_remote": True,
            "remote_include": ["United States", "USA", "US"],
            "allow_unspecified_remote": False,
            "allow_unknown": False,
        }

        self.assertTrue(job_monitor.matches_location("New York, NY", location_filter))
        self.assertTrue(job_monitor.matches_location("Brooklyn, New York", location_filter))
        self.assertTrue(job_monitor.matches_location("Newark, NJ", location_filter))
        self.assertTrue(
            job_monitor.matches_location(
                "New York, NY; San Francisco, CA", location_filter
            )
        )
        self.assertTrue(job_monitor.matches_location("Remote - United States", location_filter))
        self.assertTrue(
            job_monitor.matches_location(
                "Remote - US, Remote - Canada", location_filter
            )
        )
        self.assertFalse(job_monitor.matches_location("San Francisco, CA", location_filter))
        self.assertFalse(job_monitor.matches_location("Newark, DE", location_filter))
        self.assertFalse(job_monitor.matches_location("Remote - California", location_filter))
        self.assertFalse(job_monitor.matches_location("Remote - Canada", location_filter))
        self.assertFalse(job_monitor.matches_location("Remote, Poland, PL", location_filter))
        self.assertFalse(
            job_monitor.matches_location("Remote - European Union", location_filter)
        )
        self.assertFalse(job_monitor.matches_location("Remote", location_filter))
        self.assertFalse(job_monitor.matches_location("", location_filter))

    def test_main_filters_notifies_and_persists_all_current_ids(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "companies.yaml"
            state_path = Path(temp_dir) / "state.json"
            config_path.write_text(
                "companies:\n"
                "  - name: Acme\n"
                "    ats: greenhouse\n"
                "    slug: acme\n"
                "    keywords: [engineer]\n"
                "    exclude_keywords: [senior]\n"
                "    location_filter:\n"
                "      include: ['New York, NY', NJ]\n"
                "      include_remote: true\n"
                "      remote_include: [United States, USA, US]\n"
                "      allow_unknown: false\n",
                encoding="utf-8",
            )
            state_path.write_text(
                json.dumps({"greenhouse:acme": ["old", "existing-match"]}),
                encoding="utf-8",
            )
            fetcher = Mock(
                return_value=[
                    ("old", "Sales", "https://jobs.test/old", "New York, NY"),
                    (
                        "existing-match",
                        "Cloud Engineer",
                        "https://jobs.test/existing-match",
                        "New York, NY",
                    ),
                    (
                        "new",
                        "Platform Engineer",
                        "https://jobs.test/new",
                        "New York, NY",
                    ),
                    (
                        "wrong-location",
                        "Backend Engineer",
                        "https://jobs.test/wrong-location",
                        "San Francisco, CA",
                    ),
                    (
                        "filtered",
                        "Account Executive",
                        "https://jobs.test/filtered",
                        "New York, NY",
                    ),
                    (
                        "senior",
                        "Senior Software Engineer",
                        "https://jobs.test/senior",
                        "New York, NY",
                    ),
                ]
            )

            with (
                patch.object(job_monitor, "CONFIG_PATH", config_path),
                patch.object(job_monitor, "STATE_PATH", state_path),
                patch.dict(job_monitor.FETCHERS, {"greenhouse": fetcher}),
                patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": "https://discord.test/hook"}),
                patch("job_monitor.send_discord_notification") as notify,
                patch("job_monitor.time.sleep"),
            ):
                job_monitor.main()

                notify.assert_called_once_with(
                    "https://discord.test/hook",
                    "Acme",
                    [
                        ("Cloud Engineer", "https://jobs.test/existing-match"),
                        ("Platform Engineer", "https://jobs.test/new"),
                    ],
                )
                notify.reset_mock()
                job_monitor.main()
                notify.assert_not_called()

            saved = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertEqual(
            saved["greenhouse:acme"],
            [
                "existing-match",
                "filtered",
                "new",
                "old",
                "senior",
                "wrong-location",
            ],
        )
        self.assertEqual(
            saved["greenhouse:acme:notified-v2"],
            ["existing-match", "new"],
        )


if __name__ == "__main__":
    unittest.main()
