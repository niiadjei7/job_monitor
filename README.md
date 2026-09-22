# Job Monitor

Job Monitor checks company career systems and approved job-search feeds, remembers
the postings it has already seen, and sends new early-career role matches to
Discord. Matches are tagged by specialty. It runs on a GitHub Actions schedule,
so no server is required.

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
4. Copy `profile.example.yaml` to `profile.yaml` and fill in your profile.
   `profile.yaml` is ignored so contact information is not published. Save the
   complete YAML document as a repository Actions secret named `PROFILE_YAML`
   so scheduled runs use the same scoring profile.
5. Edit `companies.yaml`, add the companies/searches you want, and change their
   `enabled` value to `true`.
6. Run **Actions → Job Monitor → Run workflow** once to verify the setup.

The first run treats every matching current posting as new. To establish a quiet
baseline, temporarily point the Discord secret at a test channel or let the first
run finish before relying on notifications.

## Company source configuration

Every checked-in company entry uses `ats`, provider-specific identifiers,
`keyword_categories`, and the shared early-career and exclusion filters. A
posting must match at least one specialty category **and** an early-career title
phrase, and it must not match a seniority exclusion. Terms are literal,
case-insensitive whole words or phrases; use `sre`, not regex such as `\bsre\b`.

Each category can define separate `title` and `context` phrases. Context is ATS
team or department metadata when available. This allows `Software Engineer I`
on an `Infrastructure` team to match without restoring generic standalone terms
such as `software` or `engineer`. A legacy flat `keywords` list remains supported
for custom feeds and is tagged `General`.

```yaml
source_defaults:
  early_career_keywords: ["new grad", "junior", "associate", "engineer i"]
  exclude_keywords: ["senior", "staff", "principal", "lead", "manager"]

companies:
  - name: "Example"
    ats: greenhouse
    slug: "example-company"
    keyword_categories:
      DevOps:
        title: ["devops", "site reliability", "sre", "platform engineer"]
        context: ["devops", "platform", "infrastructure"]
      Data Analytics:
        title: ["data analyst", "business intelligence", "analytics engineer"]
        context: ["data analytics", "business intelligence", "analytics"]
```

Discord lines include each matching category:

```text
- [DevOps] [Software Engineer I](https://example.test/job/1)
```

### Location filtering

Company ATS feeds usually provide location text rather than coordinates. Add a
`location_filter` to match city, state, or region phrases after jobs are fetched:

```yaml
location_filters:
  ny_nj: &ny_nj_location
    include_remote: true
    remote_include: ["United States", "USA", "US"]
    allow_unspecified_remote: true
    allow_unknown: false
    include:
      - "New York"
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

`remote_include` accepts remote postings that explicitly include a U.S. marker.
With `allow_unspecified_remote: true`, a bare `Remote` posting also passes unless
it names another state or foreign region. Explicitly located on-site or hybrid
roles must still match the NYC/NJ list. `allow_unknown: false` continues to
reject jobs whose feed exposes neither a location nor a remote signal. Because
most feeds lack coordinates, the matcher approximates the area; ZipRecruiter
uses its native numeric `radius` when enabled.

### Rejection diagnostics

Set `DEBUG_REJECTIONS=true` to print every ineligible posting with its score,
threshold, location, and all failed scoring gates. Each source also prints an
aggregate reason summary and separates previously notified matches from newly
eligible jobs. Debug mode does not change notification or state behavior.

For a manual Actions run, enable the `debug_rejections` input. To debug scheduled
runs, create a repository Actions variable named `DEBUG_REJECTIONS` with value
`true`, then disable it after collecting enough logs.

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
    keyword_categories:
      DevOps:
        title: ["devops", "platform engineer"]
        context: ["platform", "infrastructure"]
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

The active workflow requires both `DISCORD_WEBHOOK_URL` and `PROFILE_YAML`. It
materializes the profile only inside the temporary Actions runner, and fails
clearly when the profile secret is absent. If you later obtain an approved
Jobvite, LinkedIn, or Indeed feed, add its URL as a repository secret and map
that secret under the workflow's `Run job monitor` step before adding the source
back to `companies.yaml`.

## Schedule and state

The default cron expression is `0 */3 * * *`, which runs every three hours in
UTC. Edit `.github/workflows/job-monitor.yml` to change it.

`state.json` stores both the current source inventory and a separate set of job
IDs successfully delivered to Discord. This allows newly eligible jobs to be
reconsidered after a filter correction without resending jobs already delivered.
The `notified-v2` state starts with one catch-up pass after this migration. Large
catch-up sets are sent to Discord in complete chunks of ten rather than silently
omitting jobs beyond the first message.

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
