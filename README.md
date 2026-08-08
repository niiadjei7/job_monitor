# Job Monitor

Watches Greenhouse / Lever / Ashby job boards for companies you list, and
pings a Discord channel when a new matching posting appears. Runs for free
on a schedule via GitHub Actions — no server required.

## What it does NOT do (by design)

- **No Indeed scraping.** Indeed's ToS prohibits scraping and actively
  blocks bots. This tool only hits Greenhouse/Lever/Ashby's public,
  unauthenticated JSON APIs — the same data their own career pages load
  in the browser, just fetched directly.
- **No GitHub repo job-posting search yet.** That's a reasonable v2
  addition (searching repo READMEs/issues for "we're hiring") but wasn't
  included here to keep v1 simple and reliable. Ask if you want it added.

## One-time setup

### 1. Create a Discord webhook
In your Discord server: **Server Settings → Integrations → Webhooks →
New Webhook**. Pick the channel you want pings in, copy the Webhook URL.

### 2. Create a GitHub repo
Push this folder to a new **private** GitHub repo (private is fine —
GitHub Actions works the same either way, and you don't need this public).

```bash
cd job-monitor
git init
git add .
git commit -m "Initial job monitor setup"
git branch -M main
git remote add origin https://github.com/<you>/job-monitor.git
git push -u origin main
```

### 3. Add the Discord webhook as a repo secret
In the GitHub repo: **Settings → Secrets and variables → Actions →
New repository secret**.
- Name: `DISCORD_WEBHOOK_URL`
- Value: the webhook URL from step 1

### 4. Edit `companies.yaml`
Replace the example entries with real companies. For each one you need:
- Which ATS they use (Greenhouse / Lever / Ashby) — check their careers
  page URL, it usually gives it away (see comments in the file).
- Their `slug` on that platform.
- Optional `keywords` to filter titles (leave `[]` to get every posting).

Commit and push the change.

### 5. Test it manually
In the repo: **Actions tab → Job Monitor → Run workflow**. This triggers
it immediately instead of waiting for the schedule, so you can confirm
the Discord ping works before trusting the cron schedule.

## Adjusting the schedule

Edit the `cron:` line in `.github/workflows/job-monitor.yml`. It's
standard 5-field cron syntax, in UTC. Examples:
- `"0 */3 * * *"` — every 3 hours (default)
- `"0 8,20 * * *"` — twice a day, 8am and 8pm UTC
- `"0 9 * * 1-5"` — once a day, weekdays only, 9am UTC

## How "new" is determined

The first run will see every current posting as "new" (since `state.json`
starts empty) and will likely fire a large batch of Discord messages.
That's expected — after that first run, only genuinely new postings will
trigger a ping.
