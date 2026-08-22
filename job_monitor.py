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


def load_config():
    if not CONFIG_PATH.exists():
        print(f"ERROR: {CONFIG_PATH} not found.", file=sys.stderr)
        sys.exit(1)

    with open(CONFIG_PATH, encoding="utf-8") as config_file:
        data = yaml.safe_load(config_file) or {}

    sources = data.get("companies", []) + data.get("searches", [])
    if not isinstance(sources, list):
        raise ValueError("companies and searches must both be YAML lists")
    return sources


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
    """Return (job_id, title, URL) tuples for a Greenhouse board."""
    slug = _source_value(source, "slug")
    data = _get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    return [
        (str(job["id"]), job.get("title", "Untitled"), job.get("absolute_url", ""))
        for job in data.get("jobs", [])
    ]


def fetch_lever(source):
    """Return jobs for a Lever board."""
    slug = _source_value(source, "slug")
    jobs = _get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    return [
        (str(job["id"]), job.get("text", "Untitled"), job.get("hostedUrl", ""))
        for job in jobs
    ]


def fetch_ashby(source):
    """Return jobs for an Ashby board."""
    slug = _source_value(source, "slug")
    data = _get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    return [
        (str(job["id"]), job.get("title", "Untitled"), job.get("jobUrl", ""))
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
            jobs.append((job_id, title, url))

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
        jobs.append((job_id, title, url))
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
            jobs.append((job_id, job.get("title", "Untitled"), f"{career_url}{path}"))

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
        if title:
            jobs.append((job_id or _stable_job_id(title, url), title, url))
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
        if title:
            jobs.append((str(job_id or _stable_job_id(title, url)), str(title), str(url)))
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


def matches_keywords(title, keywords):
    if not keywords:
        return True
    return any(
        re.search(rf"(?<!\w){re.escape(str(keyword).strip())}(?!\w)", title, re.IGNORECASE)
        for keyword in keywords
        if str(keyword).strip()
    )


def send_discord_notification(webhook_url, source_name, new_jobs):
    if not new_jobs:
        return

    lines = [f"**{len(new_jobs)} new posting(s) from {source_name}**"]
    for title, url in new_jobs[:10]:
        lines.append(f"- [{title}]({url})" if url else f"- {title}")
    if len(new_jobs) > 10:
        lines.append(f"...and {len(new_jobs) - 10} more.")

    response = requests.post(
        webhook_url, json={"content": "\n".join(lines)}, timeout=REQUEST_TIMEOUT
    )
    if response.status_code >= 300:
        print(
            f"WARN: Discord webhook returned {response.status_code}: {response.text}",
            file=sys.stderr,
        )


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
        fetcher = FETCHERS.get(provider)

        if not fetcher:
            print(f"WARN: unknown platform '{provider}' for {name}, skipping.", file=sys.stderr)
            any_errors = True
            continue

        try:
            state_key = _state_key(source, provider)
            seen_ids = set(state.get(state_key, []))
            jobs = fetcher(source)
        except Exception as exc:
            print(f"WARN: failed to fetch {name} ({provider}): {exc}", file=sys.stderr)
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

        # Store every current ID, including non-matches, so changing keywords later
        # does not re-notify for jobs that were already live.
        state[state_key] = sorted(current_ids)
        time.sleep(float(source.get("request_delay", 1)))

    save_state(state)
    if any_errors:
        sys.exit(2)


if __name__ == "__main__":
    main()
