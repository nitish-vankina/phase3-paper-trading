# Deploying Phase 3 for free

## What you're deploying

Two free pieces, no paid Render service, no server at all:

| Piece | Where it runs | Job |
|---|---|---|
| **GitHub Actions workflow** | GitHub (free) | Once a day after market close, pulls prices, rebalances the paper portfolio, and commits `paper_state.json` + `dashboard_data.json` back to your repo. |
| **Static site** | Render free tier | Just serves `dashboard.html` + `dashboard_data.json` as static files. Redeploys automatically every time the Action pushes a commit. |

There's no backend and no persistent disk to pay for — GitHub itself is the "database" (the state file lives in your repo's git history), and Render's free static sites don't spin down or expire. GitHub Actions is free for public repos; private repos get 2,000 free minutes/month and this job takes well under a minute a day.

## 1. Put the files in a GitHub repo

You need these at the root of a repo, plus the workflow file in `.github/workflows/`:

```
phase3_paper_trader.py
dashboard.html
requirements.txt
.github/workflows/daily-run.yml
```

```bash
mkdir phase3-paper-trading && cd phase3-paper-trading
# copy the files above in here, preserving the .github/workflows/ path
git init
git add .
git commit -m "Phase 3 paper trading — dashboard + engine"
git branch -M main
git remote add origin https://github.com/<you>/phase3-paper-trading.git
git push -u origin main
```

If your repo is **private**, go to the repo's **Settings → Actions → General → Workflow permissions** and make sure "Read and write permissions" is selected — otherwise the Action can't push its daily commit back. Public repos usually have this on by default.

## 2. Run the workflow once by hand

Don't wait for tonight's schedule — trigger it yourself first to make sure everything works:

1. Go to your repo on GitHub → **Actions** tab → **Daily paper trading run** (in the left sidebar) → **Run workflow** button → **Run workflow**.
2. Watch it run (takes ~30-60 seconds). If it succeeds, check your repo — you should see a new commit like "Daily paper trading update 2026-07-21" adding `paper_state.json` and `dashboard_data.json`.

If it fails, click into the failed run's logs — the most common cause is `yfinance` occasionally hiccupping on a ticker lookup; re-running usually fixes it.

## 3. Deploy the static site on Render

1. [Render Dashboard](https://dashboard.render.com) → **New** → **Static Site**.
2. Connect the GitHub repo.
3. Leave **Build Command** blank (or `echo "no build needed"`) and set **Publish Directory** to `.` (repo root).
4. Click **Create Static Site**. Render gives you a URL like `https://phase3-paper-trading.onrender.com` — open it.

That's the whole setup. No environment variables, no secrets, nothing else to configure.

## 4. Keeping it running

- The workflow re-runs automatically every weekday at 21:30 UTC (4:30pm ET winter / 5:30pm ET summer — always after the 4pm close). Adjust the `cron:` line in `.github/workflows/daily-run.yml` if you want a different time — [crontab.guru](https://crontab.guru) helps build the expression.
- Every push to `main` — whether from the daily job or from you editing code — makes Render redeploy the static site automatically, usually within a few seconds.
- The dashboard auto-refreshes every 5 minutes and has a manual **↻ Refresh** button, both of which just re-fetch `dashboard_data.json` with a cache-busting query param.
- **Import JSON** / **Load demo data** still work for testing — they're clearly labeled overrides that don't touch the live file.
- The **Manual Journal** tab saves to that browser's local storage — per-browser, not shared across devices.

## 5. One tradeoff worth knowing

Because state lives in git, there is a small window (a few seconds between the Action's commit and Render's redeploy finishing) where the site might serve the previous day's data. That's harmless for a once-a-day paper trading update. It also means `paper_state.json`'s full history lives in your repo's commit log — fine for a personal project, just don't put a private repo's contents somewhere public if you'd rather not share your trade history.

## 6. Testing locally (optional)

```bash
pip install -r requirements.txt
python phase3_paper_trader.py run
python -m http.server 8000
```

Then open `http://localhost:8000/dashboard.html` — it'll read the `dashboard_data.json` the script just wrote, right there in the same folder.
