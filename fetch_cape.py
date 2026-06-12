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

  # 4. If we have price but no peak yet, try chart API for peak (with 52-week high as fallback)
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

  payload = {
    "generated_at":      datetime.now(timezone.utc).isoformat(),
    "sp500":             sp500,
    "cape":              cape,
    "earnings_base":     earnings_base,
    "long_run_avg_cape": LONG_RUN_AVG_CAPE,
    "recent_20y_avg":    RECENT_20Y_AVG_CAPE,
    "dot_com_peak":      DOT_COM_PEAK_CAPE,
    "recent_peak":       peak,
    "scenarios":         scenarios,
  }

  out = Path("data.json")
  out.write_text(json.dumps(payload, indent=2))
  print(f"\nWrote {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
  main()
