# Job Monitor

Job Monitor checks company career systems and approved job-search feeds, remembers
the postings it has already seen, and sends new title matches to Discord. It runs
on a GitHub Actions schedule, so no server is required.

## Supported platforms

| Platform | Access method | Extra setup |
| --- | --- | --- |
| Greenhouse | Public job-board API | Company slug |
| Lever | Public postings API | Company slug |
| Ashby | Public job-board API | Company slug |
| SmartRecruiters | Public Posting API | Company slug |
| Workable | Public careers endpoint | Account slug |
| Recruitee | Public Careers Site API | Careers subdomain |
| Workday | External career-site endpoint | Host, tenant, and site |
| Jobvite | Customer XML/JSON feed | Feed URL supplied by Jobvite |
| ZipRecruiter | Official job-search MCP endpoint | Search query; no secret |
| LinkedIn | Approved RSS/Atom/XML/JSON feed only | Approved feed URL |
| Indeed | Approved RSS/Atom/XML/JSON feed only | Approved feed URL |

LinkedIn and Indeed do not offer an open job-seeker search API. Job Monitor does
not scrape their pages; their adapters activate only when you have an authorized
feed or API export. This avoids account blocks, unstable HTML parsers, and terms
violations. ZipRecruiter's official search currently returns only a small result
page per request, so use focused queries.

The checked-in `companies.yaml` currently activates only sources that can run
through public company-career endpoints. Jobvite, LinkedIn, and Indeed are not
active because no approved feed URLs are configured. ZipRecruiter is not active
because its public MCP endpoint rate-limits the scheduled requests.

## One-time setup

1. Create a Discord webhook under **Server Settings → Integrations → Webhooks**.
2. Push this folder to a private or public GitHub repository.
3. Add a repository Actions secret named `DISCORD_WEBHOOK_URL` containing the
   webhook URL.
4. Edit `companies.yaml`, add the companies/searches you want, and change their
   `enabled` value to `true`.
5. Run **Actions → Job Monitor → Run workflow** once to verify the setup.

The first run treats every matching current posting as new. To establish a quiet
baseline, temporarily point the Discord secret at a test channel or let the first
run finish before relying on notifications.

## Company source configuration

Every company entry uses `ats`, `slug` or the provider-specific identifiers, and
an optional `keywords` list. Keywords match case-insensitive whole words or
phrases; an empty list matches every title. Broad career boards can use a tighter
early-career list so senior openings do not crowd the notification limit.

```yaml
companies:
  - name: "Example on SmartRecruiters"
    ats: smartrecruiters
    slug: "example-company"
    keywords: ["engineer", "developer"]

  - name: "Example on Workable"
    ats: workable
    slug: "example-company"
    keywords: []

  - name: "Example on Recruitee"
    ats: recruitee
    slug: "example-company"
    keywords: ["software"]
```

### Location filtering

Company ATS feeds usually provide location text rather than coordinates. Add a
`location_filter` to match city, state, or region phrases after jobs are fetched:

```yaml
location_filters:
  ny_nj: &ny_nj_location
    include_remote: true
    allow_unknown: false
    include:
      - "New York, NY"
      - "New York City"
      - "Brooklyn"
      - "New Jersey"
      - "NJ"

companies:
  - name: "Example"
    ats: greenhouse
    slug: "example"
    location_filter: *ny_nj_location
    keywords: ["engineer"]
```

`include_remote` keeps generic U.S.-remote roles, but rejects remote postings
tied to another state or country. `allow_unknown: false` rejects jobs whose feed
does not expose a location. The checked-in configuration expands this list with
NYC boroughs and nearby New York cities and applies it to every active source.
Because these feeds lack coordinates, the matcher approximates the NYC metro
area; ZipRecruiter searches use their native `location` and numeric `radius`
fields when that source is enabled.

### Workday

For a URL such as:

```text
https://acme.wd5.myworkdayjobs.com/External_Careers
```

open the career site in a browser and look for a request shaped like
`/wday/cxs/<tenant>/<site>/jobs`. Configure those values:

```yaml
  - name: "Acme"
    ats: workday
    host: "acme.wd5.myworkdayjobs.com"
    tenant: "acme"
    site: "External_Careers"
    search_text: "Software"
    keywords: ["engineer", "developer"]
```

Some employers disable third-party indexing. A disabled or private Workday site
cannot be monitored with this adapter. `search_text` is optional; use it on very
large boards to have Workday narrow the result set before title keywords are
applied locally.

### Jobvite

Jobvite provides each customer a job-feed URL. If the URL contains a key, store
the entire URL in the GitHub Actions secret `JOBVITE_FEED_URL`:

```yaml
  - name: "Acme on Jobvite"
    ats: jobvite
    feed_url_env: "JOBVITE_FEED_URL"
    keywords: ["engineer"]
```

The feed parser accepts RSS, Atom, common XML job feeds, and common JSON job-feed
shapes.

## Job-search configuration

Search entries live under the top-level `searches` key. Every search needs a
stable, unique `id`; changing it creates a new seen-job history.

### ZipRecruiter

```yaml
searches:
  - id: "zip-backend-philadelphia"
    name: "ZipRecruiter — backend near Philadelphia"
    platform: ziprecruiter
    query: "backend engineer"
    location: "Philadelphia, PA"
    radius: 25
    keywords: ["backend", "platform", "software"]
```

The adapter reads ZipRecruiter's advertised input schema at runtime. For filters
not represented by the simple fields above, place exact schema keys under
`arguments`:

```yaml
    arguments:
      employment_type: "full_time"
```

If the service changes a key, the run reports the unsupported argument rather
than silently ignoring it.

### LinkedIn and Indeed approved feeds

Add approved feed URLs as repository secrets named `LINKEDIN_FEED_URL` and
`INDEED_FEED_URL`, then enable the templates in `companies.yaml`.

```yaml
searches:
  - id: "linkedin-backend"
    name: "LinkedIn approved backend feed"
    platform: linkedin
    feed_url_env: "LINKEDIN_FEED_URL"
    keywords: ["backend", "platform"]
```

You can use `feed_url` instead of `feed_url_env` for a public URL that contains no
credentials. Do not commit signed URLs, tokens, or API keys.

## GitHub Actions secrets

The active workflow maps only `DISCORD_WEBHOOK_URL`, which is required. If you
later obtain an approved Jobvite, LinkedIn, or Indeed feed, add its URL as a
repository secret and map that secret under the workflow's `Run job monitor`
step before adding the source back to `companies.yaml`.

## Schedule and state

The default cron expression is `0 */3 * * *`, which runs every three hours in
UTC. Edit `.github/workflows/job-monitor.yml` to change it.

`state.json` stores every currently visible job ID for each source, including
titles that did not match your filters. The workflow commits this file after each
run, preventing an old posting from becoming "new" merely because you changed a
keyword.

Failed sources leave their previous state untouched. The process exits with code
2 after checking the remaining sources, making partial failures visible in the
Actions UI without losing successful results.

## Local verification

Install dependencies and run the fixture-based tests:

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

To execute a live run locally:

```bash
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
python job_monitor.py
```

In PowerShell, set the variable with:

```powershell
$env:DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/..."
python job_monitor.py
```
