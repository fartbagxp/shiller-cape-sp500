# S&P 500 Shiller CAPE Dashboard

A self-updating GitHub Pages site that tracks the Shiller CAPE (Cyclically Adjusted
Price-to-Earnings) ratio against the S&P 500 index, with a full price-sensitivity
table showing what the CAPE would be at various market levels.

[Live Site](https://fartbagxp.github.io/shiller-cape-sp500)

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

| Data point             | Primary source             | Fallback                |
| ---------------------- | -------------------------- | ----------------------- |
| Shiller CAPE           | multpl.com/shiller-pe      | —                       |
| S&P 500 price          | multpl.com/s-p-500-value   | Yahoo Finance (^GSPC)   |
| Daily closes (≤10 yr)  | FRED (series `SP500`)      | Yahoo Finance chart API |
| Monthly levels (>10 yr)| multpl historical prices   | —                       |

FRED serves 10 years of daily closes with no API key or rate limit, so it leads the
history chain. It does stall on a browser `User-Agent`, hence the separate
`FRED_HEADERS` in `fetch_cape.py`.

Nothing free supplies daily S&P 500 closes past ten years — Yahoo rate-limits hard and
Stooq now sits behind a JS challenge — so years 10–20 come from multpl's monthly table.
Those are Shiller's **monthly averages** of daily closes, not month-end closes, so a
peak found in that span is accurate to the month and its level is an average rather
than a true high. Points and peaks from it are tagged `monthly`, the chart footnotes it,
and the peak table badges those rows.

Shiller PE data is sourced from Robert Shiller's dataset as published at multpl.com.

---

## Peak tracking

Record highs used to erase the previous "Peak" reading: the peak row is derived from
the price series, so a new high overwrote it and the level that came before was gone.
The dashboard now keeps the whole progression instead.

`fetch_cape.py` pulls twenty years of price history and runs a zig-zag scan over it
(`find_peaks`). A running high is banked as a peak only once price falls a given
percentage below it, so a long rally produces one peak rather than one per record-high
day. Each peak carries its date, level, the pullback that followed, and the date price
first closed back above it.

The scan runs once per chart range, because one threshold cannot serve every window —
a 3% rule over twenty years banks a hundred-odd peaks. `PEAK_THRESHOLDS` widens it with
the window:

| Range | Pullback required |
| ----- | ----------------- |
| 2Y    | 3%                |
| 5Y    | 5%                |
| 10Y   | 7%                |
| 20Y   | 10%               |

Peaks are always found on the full-resolution series and only then is the stored chart
series thinned (`CHART_STRIDES`, denser in recent years), so marker dates stay exact
while `data.json` carries ~1,400 points instead of ~2,600.

Two flags separate the kinds of peak:

- `record_high` — the close cleared every close before it **within the 2-year window**.
  These are the milestones a new high used to overwrite.
- `in_progress` — the high of the episode still running. On a record-high day this is
  today, which is exactly the case that used to leave the table with nothing to show.

The dashboard renders this as a chart of closes with peaks marked (record highs filled,
interim tops hollow, the window high on a dashed guide line) plus a dated peak table
that defaults to record highs and expands to every detected peak. When today is the
window high, the peak row is folded into the "Today" row instead of duplicating it.

**2Y / 5Y / 10Y / 20Y** buttons switch the window; the peak table and thresholds follow.
A **LOG** toggle sits beside them and turns itself on at 10Y and above, where a linear
axis flattens the 2008–09 crash into a line along the bottom. Both the axis and the
peak markers are positioned by date rather than by index, so the thinned older data
stays correctly spaced.

Tuning: `HISTORY_YEARS` sets the total lookback, `DAILY_YEARS` how far the daily source
reaches, `PEAK_THRESHOLDS` which ranges appear and how selective each scan is, and
`DEFAULT_RANGE` which one the chart opens on. A range is only offered if the fetched
history actually covers 80% of it, so the 20Y button disappears rather than lying if
multpl is unreachable.

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
