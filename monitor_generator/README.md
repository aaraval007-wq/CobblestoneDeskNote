# European Cross-Commodity Risk Monitor
**Gas + Carbon → Power Curve Implications**

*Author: Aarav Agarwal*
*aaraval007@gmail.com*

---

## Overview

This script automates the daily monitoring workflow for European cross-commodity energy risk. It pulls gas, carbon and power market data from public sources, computes 8 trading metrics, generates two charts, and produces a structured daily brief (PDF + Markdown) with an AI-generated narrative via the Google Gemini API.

The monitor covers the core analytical triangle used by European energy desks:

```
TTF Gas (supply/storage) → EUA Carbon (compliance cost) → German Power Curve (price implication)
```

---

## Outputs

Every run writes four files to the `output/` folder:

| File | Description |
|---|---|
| `daily_brief_YYYY-MM-DD.pdf` | Formatted A4 desk note with charts, metrics table, narrative, and risk skew |
| `daily_brief_YYYY-MM-DD.md` | Same content as editable Markdown, including full AI prompt/response audit log |
| `chart1_storage.png` | EU gas storage fill % vs 5-year seasonal min/max/average band |
| `chart2_prices.png` | TTF, EUA and German Cal+1 power — 12-month lookback panel |

---

## Requirements

```
pip install requests pandas matplotlib numpy reportlab
```

No other libraries are required. The Gemini API is called via a direct HTTP request using `requests` — no Gemini SDK needed.

---

## Setup and Usage

### 1. Install dependencies
```bash
pip install requests pandas matplotlib numpy reportlab
```

### 2. Get a free Gemini API key (optional but recommended)
- Go to **aistudio.google.com**
- Sign in with a Google account
- Click **Get API Key** → **Create API key**

### 3. Set your API key

**(Command Prompt):**
```
set GEMINI_API_KEY=AIzaYourKeyHere
python energy_monitor.py
```

**Or paste it directly into the script** (line 52 in CONFIG):
```python
"gemini_api_key": os.getenv("GEMINI_API_KEY", "AIzaYourKeyHere"),
```

### 4. Run the script
```bash
python energy_monitor.py
```

The terminal will print timestamped logs for each step. The full run takes approximately 5–10 seconds (longer if Gemini rate-limits and retries).

---

## Data Sources and What Actually Works

This is important to understand before using the outputs. The script has three tiers of data reliability:

### ✅ Live (working reliably)
**TTF Natural Gas front-month** via Yahoo Finance (`TTF=F`)
- Pulls 12 months of daily price history
- Confirmed working from residential/office IP addresses
- May return 403 Forbidden from cloud/server environments — falls back to synthetic in that case

### ⚠️ Failing (falling back to synthetic data)
**EUA Carbon front-year** — ticker `EUANX=F` does not exist on Yahoo Finance (404)
- Falls back to a **synthetic price path** anchored between €63 and €73 using geometric Brownian motion
- The path looks realistic but is not real market data
- To update the anchor: edit `_synthetic_fallback()` in the script, find the `"EUANX=F"` entry and change the `(start, end, vol)` tuple to reflect current EUA levels

**German Cal+1 baseload power** — ticker `DE1YF=F` does not exist on Yahoo Finance (404)
- Falls back to a **synthetic price path** anchored between €70 and €91
- Same caveat as EUA — realistic but not live
- To update: edit the `"DE1YF=F"` entry in `_synthetic_fallback()`

**EU aggregate gas storage** via GIE AGSI+ API
- The public endpoint (`agsi.gie.eu/api`) returns an empty data array without an API key
- Falls back to **hardcoded values**: 36.34% fill, 411.3 TWh in storage, 2,100 GWh/day injection pace
- These values reflect the actual market position as of mid-May 2026 but will not update daily without a key

### Synthetic Data Generation
When live data is unavailable, the script generates a price series using **geometric Brownian motion** — the same stochastic model underlying Black-Scholes options pricing:

```
drift     = log(end / start) / n          # average daily log return
returns   = drift + volatility × N(0,1)   # daily returns with noise
prices    = start × exp(cumsum(returns))   # reconstruct price path
```

The random seed is fixed per ticker (`np.random.seed(hash(ticker))`), so the synthetic path is **reproducible** — the same script run on the same day always produces the same synthetic series.

---

## AI Narrative Generation

The script uses the **Google Gemini API** (`gemini-1.5-flash`) to generate the three-paragraph market narrative in the daily brief.

### How it works
1. All 8 computed metrics are formatted into a structured prompt
2. The prompt instructs Gemini to write a 3-paragraph trading floor morning note using the exact numbers provided
3. The response is inserted into the brief and the full prompt + response are logged in the Markdown appendix for auditability

### Rate limiting and retries
The free Gemini tier allows approximately 15 requests per minute and ~50–100 requests per day. If the rate limit is hit, the script **automatically retries** with exponential backoff:
- Attempt 1: wait 20 seconds, retry
- Attempt 2: wait 40 seconds, retry
- Attempt 3: wait 60 seconds, retry
- If all 3 fail: falls back to a **hardcoded template narrative**

### Fallback narrative
If Gemini is unavailable (no key, rate limit exhausted, or API error), the script uses a pre-written template narrative that covers the same three topics — gas tightness, carbon signal, and power curve call — using generic language rather than live numbers. The brief is still fully generated and the PDF is still produced.

### Daily quota reset
The Gemini free tier quota resets at midnight Pacific Time. If you exhaust the daily limit, wait until the next day or create a second free API key at aistudio.google.com.

---

## The 8 Monitor Metrics

| # | Metric | Formula | Source |
|---|--------|---------|--------|
| 1 | TTF M+1 (€/MWh) | ICE TTF front-month settle | Yahoo Finance (TTF=F) |
| 2 | EU Storage vs 5-yr avg (pp) | AGSI+ fill% − seasonal average | GIE AGSI+ |
| 3 | Required injection pace (GWh/day) | (Target TWh − Current TWh) × 1000 ÷ Days to 1 Nov | GIE AGSI+ + calculation |
| 4 | EUA front-year (€/t) | ICE EUA Dec front-year settle | Yahoo Finance (EUANX=F) |
| 5 | Carbon cost per MWh (€/MWh) | EUA × 0.394 tCO₂/MWh | Derived |
| 6 | Clean Spark Spread (€/MWh) | Power − (TTF ÷ 0.4913) − (EUA × 0.394) | Derived |
| 7 | German Cal+1 baseload (€/MWh) | EEX German baseload Cal+1 settle | Yahoo Finance (DE1YF=F) |
| 8 | Implied EUA in Cal+1 (€/t) | (Power − TTF/η) ÷ EF — solving CSS = 0 | Derived |

**Conventions:** CCGT efficiency η = 49.13% (Argus/ICIS UK convention). Emission factor EF = 0.394 tCO₂/MWh of power output.

**Signal thresholds:**
- TTF: BULLISH > €40/MWh, BEARISH < €28/MWh
- Storage gap: BULLISH < −10pp, BEARISH > +5pp
- EUA: BULLISH > €80/t, BEARISH < €60/t
- CSS: BULLISH > €5/MWh, BEARISH < −€20/MWh

---

## File Structure

```
energy_monitor/
├── energy_monitor.py       # main script
├── requirements.txt        # pip dependencies
├── README.md               # this file
└── output/                 # generated on first run
    ├── daily_brief_YYYY-MM-DD.pdf
    ├── daily_brief_YYYY-MM-DD.md
    ├── chart1_storage.png
    └── chart2_prices.png
```

---

## Known Limitations

- **EUA and German power prices are synthetic.** Until a reliable free data source is identified, these series are generated rather than pulled from live markets. The synthetic values are calibrated to realistic May 2026 levels but will drift over time.
- **GIE storage requires a free API key** for live data. The fallback hardcodes mid-May 2026 values.
- **Yahoo Finance blocks server-side requests** (403 Forbidden). The script works correctly from a local machine but will fall back to synthetic data if run from a cloud server or CI pipeline.
- **Gemini free tier is limited.** Running the script more than ~10 times in a day will exhaust the daily quota. The fallback narrative is used automatically when this happens.
- **Seasonal storage curve is approximated.** The 5-year average band uses a sine curve calibrated to historical GIE patterns rather than actual historical data per day.

---
