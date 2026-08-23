"""
fetch_cape.py

Fetches live Shiller CAPE ratio and S&P 500 price, then writes data.json
for the GitHub Pages dashboard.

Sources:
  - CAPE ratio: multpl.com/shiller-pe  (Robert Shiller's official data series)
  - S&P 500:    multpl.com/s-p-500-historical-annual-returns (or Yahoo Finance fallback)

Usage:
  uv run fetch_cape.py
  python fetch_cape.py

Dependencies (see requirements.txt):
  httpx, beautifulsoup4, lxml
"""

import csv
import io
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

HEADERS = {
  "User-Agent": (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
  ),
  "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
  "Accept-Language": "en-US,en;q=0.9",
}

FRED_HEADERS = {
  "User-Agent": "shiller-cape-sp500 (github actions data fetcher)",
  "Accept": "text/csv,*/*;q=0.8",
}

LONG_RUN_AVG_CAPE  = 17.3   # ~140-year average per Shiller's dataset
RECENT_20Y_AVG_CAPE = 27.6  # 20-year average per GuruFocus
DOT_COM_PEAK_CAPE  = 44.19  # December 1999 peak


def fetch_cape() -> float:
  """Scrape current Shiller CAPE ratio from multpl.com."""
  url = "https://www.multpl.com/shiller-pe"
  resp = httpx.get(url, headers=HEADERS, timeout=15, follow_redirects=True)
  resp.raise_for_status()
  soup = BeautifulSoup(resp.text, "lxml")

  # multpl.com puts the current value in <div id="current">
  el = soup.find("div", id="current")
  if not el:
    raise ValueError("Could not find #current element on multpl.com/shiller-pe")

  text = el.get_text(strip=True)
  # Strip any trailing footnote chars, commas, etc.
  match = re.search(r"[\d,]+\.?\d*", text)
  if not match:
    raise ValueError(f"Could not parse CAPE value from: {text!r}")

  return float(match.group().replace(",", ""))


def fetch_sp500() -> float:
  """Scrape current S&P 500 price from multpl.com."""
  url = "https://www.multpl.com/s-p-500-historical-annual-returns"
  resp = httpx.get(url, headers=HEADERS, timeout=15, follow_redirects=True)
  resp.raise_for_status()
  soup = BeautifulSoup(resp.text, "lxml")

  el = soup.find("div", id="current")
  if not el:
    raise ValueError("Could not find #current on multpl.com SP500 page")

  text = el.get_text(strip=True)
  match = re.search(r"[\d,]+\.?\d*", text)
  if not match:
    raise ValueError(f"Could not parse SP500 value from: {text!r}")

  raw = float(match.group().replace(",", ""))
  # multpl SP500 returns page sometimes shows percentage; if looks like a %, fall through
  if raw < 500:
    raise ValueError(f"SP500 value looks wrong ({raw}), trying fallback")
  return raw


def fetch_sp500_yahoo() -> dict:
  """
  Scrape S&P 500 quote page.  Returns dict with current price and 52-week high.
  Parsing both from the same request avoids a second Yahoo call (rate-limit risk).
  """
  url = "https://finance.yahoo.com/quote/%5EGSPC/"
  resp = httpx.get(url, headers=HEADERS, timeout=15, follow_redirects=True)
  resp.raise_for_status()
  soup = BeautifulSoup(resp.text, "lxml")

  # Current price
  sp500 = None
  el = soup.find("fin-streamer", {"data-symbol": "^GSPC", "data-field": "regularMarketPrice"})
  if el and el.get("value"):
    sp500 = float(el["value"].replace(",", ""))

  if sp500 is None:
    el = soup.find("span", {"data-testid": "qsp-price"})
    if el:
      sp500 = float(el.get_text(strip=True).replace(",", ""))

  if sp500 is None:
    raise ValueError("Could not parse SP500 price from Yahoo Finance")

  # 52-week high from the range table  e.g. "52 Week Range5,943.23 - 7,620.90"
  high_52w = None
  for el in soup.find_all("li"):
    text = el.get_text(strip=True)
    m = re.match(r"52 Week Range[\s\d,.]+-\s*([\d,]+\.?\d*)", text)
    if m:
      high_52w = float(m.group(1).replace(",", ""))
      break

  return {"sp500": sp500, "high_52w": high_52w}


def fetch_sp500_multpl_direct() -> float:
  """Try the dedicated S&P 500 price page on multpl."""
  url = "https://www.multpl.com/s-p-500-value"
  resp = httpx.get(url, headers=HEADERS, timeout=15, follow_redirects=True)
  resp.raise_for_status()
  soup = BeautifulSoup(resp.text, "lxml")

  el = soup.find("div", id="current")
  if not el:
    raise ValueError("No #current on multpl SP500 value page")

  text = el.get_text(strip=True)
  match = re.search(r"[\d,]+\.?\d*", text)
  if not match:
    raise ValueError(f"Could not parse from: {text!r}")

  return float(match.group().replace(",", ""))


RECENT_PEAK_WINDOW_DAYS = 30


def fetch_sp500_stooq_peak(window_days: int = RECENT_PEAK_WINDOW_DAYS) -> dict:
  """
  Fetch recent daily closes from stooq.com (no API key, no rate limits shared with Yahoo).
  Returns the peak close and date within the window.
  """
  url = "https://stooq.com/q/d/l/?s=%5Espx&i=d"
  resp = httpx.get(url, headers=HEADERS, timeout=15, follow_redirects=True)
  resp.raise_for_status()

  reader = csv.DictReader(io.StringIO(resp.text))
  rows = list(reader)
  if not rows:
    raise ValueError("stooq returned empty CSV")

  cutoff = datetime.now(timezone.utc).timestamp() - window_days * 86400
  peak_close = None
  peak_date  = None

  for row in rows:
    try:
      date_str = row.get("Date") or row.get("date") or list(row.values())[0]
      close    = float(row.get("Close") or row.get("close") or list(row.values())[4])
      dt       = datetime.strptime(date_str.strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (ValueError, KeyError, IndexError):
      continue
    if dt.timestamp() < cutoff:
      continue
    if peak_close is None or close > peak_close:
      peak_close = close
      peak_date  = date_str.strip()

  if peak_close is None:
    raise ValueError(f"No SP500 data within last {window_days} days from stooq")

  return {
    "sp500": None,  # stooq data may lag; don't use as current price
    "peak": {
      "peak_sp500":  round(peak_close, 2),
      "peak_date":   peak_date,
      "window_days": window_days,
    },
  }


def fetch_sp500_yahoo_chart(window_days: int = RECENT_PEAK_WINDOW_DAYS) -> dict:
  """
  Single Yahoo Finance chart API call returning both current price and recent peak.
  Avoids two separate Yahoo requests (which triggers 429 rate-limits).
  """
  time.sleep(2)  # brief pause after Yahoo HTML scrape to avoid 429
  url = (
    f"https://query2.finance.yahoo.com/v8/finance/chart/%5EGSPC"
    f"?interval=1d&range={window_days}d"
  )
  resp = httpx.get(url, headers=HEADERS, timeout=15, follow_redirects=True)
  resp.raise_for_status()
  body = resp.json()

  result     = body["chart"]["result"][0]
  closes     = result["indicators"]["quote"][0]["close"]
  timestamps = result["timestamp"]

  last_close = None
  peak_close = None
  peak_ts    = None
  for ts, close in zip(timestamps, closes):
    if close is None:
      continue
    last_close = close
    if peak_close is None or close > peak_close:
      peak_close = close
      peak_ts    = ts

  if last_close is None:
    raise ValueError("No close prices returned from Yahoo Finance chart API")

  peak_date = datetime.fromtimestamp(peak_ts, tz=timezone.utc).strftime("%Y-%m-%d")
  return {
    "sp500": round(last_close, 2),
    "peak": {
      "peak_sp500":  round(peak_close, 2),
      "peak_date":   peak_date,
      "window_days": window_days,
    },
  }


HISTORY_YEARS = 20            # how far back the fetcher pulls price history
DAILY_YEARS   = 10            # FRED's SP500 series only reaches back this far
HISTORY_RANGE = f"{DAILY_YEARS}y"  # daily window, in Yahoo chart-API syntax
DEFAULT_RANGE = 2             # range the chart opens on

# Chart ranges offered in the UI, each with the pullback that has to follow a
# running high before it counts as a peak.  A 3% rule over twenty years would
# bank a hundred-odd peaks, so the threshold widens with the window.
PEAK_THRESHOLDS = {2: 3.0, 5: 5.0, 10: 7.0, 20: 10.0}
CHART_RANGES    = sorted(PEAK_THRESHOLDS)
PEAK_MIN_DRAWDOWN_PCT = PEAK_THRESHOLDS[DEFAULT_RANGE]


def fetch_sp500_history_fred(years: float = DAILY_YEARS) -> list[dict]:
  """
  Daily closes from FRED (series SP500) — no API key, no rate limiting, which is
  why it leads the history chain instead of Yahoo.  FRED marks market holidays
  with "." and only keeps 10 years, both of which are fine here.
  """
  url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=SP500"
  # FRED stalls on the browser User-Agent the scrapers use, so it gets its own.
  resp = httpx.get(url, headers=FRED_HEADERS, timeout=30, follow_redirects=True)
  resp.raise_for_status()

  cutoff  = datetime.now(timezone.utc).timestamp() - years * 365.25 * 86400
  history = []
  for row in csv.DictReader(io.StringIO(resp.text)):
    date_str = (row.get("observation_date") or "").strip()
    raw      = (row.get("SP500") or "").strip()
    if not date_str or raw in ("", "."):
      continue
    try:
      dt    = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
      close = float(raw)
    except ValueError:
      continue
    if dt.timestamp() < cutoff:
      continue
    history.append({"date": date_str, "close": round(close, 2)})

  if not history:
    raise ValueError("FRED returned no usable SP500 closes")
  return history


def fetch_sp500_history_yahoo(range_: str = HISTORY_RANGE) -> list[dict]:
  """
  Daily closes for the peak chart, via the Yahoo Finance chart API.
  Returns [{"date": "YYYY-MM-DD", "close": float}, ...] oldest first.
  """
  url = (
    f"https://query2.finance.yahoo.com/v8/finance/chart/%5EGSPC"
    f"?interval=1d&range={range_}"
  )
  resp = httpx.get(url, headers=HEADERS, timeout=20, follow_redirects=True)
  resp.raise_for_status()
  body = resp.json()

  result     = body["chart"]["result"][0]
  closes     = result["indicators"]["quote"][0]["close"]
  timestamps = result["timestamp"]

  history = []
  for ts, close in zip(timestamps, closes):
    if close is None:
      continue
    history.append({
      "date":  datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d"),
      "close": round(close, 2),
    })

  if not history:
    raise ValueError("Yahoo chart API returned no closes for the history range")
  return history


def find_peaks(history: list[dict], min_drawdown_pct: float = PEAK_MIN_DRAWDOWN_PCT) -> list[dict]:
  """
  Zig-zag peak detection: walk the series tracking a running high, and bank that
  high as a peak once price falls `min_drawdown_pct` below it.  A fresh episode
  then starts from the pullback, so a long grinding rally yields one peak rather
  than one per record-high day.  The final running high is always included — at a
  record high that peak is today, which is exactly the case the chart has to show.

  Each peak carries the drawdown that followed it and the date price first got
  back above it (null if it never did).
  """
  if not history:
    return []

  peak_idx  = 0
  peak_idxs = []
  for i in range(1, len(history)):
    close = history[i]["close"]
    high  = history[peak_idx]["close"]
    if close > high:
      peak_idx = i
    elif (high - close) / high * 100 >= min_drawdown_pct:
      peak_idxs.append(peak_idx)
      peak_idx = i  # new episode starts from the pullback
  peak_idxs.append(peak_idx)

  today       = datetime.now(timezone.utc).date()
  window_high = max(r["close"] for r in history)
  peaks       = []

  for n, pi in enumerate(peak_idxs):
    peak_close = history[pi]["close"]
    peak_date  = history[pi]["date"]

    # Trough between this peak and the next one (or the end of the series).
    end     = peak_idxs[n + 1] if n + 1 < len(peak_idxs) else len(history) - 1
    segment = history[pi:end + 1]
    trough  = min(segment, key=lambda r: r["close"])
    drawdown_pct = round((peak_close - trough["close"]) / peak_close * 100, 1)

    recovered_on = next(
      (r["date"] for r in history[pi + 1:] if r["close"] >= peak_close),
      None,
    )

    days_ago = (today - datetime.strptime(peak_date, "%Y-%m-%d").date()).days
    # A peak that cleared every close before it was a record high on the day —
    # that is the milestone worth dating, and the one a later record overwrites.
    record_high = peak_close >= max(r["close"] for r in history[:pi + 1])
    peaks.append({
      "date":          peak_date,
      "sp500":         peak_close,
      "drawdown_pct":  drawdown_pct,
      "trough_sp500":  trough["close"],
      "trough_date":   trough["date"],
      "recovered_on":  recovered_on,
      "days_ago":      days_ago,
      "record_high":   record_high,
      "window_high":   peak_close >= window_high,
      # The last entry is the high of the episode still in progress — the level
      # price has to clear next, not a pullback that has already played out.
      "in_progress":   n == len(peak_idxs) - 1,
    })

  return peaks


def fetch_sp500_history_chained() -> list[dict]:
  """Daily closes from the first history source that answers."""
  errors = []
  for fetcher, name in [
    (fetch_sp500_history_fred,  "FRED"),
    (fetch_sp500_history_yahoo, "Yahoo chart"),
  ]:
    try:
      history = fetcher()
      print(f"  History source: {name}")
      return history
    except Exception as exc:
      errors.append(f"{name}: {exc}")
  raise RuntimeError("; ".join(errors))


def fetch_sp500_history_multpl(years: float = HISTORY_YEARS) -> list[dict]:
  """
  Monthly S&P 500 levels from multpl, which reach back to 1871 — the only free
  source here that covers more than ten years.

  These are Shiller's *monthly averages* of daily closes, not month-end closes,
  so a peak found in this segment is accurate to the month and its level is an
  average rather than a true high.  Points are tagged `monthly` so the dashboard
  can say so rather than implying a precision the data does not have.
  """
  url = "https://www.multpl.com/s-p-500-historical-prices/table/by-month"
  resp = httpx.get(url, headers=HEADERS, timeout=30, follow_redirects=True)
  resp.raise_for_status()
  soup = BeautifulSoup(resp.text, "lxml")

  cutoff  = datetime.now(timezone.utc).timestamp() - years * 365.25 * 86400
  history = []
  for tr in soup.find_all("tr"):
    cells = tr.find_all("td")
    if len(cells) < 2:
      continue
    try:
      dt    = datetime.strptime(cells[0].get_text(strip=True), "%b %d, %Y").replace(tzinfo=timezone.utc)
      close = float(re.sub(r"[^\d.]", "", cells[1].get_text(strip=True)))
    except ValueError:
      continue
    if dt.timestamp() < cutoff:
      continue
    history.append({"date": dt.strftime("%Y-%m-%d"), "close": round(close, 2), "monthly": True})

  if not history:
    raise ValueError("multpl returned no usable monthly SP500 levels")
  history.sort(key=lambda r: r["date"])
  return history


def build_history() -> dict:
  """
  Assemble the price series the chart draws from: daily closes for as far back as
  a daily source reaches, with multpl's monthly averages filling the years before
  that.  Returns the merged series plus the date the daily data starts, which is
  the boundary the UI warns about.
  """
  daily = fetch_sp500_history_chained()
  daily_from = daily[0]["date"]

  older = []
  try:
    monthly = fetch_sp500_history_multpl()
    older   = [r for r in monthly if r["date"] < daily_from]
    if older:
      print(f"  Monthly fill: {len(older)} points ({older[0]['date']} → {older[-1]['date']}, multpl averages)")
  except Exception as exc:
    print(f"  [multpl monthly history] failed: {exc}")

  series = older + daily
  return {
    "series":     series,
    "daily_from": daily_from if older else None,
    "years":      years_spanned(series),
  }


def years_spanned(series: list[dict]) -> float:
  """Length of a series in years, for deciding which chart ranges have data."""
  if len(series) < 2:
    return 0.0
  first = datetime.strptime(series[0]["date"],  "%Y-%m-%d")
  last  = datetime.strptime(series[-1]["date"], "%Y-%m-%d")
  return (last - first).days / 365.25


def slice_years(series: list[dict], years: float) -> list[dict]:
  """The tail of a series covering the last `years` years."""
  cutoff = datetime.now(timezone.utc).timestamp() - years * 365.25 * 86400
  return [
    r for r in series
    if datetime.strptime(r["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() >= cutoff
  ]


# How densely the chart series is stored, by age of the point.  Peaks are found
# on the full-resolution series first, so thinning here costs marker precision
# nothing — it only keeps data.json from carrying 2,600 daily closes.
CHART_STRIDES = [(2, 1), (5, 2), (10, 3)]


def downsample_for_chart(series: list[dict], keep_dates: set[str]) -> list[dict]:
  """
  Thin the stored series with age, always keeping the endpoints and every peak
  so markers land exactly on the drawn line.
  """
  now  = datetime.now(timezone.utc).timestamp()
  out  = []
  for i, row in enumerate(series):
    age = (now - datetime.strptime(row["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()) / (365.25 * 86400)
    stride = next((s for yrs, s in CHART_STRIDES if age <= yrs), 1)
    if i == 0 or i == len(series) - 1 or row["date"] in keep_dates or i % stride == 0:
      out.append(row)
  return out


def peaks_by_range(series: list[dict], daily_from: str | None) -> dict:
  """
  Run the peak scan once per chart range, each with its own pullback threshold.
  Peaks landing in the monthly-average segment are tagged so the UI can show a
  month rather than a day it cannot actually know.
  """
  out = {}
  for years in CHART_RANGES:
    window = slice_years(series, years)
    if years_spanned(window) < years * 0.8:
      continue  # not enough history fetched to honestly offer this range
    peaks = find_peaks(window, PEAK_THRESHOLDS[years])
    for pk in peaks:
      pk["monthly"] = bool(daily_from and pk["date"] < daily_from)
    out[str(years)] = peaks
  return out


def peak_from_history(history: list[dict], window_days: int = RECENT_PEAK_WINDOW_DAYS) -> dict:
  """Highest close within the last `window_days`, in the shape main() expects."""
  cutoff  = datetime.now(timezone.utc).timestamp() - window_days * 86400
  in_window = [
    r for r in history
    if datetime.strptime(r["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() >= cutoff
  ]
  if not in_window:
    raise ValueError(f"No history rows within the last {window_days} days")

  top = max(in_window, key=lambda r: r["close"])
  return {
    "peak_sp500":  top["close"],
    "peak_date":   top["date"],
    "window_days": window_days,
  }


def build_scenarios(sp500: float, cape: float) -> list[dict]:
  """
  Build the sensitivity table rows.
  CAPE denominator (10yr real earnings) is derived from current values
  and held fixed — it only shifts slowly over time.
  """
  earnings_base = sp500 / cape  # 10-yr avg inflation-adj earnings

  fixed_scenarios = [
    {"label": "+30%",                 "mult": 1.30},
    {"label": "+20%",                 "mult": 1.20},
    {"label": "+10%",                 "mult": 1.10},
    {"label": "Today",                "mult": 1.00, "current": True},
    {"label": "−10%",                 "mult": 0.90},
    {"label": "−20%",                 "mult": 0.80},
    {"label": "−30%",                 "mult": 0.70},
    {"label": "−40%",                 "mult": 0.60},
    {"label": "−50%",                 "mult": 0.50},
  ]

  mean_reversion = [
    {
      "label":       f"Revert to dot-com peak CAPE ({DOT_COM_PEAK_CAPE})",
      "target_cape": DOT_COM_PEAK_CAPE,
    },
    {
      "label":       f"Revert to 20-yr avg CAPE ({RECENT_20Y_AVG_CAPE})",
      "target_cape": RECENT_20Y_AVG_CAPE,
    },
    {
      "label":       f"Revert to long-run avg CAPE ({LONG_RUN_AVG_CAPE})",
      "target_cape": LONG_RUN_AVG_CAPE,
    },
  ]

  rows = []

  for s in fixed_scenarios:
    level = round(sp500 * s["mult"])
    derived_cape = round(level / earnings_base, 2)
    pct_change = round((level - sp500) / sp500 * 100, 1)
    rows.append({
      "label":      s["label"],
      "sp500":      level,
      "cape":       derived_cape,
      "pct_change": pct_change,
      "current":    s.get("current", False),
      "type":       "relative",
    })

  for s in mean_reversion:
    level = round(s["target_cape"] * earnings_base)
    pct_change = round((level - sp500) / sp500 * 100, 1)
    rows.append({
      "label":      s["label"],
      "sp500":      level,
      "cape":       s["target_cape"],
      "pct_change": pct_change,
      "current":    False,
      "type":       "mean_reversion",
    })

  return rows


def valuation_zone(cape: float) -> dict:
  if cape >= 40:
    return {"label": "Extreme",  "cls": "zone-extreme"}
  if cape >= 32:
    return {"label": "High",     "cls": "zone-high"}
  if cape >= 25:
    return {"label": "Elevated", "cls": "zone-elevated"}
  if cape >= 18:
    return {"label": "Fair",     "cls": "zone-fair"}
  return   {"label": "Cheap",    "cls": "zone-cheap"}


def dump_payload(payload: dict) -> str:
  """
  json.dumps(indent=2), except history points and peaks are kept to one line each
  so data.json stays reviewable in a diff — and a good deal smaller — instead of
  exploding to tens of thousands of lines.
  """
  history  = payload.get("history") or []
  by_range = payload.get("peaks_by_range") or {}

  text = json.dumps(
    {**payload, "history": "__HISTORY__", "peaks_by_range": "__PEAKS__"},
    indent=2,
  )

  rows = ",\n".join(
    f'    {{"date": "{r["date"]}", "close": {r["close"]}'
    + (', "monthly": true}' if r.get("monthly") else '}')
    for r in history
  )
  text = text.replace('"__HISTORY__"', f"[\n{rows}\n  ]" if history else "[]", 1)

  blocks = []
  for years, peaks in by_range.items():
    lines = ",\n".join(f"      {json.dumps(pk)}" for pk in peaks)
    blocks.append(f'    "{years}": [\n{lines}\n    ]')
  compact = "{\n" + ",\n".join(blocks) + "\n  }" if blocks else "{}"
  return text.replace('"__PEAKS__"', compact, 1)


def main() -> None:
  print("Fetching Shiller CAPE ratio...")
  cape = fetch_cape()
  print(f"  CAPE: {cape}")

  print("Fetching S&P 500 price + recent peak...")
  sp500    = None
  high_52w = None
  peak     = None

  # 1. Try multpl sources (return float, no peak data)
  for fetcher, name in [
    (fetch_sp500_multpl_direct, "multpl (value page)"),
    (fetch_sp500,               "multpl (returns page)"),
  ]:
    try:
      sp500 = fetcher()
      print(f"  SP500: {sp500:,.2f}  (source: {name})")
      break
    except Exception as exc:
      print(f"  [{name}] failed: {exc}")

  # 2. Yahoo Finance HTML — returns current price AND 52-week high in one request
  if sp500 is None:
    try:
      ydata    = fetch_sp500_yahoo()
      sp500    = ydata["sp500"]
      high_52w = ydata.get("high_52w")
      print(f"  SP500: {sp500:,.2f}  (source: Yahoo Finance (quote page))")
      if high_52w:
        print(f"  52-week high: {high_52w:,.2f}")
    except Exception as exc:
      print(f"  [Yahoo Finance (quote page)] failed: {exc}")

  # 3. Last resort — Yahoo chart API (gives price + 30-day peak, one request)
  if sp500 is None:
    try:
      chart = fetch_sp500_yahoo_chart()
      sp500 = chart["sp500"]
      peak  = chart["peak"]
      print(f"  SP500: {sp500:,.2f}  (source: Yahoo Finance chart)")
      print(f"  Peak: {peak['peak_sp500']:,.2f} on {peak['peak_date']} (last {peak['window_days']}d)")
    except Exception as exc:
      print(f"  [Yahoo Finance chart] failed: {exc}")

  # 4. Daily history for the peak chart.  Doubles as the source of the near-term
  #    peak row, so no extra Yahoo request is needed for it.
  history:    list[dict] = []
  by_range:   dict       = {}
  daily_from: str | None = None
  if sp500 is not None:
    try:
      built      = build_history()
      full       = built["series"]
      daily_from = built["daily_from"]
      print(f"  History: {len(full)} points ({full[0]['date']} → {full[-1]['date']}, {built['years']:.1f} years)")

      by_range = peaks_by_range(full, daily_from)
      for years in sorted(by_range, key=int):
        pks = by_range[years]
        print(f"  Peaks {years:>2}y: {len(pks):>2} (≥{PEAK_THRESHOLDS[int(years)]}% pullback), latest {pks[-1]['date']} @ {pks[-1]['sp500']:,.2f}")

      # Thin the stored series, but never a date a peak marker sits on.
      keep    = {pk["date"] for pks in by_range.values() for pk in pks}
      history = downsample_for_chart(full, keep)
      print(f"  Chart series: {len(history)} points after thinning")

      if peak is None:
        peak = peak_from_history(full)
        print(f"  Peak: {peak['peak_sp500']:,.2f} on {peak['peak_date']} (last {peak['window_days']}d, from history)")
    except Exception as exc:
      print(f"  [price history] failed: {exc}")

  # 5. Still no peak — one more chart call, then the 52-week high as a last resort
  if sp500 is not None and peak is None:
    try:
      chart = fetch_sp500_yahoo_chart()
      peak  = chart["peak"]
      print(f"  Peak: {peak['peak_sp500']:,.2f} on {peak['peak_date']} (last {peak['window_days']}d, source: Yahoo chart)")
    except Exception as exc:
      print(f"  [Yahoo chart peak] failed: {exc}")
      # Fall back to 52-week high (no date available)
      if high_52w is not None:
        peak = {"peak_sp500": high_52w, "peak_date": None, "window_days": 365}
        print(f"  Peak (52-week high, no date): {high_52w:,.2f}")

  if sp500 is None:
    print("ERROR: all SP500 sources failed", file=sys.stderr)
    sys.exit(1)

  earnings_base = round(sp500 / cape, 2)
  scenarios     = build_scenarios(sp500, cape)

  for row in scenarios:
    row["zone"] = valuation_zone(row["cape"])

  # Insert peak as a scenario row at the correct SP500 position.  When price is
  # sitting at the window high the peak row would just duplicate "Today", so mark
  # the current row instead — the peak chart and the peak table below carry the
  # history that would otherwise vanish on a record-high day.
  at_peak = False
  if peak is not None:
    peak_level = peak["peak_sp500"]
    peak_cape  = round(peak_level / earnings_base, 2)
    pct_change = round((peak_level - sp500) / sp500 * 100, 1)
    window     = peak["window_days"]
    label      = "52-week high" if window >= 360 else f"{window}-day peak"
    at_peak    = peak_level <= sp500 * 1.0005

  if at_peak:
    current_row = next(r for r in scenarios if r.get("current"))
    current_row["at_peak"]          = True
    current_row["peak_window_days"] = window
    print(f"  Today ({sp500:,.2f}) is the {window}-day high — peak row folded into Today")
  elif peak is not None:
    peak_row = {
      "label":      label,
      "sp500":      peak_level,
      "cape":       peak_cape,
      "pct_change": pct_change,
      "current":    False,
      "type":       "peak",
      "peak_date":  peak.get("peak_date"),
    }
    peak_row["zone"] = valuation_zone(peak_cape)

    # Splice into the relative rows (sorted descending by SP500 level)
    rel_end = next(i for i, r in enumerate(scenarios) if r["type"] != "relative")
    insert_at = next(
      (i for i in range(rel_end) if scenarios[i]["sp500"] <= peak_level),
      rel_end,
    )
    scenarios.insert(insert_at, peak_row)

  payload = {
    "generated_at":      datetime.now(timezone.utc).isoformat(),
    "sp500":             sp500,
    "cape":              cape,
    "earnings_base":     earnings_base,
    "long_run_avg_cape": LONG_RUN_AVG_CAPE,
    "recent_20y_avg":    RECENT_20Y_AVG_CAPE,
    "dot_com_peak":      DOT_COM_PEAK_CAPE,
    "at_peak":           at_peak,
    "chart_ranges":      [int(y) for y in sorted(by_range, key=int)],
    "default_range":     DEFAULT_RANGE,
    "peak_thresholds":   {y: PEAK_THRESHOLDS[int(y)] for y in by_range},
    "daily_from":        daily_from,
    "peaks_by_range":    {
      years: [
        dict(pk, cape=round(pk["sp500"] / earnings_base, 2),
                 zone=valuation_zone(round(pk["sp500"] / earnings_base, 2)),
                 pct_from_today=round((pk["sp500"] - sp500) / sp500 * 100, 1))
        for pk in pks
      ]
      for years, pks in by_range.items()
    },
    "history":           history,
    "scenarios":         scenarios,
  }

  out = Path("data.json")
  out.write_text(dump_payload(payload))
  print(f"\nWrote {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
  main()
