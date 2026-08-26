#!/usr/bin/env python3
"""Monitor public company job boards and approved job-search feeds."""

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree

import requests
import yaml

CONFIG_PATH = Path("companies.yaml")
STATE_PATH = Path("state.json")
REQUEST_TIMEOUT = 20
USER_AGENT = "job-monitor-bot/2.0 (personal use)"
MCP_PROTOCOL_VERSION = "2025-03-26"
NOTIFIED_STATE_SUFFIX = "notified-v2"

US_STATE_ABBREVIATIONS = {
    "AL",
    "AK",
    "AZ",
    "AR",
    "CA",
    "CO",
    "CT",
    "DE",
    "FL",
    "GA",
    "HI",
    "ID",
    "IL",
    "IN",
    "IA",
    "KS",
    "KY",
    "LA",
    "ME",
    "MD",
    "MA",
    "MI",
    "MN",
    "MS",
    "MO",
    "MT",
    "NE",
    "NV",
    "NH",
    "NM",
    "NC",
    "ND",
    "OH",
    "OK",
    "OR",
    "PA",
    "RI",
    "SC",
    "SD",
    "TN",
    "TX",
    "UT",
    "VT",
    "VA",
    "WA",
    "WV",
    "WI",
    "WY",
    "DC",
}
OTHER_US_STATE_NAMES = {
    "alabama",
    "alaska",
    "arizona",
    "arkansas",
    "california",
    "colorado",
    "connecticut",
    "delaware",
    "florida",
    "georgia",
    "hawaii",
    "idaho",
    "illinois",
    "indiana",
    "iowa",
    "kansas",
    "kentucky",
    "louisiana",
    "maine",
    "maryland",
    "massachusetts",
    "michigan",
    "minnesota",
    "mississippi",
    "missouri",
    "montana",
    "nebraska",
    "nevada",
    "new hampshire",
    "new mexico",
    "north carolina",
    "north dakota",
    "ohio",
    "oklahoma",
    "oregon",
    "pennsylvania",
    "rhode island",
    "south carolina",
    "south dakota",
    "tennessee",
    "texas",
    "utah",
    "vermont",
    "virginia",
    "washington",
    "west virginia",
    "wisconsin",
    "wyoming",
    "district of columbia",
}
FOREIGN_REMOTE_MARKERS = {
    "apac",
    "asia",
    "australia",
    "canada",
    "emea",
    "europe",
    "india",
    "latin america",
    "mexico",
    "united kingdom",
}


def load_config():
    if not CONFIG_PATH.exists():
        print(f"ERROR: {CONFIG_PATH} not found.", file=sys.stderr)
        sys.exit(1)

    with open(CONFIG_PATH, encoding="utf-8") as config_file:
        data = yaml.safe_load(config_file) or {}

    companies = data.get("companies", [])
    searches = data.get("searches", [])
    if not isinstance(companies, list) or not isinstance(searches, list):
        raise ValueError("companies and searches must both be YAML lists")
    raw_sources = companies + searches
    source_defaults = data.get("source_defaults", {}) or {}
    if not isinstance(source_defaults, dict):
        raise ValueError("source_defaults must be a mapping")
    return [
        {**source_defaults, **source} if isinstance(source, dict) else source
        for source in raw_sources
    ]


def load_state():
    if not STATE_PATH.exists():
        return {}
    with open(STATE_PATH, encoding="utf-8") as state_file:
        return json.load(state_file)


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as state_file:
        json.dump(state, state_file, indent=2, sort_keys=True)


def _source_value(source, key, default=None):
    """Allow the original fetcher('slug') calling convention in small scripts/tests."""
    if isinstance(source, str):
        return source if key == "slug" else default
    return source.get(key, default)


def _location_text(*values):
    """Flatten the common ATS location shapes into a readable string."""
    parts = []

    def add(value):
        if value is None or value is False:
            return
        if isinstance(value, dict):
            if value.get("remote") is True:
                add("Remote")
            for key in (
                "name",
                "text",
                "location",
                "city",
                "region",
                "state",
                "country",
                "country_code",
                "countryCode",
            ):
                add(value.get(key))
            return
        if isinstance(value, (list, tuple, set)):
            for item in value:
                add(item)
            return
        if value is True:
            add("Remote")
            return
        text = str(value).strip()
        if text and text.casefold() not in {part.casefold() for part in parts}:
            parts.append(text)

    for value in values:
        add(value)
    return ", ".join(parts)


def _normalize_location(value):
    return " ".join(re.findall(r"[a-z0-9]+", str(value).casefold()))


def _has_location_term(location, term):
    normalized_location = _normalize_location(location)
    normalized_term = _normalize_location(term)
    return bool(
        normalized_term
        and re.search(rf"(?<!\w){re.escape(normalized_term)}(?!\w)", normalized_location)
    )


def matches_location(location, location_filter):
    """Match a normalized ATS location against a configured metro-area filter."""
    if not location_filter:
        return True
    if not isinstance(location_filter, dict):
        raise ValueError("location_filter must be a mapping")

    location = str(location or "").strip()
    if not location:
        return bool(location_filter.get("allow_unknown", False))

    state_tokens = set(re.findall(r"\b[A-Z]{2}\b", location.upper()))
    has_local_region = bool(state_tokens & {"NY", "NJ"}) or any(
        _has_location_term(location, term) for term in ("new york", "new jersey")
    )
    has_other_state = bool(state_tokens & US_STATE_ABBREVIATIONS) or any(
        _has_location_term(location, state) for state in OTHER_US_STATE_NAMES
    )
    has_foreign_region = any(
        _has_location_term(location, marker) for marker in FOREIGN_REMOTE_MARKERS
    )
    has_included_location = any(
        _has_location_term(location, term)
        for term in location_filter.get("include", [])
    )
    if has_included_location and (
        has_local_region or not (has_other_state or has_foreign_region)
    ):
        return True

    is_remote = any(
        _has_location_term(location, term)
        for term in ("remote", "work from home", "anywhere")
    )
    if not (location_filter.get("include_remote", False) and is_remote):
        return False

    remote_include = location_filter.get("remote_include", [])
    if remote_include:
        # A positive U.S. marker wins for multi-region postings such as
        # "Remote - US, Remote - Canada" because the role is available in the U.S.
        return any(_has_location_term(location, term) for term in remote_include)

    # Unspecified remote jobs are rejected by default. A source can opt in only
    # when its feed contract guarantees that bare "Remote" means U.S.-remote.
    return bool(location_filter.get("allow_unspecified_remote", False)) and not (
        has_foreign_region or has_other_state
    )


def _get_json(url, **kwargs):
    response = requests.get(
        url,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        timeout=REQUEST_TIMEOUT,
        **kwargs,
    )
    response.raise_for_status()
    return response.json()


def fetch_greenhouse(source):
    """Return (job_id, title, URL, location) tuples for a Greenhouse board."""
    slug = _source_value(source, "slug")
    data = _get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    return [
        (
            str(job["id"]),
            job.get("title", "Untitled"),
            job.get("absolute_url", ""),
            _location_text(job.get("location")),
        )
        for job in data.get("jobs", [])
    ]


def fetch_lever(source):
    """Return jobs for a Lever board."""
    slug = _source_value(source, "slug")
    jobs = _get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    return [
        (
            str(job["id"]),
            job.get("text", "Untitled"),
            job.get("hostedUrl", ""),
            _location_text(
                (job.get("categories") or {}).get("location"),
                job.get("allLocations"),
                job.get("workplaceType"),
            ),
        )
        for job in jobs
    ]


def fetch_ashby(source):
    """Return jobs for an Ashby board."""
    slug = _source_value(source, "slug")
    data = _get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    return [
        (
            str(job["id"]),
            job.get("title", "Untitled"),
            job.get("jobUrl", ""),
            _location_text(
                job.get("location"),
                job.get("secondaryLocations"),
                job.get("workplaceType"),
                job.get("isRemote"),
            ),
        )
        for job in data.get("jobs", [])
    ]


def fetch_smartrecruiters(source):
    """Return every public posting for a SmartRecruiters company."""
    slug = _source_value(source, "slug")
    jobs = []
    offset = 0
    limit = 100

    while True:
        data = _get_json(
            f"https://api.smartrecruiters.com/v1/companies/{slug}/postings",
            params={"limit": limit, "offset": offset},
        )
        page = data.get("content", [])
        for job in page:
            job_id = str(job.get("id") or job.get("uuid"))
            title = job.get("name", "Untitled")
            # SmartRecruiters serves an ID-only URL, so no fragile title slug is needed.
            url = f"https://jobs.smartrecruiters.com/{slug}/{job_id}"
            jobs.append((job_id, title, url, _location_text(job.get("location"))))

        offset += len(page)
        if not page or offset >= data.get("totalFound", offset):
            break

    return jobs


def fetch_workable(source):
    """Return published jobs from Workable's documented public endpoint."""
    slug = _source_value(source, "slug")
    data = _get_json(
        f"https://www.workable.com/api/accounts/{slug}", params={"details": "false"}
    )
    return [
        (
            str(job.get("shortcode") or job.get("id")),
            job.get("title", "Untitled"),
            job.get("url") or job.get("shortlink") or job.get("application_url", ""),
            _location_text(
                job.get("location"),
                job.get("locations"),
                job.get("city"),
                job.get("state"),
                job.get("country"),
                (job.get("department") or {}).get("location")
                if isinstance(job.get("department"), dict)
                else None,
                job.get("remote"),
                job.get("telecommuting"),
            ),
        )
        for job in data.get("jobs", [])
    ]


def fetch_recruitee(source):
    """Return published jobs from a Recruitee careers site."""
    slug = _source_value(source, "slug")
    data = _get_json(f"https://{slug}.recruitee.com/api/offers/")
    jobs = []
    for job in data.get("offers", []):
        if job.get("status", "published") != "published":
            continue
        job_id = str(job.get("id") or job.get("guid") or job.get("slug"))
        title = job.get("title", "Untitled")
        url = job.get("careers_url") or f"https://{slug}.recruitee.com/o/{job.get('slug', '')}"
        jobs.append(
            (
                job_id,
                title,
                url,
                _location_text(
                    job.get("location"),
                    job.get("locations"),
                    job.get("city"),
                    job.get("region"),
                    job.get("country"),
                    job.get("remote"),
                ),
            )
        )
    return jobs


def _workday_origin(source):
    base_url = _source_value(source, "base_url")
    host = _source_value(source, "host")
    if not base_url and not host:
        raise ValueError("Workday sources require base_url or host")
    if base_url:
        parsed = urlparse(base_url if "://" in base_url else f"https://{base_url}")
        return f"{parsed.scheme}://{parsed.netloc}"
    return host.rstrip("/") if "://" in host else f"https://{host.rstrip('/')}"


def fetch_workday(source):
    """Return all postings from a Workday external career site."""
    tenant = _source_value(source, "tenant") or _source_value(source, "slug")
    site = _source_value(source, "site")
    if not tenant or not site:
        raise ValueError("Workday sources require tenant/slug and site")

    origin = _workday_origin(source)
    api_url = f"{origin}/wday/cxs/{tenant}/{site}/jobs"
    career_url = _source_value(source, "career_url", f"{origin}/{site}").rstrip("/")
    page_size = int(_source_value(source, "page_size", 20))
    search_text = str(_source_value(source, "search_text", ""))
    offset = 0
    total = None
    jobs = []

    while True:
        response = requests.post(
            api_url,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
            json={
                "appliedFacets": {},
                "limit": page_size,
                "offset": offset,
                "searchText": search_text,
            },
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        page = data.get("jobPostings", [])
        if data.get("total"):
            total = int(data["total"])

        for job in page:
            path = job.get("externalPath", "")
            job_id = str(job.get("jobReqId") or path or job.get("title"))
            jobs.append(
                (
                    job_id,
                    job.get("title", "Untitled"),
                    f"{career_url}{path}",
                    _location_text(
                        job.get("locationsText"),
                        job.get("location"),
                        job.get("bulletFields", [None])[0]
                        if job.get("bulletFields")
                        else None,
                    ),
                )
            )

        offset += len(page)
        if not page or (total is not None and offset >= total):
            break

    return jobs


def _local_name(tag):
    return tag.rsplit("}", 1)[-1].lower().replace("_", "-")


def _first_text(values, names):
    for name in names:
        value = values.get(name)
        if value:
            return value.strip()
    return ""


def _stable_job_id(title, url):
    return hashlib.sha256(f"{title}\0{url}".encode("utf-8")).hexdigest()[:24]


def _parse_xml_jobs(content):
    root = ElementTree.fromstring(content)
    candidate_names = {"item", "entry", "job", "requisition", "position"}
    candidates = [node for node in root.iter() if _local_name(node.tag) in candidate_names]
    if not candidates and _local_name(root.tag) in candidate_names:
        candidates = [root]

    jobs = []
    for node in candidates:
        values = {}
        for child in node.iter():
            name = _local_name(child.tag)
            text = "".join(child.itertext()).strip()
            if text and name not in values:
                values[name] = text
            if name == "link" and child.attrib.get("href"):
                values.setdefault("link", child.attrib["href"])

        title = _first_text(values, ("title", "job-title", "name"))
        url = _first_text(
            values,
            ("detail-url", "posting-url", "job-url", "link", "url", "apply-url"),
        )
        job_id = _first_text(
            values,
            ("id", "guid", "requisition-id", "job-id", "jobid", "reference-number"),
        )
        location = _location_text(
            *(
                values.get(name)
                for name in (
                    "location",
                    "job-location",
                    "location-name",
                    "city",
                    "region",
                    "state",
                    "country",
                    "workplace-type",
                )
            )
        )
        if title:
            jobs.append((job_id or _stable_job_id(title, url), title, url, location))
    return jobs


def _find_json_items(data):
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    for key in ("jobs", "results", "items", "content", "postings", "offers"):
        if isinstance(data.get(key), list):
            return data[key]
    return []


def _parse_json_jobs(data):
    jobs = []
    for item in _find_json_items(data):
        if not isinstance(item, dict):
            continue
        title = next(
            (item.get(key) for key in ("title", "name", "job_title", "jobTitle") if item.get(key)),
            "",
        )
        url = next(
            (
                item.get(key)
                for key in (
                    "url",
                    "link",
                    "job_url",
                    "jobUrl",
                    "posting_url",
                    "postingUrl",
                    "apply_url",
                    "applyUrl",
                    "redirect_url",
                )
                if item.get(key)
            ),
            "",
        )
        job_id = next(
            (
                item.get(key)
                for key in ("id", "job_id", "jobId", "guid", "requisition_id", "reference")
                if item.get(key) is not None
            ),
            "",
        )
        location = _location_text(
            *(
                item.get(key)
                for key in (
                    "location",
                    "locations",
                    "job_location",
                    "jobLocation",
                    "location_name",
                    "locationName",
                    "city",
                    "region",
                    "state",
                    "country",
                    "workplace_type",
                    "workplaceType",
                    "remote",
                    "isRemote",
                )
            )
        )
        if title:
            jobs.append(
                (str(job_id or _stable_job_id(title, url)), str(title), str(url), location)
            )
    return jobs


def _resolve_feed_url(source):
    url = _source_value(source, "feed_url")
    env_name = _source_value(source, "feed_url_env")
    if not url and env_name:
        url = os.environ.get(env_name)
    if not url:
        platform = _source_value(source, "platform") or _source_value(source, "ats")
        raise ValueError(
            f"{platform} requires an approved feed URL via feed_url or feed_url_env"
        )
    return url


def fetch_authorized_feed(source):
    """Read an approved RSS, Atom, XML, or JSON job feed without scraping pages."""
    url = _resolve_feed_url(source)
    try:
        response = requests.get(
            url,
            headers={"Accept": "application/json, application/xml, text/xml, */*", "User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"approved feed returned HTTP {response.status_code}")
    except requests.RequestException as exc:
        # Feed URLs can contain tokens, so do not print requests' URL-bearing exception.
        raise RuntimeError(f"approved feed request failed ({type(exc).__name__})") from None

    content_type = response.headers.get("Content-Type", "").lower()
    if "json" in content_type or response.content.lstrip().startswith((b"{", b"[")):
        return _parse_json_jobs(response.json())
    return _parse_xml_jobs(response.content)


def fetch_jobvite(source):
    """Read the customer-specific XML/JSON feed supplied by Jobvite."""
    return fetch_authorized_feed(source)


def _mcp_post(session, payload):
    response = session.post(
        "https://api.ziprecruiter.com/mcp",
        headers={
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        json=payload,
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def _ziprecruiter_arguments(source, schema):
    configured = dict(_source_value(source, "arguments", {}) or {})
    properties = schema.get("properties", {})

    aliases = {
        "query": ("query", "search", "keywords", "search_text", "searchText", "q"),
        "location": ("location", "where", "location_text", "locationText"),
        "radius": ("radius", "radius_miles", "radiusMiles"),
        "country": ("country", "country_code", "countryCode"),
        "offset": ("offset", "start"),
    }
    for source_key, candidates in aliases.items():
        value = _source_value(source, source_key)
        if value is None:
            continue
        schema_key = next((key for key in candidates if key in properties), None)
        if schema_key:
            configured.setdefault(schema_key, value)

    unsupported = sorted(set(configured) - set(properties))
    if unsupported:
        raise ValueError(
            "unsupported ZipRecruiter argument(s): " + ", ".join(unsupported)
        )
    missing = sorted(set(schema.get("required", [])) - set(configured))
    if missing:
        raise ValueError(
            "missing required ZipRecruiter argument(s): " + ", ".join(missing)
        )
    return configured


def _collect_job_dicts(value, found):
    if isinstance(value, list):
        for item in value:
            _collect_job_dicts(item, found)
        return
    if not isinstance(value, dict):
        return

    has_title = any(value.get(key) for key in ("title", "name", "job_title", "jobTitle"))
    has_url_or_id = any(
        value.get(key)
        for key in ("url", "link", "job_url", "jobUrl", "redirect_url", "id", "job_id", "jobId")
    )
    if has_title and has_url_or_id:
        found.append(value)
        return
    for child in value.values():
        _collect_job_dicts(child, found)


def _parse_mcp_jobs(result):
    candidates = []
    _collect_job_dicts(result.get("structuredContent", {}), candidates)

    if not candidates:
        for block in result.get("content", []):
            if block.get("type") != "text":
                continue
            try:
                parsed = json.loads(block.get("text", ""))
            except (TypeError, json.JSONDecodeError):
                continue
            _collect_job_dicts(parsed, candidates)

    return _parse_json_jobs({"jobs": candidates})


def fetch_ziprecruiter(source):
    """Search jobs through ZipRecruiter's official, unauthenticated MCP server."""
    session = requests.Session()
    _mcp_post(
        session,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "job-monitor", "version": "2.0"},
            },
        },
    )
    tools_response = _mcp_post(
        session, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    )
    tools = tools_response.get("result", {}).get("tools", [])
    search_tool = next((tool for tool in tools if tool.get("name") == "search_jobs"), None)
    if not search_tool:
        raise RuntimeError("ZipRecruiter MCP server did not advertise search_jobs")

    arguments = _ziprecruiter_arguments(source, search_tool.get("inputSchema", {}))
    call_response = _mcp_post(
        session,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "search_jobs", "arguments": arguments},
        },
    )
    result = call_response.get("result", {})
    if result.get("isError"):
        message = next(
            (block.get("text") for block in result.get("content", []) if block.get("type") == "text"),
            "ZipRecruiter search failed",
        )
        raise RuntimeError(message)
    return _parse_mcp_jobs(result)


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "workday": fetch_workday,
    "smartrecruiters": fetch_smartrecruiters,
    "workable": fetch_workable,
    "recruitee": fetch_recruitee,
    "jobvite": fetch_jobvite,
    "ziprecruiter": fetch_ziprecruiter,
    # These deliberately require an approved API/feed URL. Direct page scraping is not used.
    "linkedin": fetch_authorized_feed,
    "indeed": fetch_authorized_feed,
}


def _matches_title_terms(title, terms):
    return any(
        re.search(rf"(?<!\w){re.escape(str(keyword).strip())}(?!\w)", title, re.IGNORECASE)
        for keyword in terms
        if str(keyword).strip()
    )


def matches_keywords(title, keywords, exclude_keywords=None):
    if _matches_title_terms(title, exclude_keywords or []):
        return False
    return not keywords or _matches_title_terms(title, keywords)


def send_discord_notification(webhook_url, source_name, new_jobs):
    if not new_jobs:
        return

    total = len(new_jobs)
    for start in range(0, total, 10):
        chunk = new_jobs[start : start + 10]
        end = start + len(chunk)
        suffix = f" ({start + 1}-{end} of {total})" if total > 10 else ""
        lines = [f"**{total} new posting(s) from {source_name}{suffix}**"]
        for title, url in chunk:
            lines.append(f"- [{title}]({url})" if url else f"- {title}")

        response = requests.post(
            webhook_url, json={"content": "\n".join(lines)}, timeout=REQUEST_TIMEOUT
        )
        if response.status_code >= 300:
            raise RuntimeError(f"Discord webhook returned HTTP {response.status_code}")
        if end < total:
            time.sleep(0.5)


def _provider(source):
    return source.get("ats") or source.get("platform")


def _state_key(source, provider):
    identifier = (
        source.get("id")
        or source.get("slug")
        or source.get("tenant")
        or source.get("name")
    )
    if not identifier:
        raise ValueError(f"{provider} source requires id, slug, tenant, or name")
    return f"{provider}:{identifier}"


def _notified_state_key(state_key):
    return f"{state_key}:{NOTIFIED_STATE_SUFFIX}"


def main():
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        print("ERROR: DISCORD_WEBHOOK_URL environment variable not set.", file=sys.stderr)
        sys.exit(1)

    sources = load_config()
    state = load_state()
    any_errors = False

    for source in sources:
        if source.get("enabled", True) is False:
            continue

        provider = _provider(source)
        name = source.get("name", source.get("slug", "unknown"))
        keywords = source.get("keywords", [])
        exclude_keywords = source.get("exclude_keywords", [])
        location_filter = source.get("location_filter")
        fetcher = FETCHERS.get(provider)

        if not fetcher:
            print(f"WARN: unknown platform '{provider}' for {name}, skipping.", file=sys.stderr)
            any_errors = True
            continue

        try:
            state_key = _state_key(source, provider)
            notified_state_key = _notified_state_key(state_key)
            notified_ids = set(state.get(notified_state_key, []))
            jobs = fetcher(source)
        except Exception as exc:
            print(f"WARN: failed to fetch {name} ({provider}): {exc}", file=sys.stderr)
            any_errors = True
            continue

        current_ids = {job_id for job_id, _, _, _ in jobs}
        eligible_jobs = [
            (job_id, title, url)
            for job_id, title, url, location in jobs
            if matches_keywords(title, keywords, exclude_keywords)
            and matches_location(location, location_filter)
        ]
        new_jobs = [
            (title, url)
            for job_id, title, url in eligible_jobs
            if job_id not in notified_ids
        ]

        if new_jobs:
            print(f"{name}: {len(new_jobs)} new matching posting(s).")
            try:
                send_discord_notification(webhook_url, name, new_jobs)
            except Exception as exc:
                print(f"WARN: failed to notify for {name}: {exc}", file=sys.stderr)
                any_errors = True
            else:
                notified_ids.update(job_id for job_id, _, _ in eligible_jobs)
        else:
            print(f"{name}: no new matching postings.")

        # Keep a complete inventory for auditing while tracking delivered matches
        # separately. The v2 key creates one catch-up pass for eligible jobs that
        # the old all-jobs state marked seen before location/seniority fixes.
        state[state_key] = sorted(current_ids)
        state[notified_state_key] = sorted(notified_ids)
        time.sleep(float(source.get("request_delay", 1)))

    save_state(state)
    if any_errors:
        sys.exit(2)


if __name__ == "__main__":
    main()
