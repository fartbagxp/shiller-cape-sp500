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

import json
import re
import sys
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


def fetch_sp500_yahoo() -> float:
  """Fallback: fetch S&P 500 from Yahoo Finance quote page."""
  url = "https://finance.yahoo.com/quote/%5EGSPC/"
  resp = httpx.get(url, headers=HEADERS, timeout=15, follow_redirects=True)
  resp.raise_for_status()
  soup = BeautifulSoup(resp.text, "lxml")

  # Yahoo Finance fin-streamer for regularMarketPrice
  el = soup.find("fin-streamer", {"data-symbol": "^GSPC", "data-field": "regularMarketPrice"})
  if el and el.get("value"):
    return float(el["value"].replace(",", ""))

  # Fallback: look for the large price display
  el = soup.find("span", {"data-testid": "qsp-price"})
  if el:
    return float(el.get_text(strip=True).replace(",", ""))

  raise ValueError("Could not parse SP500 from Yahoo Finance")


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

  print("Fetching S&P 500 price...")
  sp500 = None
  for fetcher, name in [
    (fetch_sp500_multpl_direct, "multpl (value page)"),
    (fetch_sp500,               "multpl (returns page)"),
    (fetch_sp500_yahoo,         "Yahoo Finance"),
  ]:
    try:
      sp500 = fetcher()
      print(f"  SP500: {sp500:,.2f}  (source: {name})")
      break
    except Exception as exc:
      print(f"  [{name}] failed: {exc}")

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
    "scenarios":         scenarios,
  }

  out = Path("data.json")
  out.write_text(json.dumps(payload, indent=2))
  print(f"\nWrote {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
  main()
