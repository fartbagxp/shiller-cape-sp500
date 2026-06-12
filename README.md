# S&P 500 Shiller CAPE Dashboard

A self-updating GitHub Pages site that tracks the Shiller CAPE (Cyclically Adjusted
Price-to-Earnings) ratio against the S&P 500 index, with a full price-sensitivity
table showing what the CAPE would be at various market levels.

## Live site

After deploying: `https://<your-username>.github.io/<repo-name>/`

---

## How it works

```bash
fetch_cape.py          →  data.json        →  index.html
(scrapes multpl.com)      (written to disk)   (reads at page load)
```

1. The GitHub Actions workflow runs on a schedule (weekdays at ~4:30 PM ET) and
   on every push to `main`.
2. `fetch_cape.py` scrapes the live CAPE ratio and S&P 500 price, computes the
   sensitivity table, and writes `data.json`.
3. The workflow deploys `index.html` + `data.json` to the `gh-pages` branch via
   `peaceiris/actions-gh-pages`.
4. `index.html` fetches `data.json` client-side and renders the dashboard.

---

## Local development

**Prerequisites:** Python 3.11+, `uv` (recommended)

```bash
# Install dependencies
uv sync

# Fetch live data
uv run python fetch_cape.py

# Serve locally (any static server)
uv run python -m http.server 8080
# then open http://localhost:8080
```

---

## GitHub Pages setup (one-time)

1. Push this repo to GitHub.
2. Go to **Settings → Pages → Source** and select **Deploy from a branch** → `gh-pages`.
3. The first workflow run (triggered by the push) will create the `gh-pages` branch
   and deploy the site.

> **Note:** The `GITHUB_TOKEN` used in the workflow has `contents: write` permission
> by default for Actions. No additional secrets are required.

---

## Data sources

| Data point    | Primary source           | Fallback              |
| ------------- | ------------------------ | --------------------- |
| Shiller CAPE  | multpl.com/shiller-pe    | —                     |
| S&P 500 price | multpl.com/s-p-500-value | Yahoo Finance (^GSPC) |

Shiller PE data is sourced from Robert Shiller's dataset as published at multpl.com.

---

## Customizing the schedule

Edit `.github/workflows/deploy.yml`:

```yaml
on:
  schedule:
    - cron: '30 21 * * 1-5' # Mon–Fri 21:30 UTC = 4:30 PM ET
```

---

## File structure

```bash
.
├── .github/
│   └── workflows/
│       └── deploy.yml      # CI/CD: fetch → build → deploy
├── fetch_cape.py           # Data fetcher (writes data.json)
├── index.html              # Dashboard UI (reads data.json at runtime)
├── requirements.txt        # Python deps
└── README.md
```
