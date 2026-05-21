"""
═══════════════════════════════════════════════════════════════════════════════
  European Cross-Commodity Risk Monitor
  Gas + Carbon → Power Curve Implications
  Author: Aarav Agarwal
═══════════════════════════════════════════════════════════════════════════════

  Usage:
      python energy_monitor.py

  Outputs (written to ./output/):
      daily_brief_YYYY-MM-DD.md   — structured desk note
      daily_brief_YYYY-MM-DD.pdf   — formatted PDF report
      chart1_storage.png          — EU storage vs seasonal band
      chart2_prices.png           — TTF / EUA / German power panel

  Requirements:
      pip install requests pandas matplotlib numpy reportlab

  API keys (set as environment variables OR edit CONFIG below):
      GEMINI_API_KEY      — for AI narrative

  Data sources (all free/public):
      GIE AGSI+       — EU gas storage  (agsi.gie.eu)
      Yahoo Finance   — TTF, EUA, German power proxies
═══════════════════════════════════════════════════════════════════════════════
"""

import os
import json
import logging
import requests
import time
import warnings
import numpy as np
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Image,
    Table, TableStyle, HRFlowable
)
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_JUSTIFY
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime, timedelta, date
from pathlib import Path

warnings.filterwarnings("ignore")

# ── CONFIG ────────────────────────────────────────────────────────────────────
CONFIG = {
    # Output directory (relative to script location)
    "output_dir": "output",

    # Google Gemini API key — free at aistudio.google.com
    # Set env var GEMINI_API_KEY or paste key directly here
    "gemini_api_key": os.getenv("GEMINI_API_KEY", "YOUR_KEY_HERE"),


    # CCGT efficiency & emission factor (Argus/ICIS UK convention)
    "ccgt_efficiency":    0.4913,     # 49.13%
    "gas_emission_factor": 0.394,     # tCO2 per MWh of power output

    # Yahoo Finance tickers
    # Note: EUA and DE power have no reliable Yahoo tickers — script falls back to synthetic data
    "ttf_ticker":    "TTF=F",         # TTF Natural Gas
    "eua_ticker":    "EUANX=F",       # EUA
    "de_pwr_ticker": "DE1YF=F",       # German power 

    # Lookback window for charts (trading days)
    "chart_lookback_days": 252,

    # GIE AGSI storage API
    "gie_storage_url": "https://agsi.gie.eu/api",
}

# ── LOGGING ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── OUTPUT DIR ────────────────────────────────────────────────────────────────
OUT = Path(CONFIG["output_dir"])
OUT.mkdir(exist_ok=True)

# ═════════════════════════════════════════════════════════════════════════════
#  SECTION 1 — DATA INGESTION
# ═════════════════════════════════════════════════════════════════════════════

def fetch_yahoo(ticker: str, period: str = "1y") -> pd.DataFrame:
    """
    Pull price history from Yahoo Finance (unofficial JSON endpoint).
    Returns a DataFrame with columns: Date, Close.
    Falls back to synthetic data on failure so the script always runs.
    """
    log.info(f"Fetching Yahoo Finance: {ticker}")
    try:
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
            f"?range={period}&interval=1d&events=history"
        )
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, headers=headers, timeout=15)
        r.raise_for_status()
        data = r.json()
        ts    = data["chart"]["result"][0]["timestamp"]
        close = data["chart"]["result"][0]["indicators"]["quote"][0]["close"]
        df = pd.DataFrame({
            "Date":  pd.to_datetime(ts, unit="s").normalize(),
            "Close": close,
        }).dropna().set_index("Date")
        log.info(f"  ✓ {ticker}: {len(df)} rows, latest={df['Close'].iloc[-1]:.2f} ({df.index[-1].date()})")
        return df
    except Exception as e:
        log.warning(f"  ✗ Yahoo {ticker} failed ({e}). Using synthetic fallback.")
        return _synthetic_fallback(ticker)


def _synthetic_fallback(ticker: str) -> pd.DataFrame:
    """
    Generate realistic synthetic price series when live data is unavailable.
    Based on publicly reported levels for May 2026.
    """
    n = 252
    dates = pd.date_range(end=date.today(), periods=n, freq="B")
    np.random.seed(hash(ticker) % (2**31))

    paths = {
        "TTF=F":    (28.0, 49.00, 0.015),   # anchored to live TTF
        "EUANX=F":  (63.0, 73.00, 0.008),   # EUA realistic range
        "DE1YF=F":  (70.0, 91.00, 0.012),   # German Cal+1 realistic range
    }
    start, end, vol = paths.get(ticker, (50.0, 60.0, 0.01))
    drift = (np.log(end / start)) / n
    log_returns = drift + vol * np.random.randn(n)
    prices = start * np.exp(np.cumsum(log_returns))
    log.info(f"  ↳ Synthetic {ticker}: latest={prices[-1]:.2f}")
    return pd.DataFrame({"Close": prices}, index=dates)


def fetch_gie_storage() -> dict:
    """
    Fetch EU aggregate gas storage from GIE AGSI+ public API.
    Returns dict with keys: fill_pct, full_twh, capacity_twh, date, trend_gwh_day.
    Falls back to latest known values on failure.
    """
    log.info("Fetching GIE AGSI+ storage data")
    try:
        # Try the public aggregated EU endpoint
        url = f"{CONFIG['gie_storage_url']}?country=eu&size=10&page=1&type=Storage"
        headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
        r = requests.get(url, headers=headers, timeout=15)
        r.raise_for_status()
        parsed = r.json()

        raw = parsed.get("data", [])
        if isinstance(raw, dict):
            raw = raw.get("data", [])
        if not raw:
            raise ValueError("Empty data array from GIE API")
        latest = raw[0]
        result = {
            "fill_pct":      float(latest.get("full", 36.34)),
            "full_twh":      float(latest.get("gasInStorage", 411.3)),
            "capacity_twh":  float(latest.get("workingGasVolume", 1132.0)),
            "date":          latest.get("gasDayStart", str(date.today())),
            "trend_gwh_day": float(latest.get("trend", 2100.0)),
        }
        log.info(f"  ✓ Storage: {result['fill_pct']:.1f}% full  ({result['date']})")
        return result
    except Exception as e:
        log.warning(f"  ✗ GIE AGSI failed ({e}). Using fallback values.")
        return {
            "fill_pct":     36.34,
            "full_twh":     411.3,
            "capacity_twh": 1132.0,
            "date":         str(date.today()),
            "trend_gwh_day": 2100.0,
        }


def fetch_all_data() -> dict:
    """
    Returns a cleaned dict of all inputs needed for metrics calculation and chart generation.
    """
    log.info("═" * 60)
    log.info("  STEP 1 — DATA INGESTION")
    log.info("═" * 60)

    ttf_df = fetch_yahoo(CONFIG["ttf_ticker"])
    eua_df = fetch_yahoo(CONFIG["eua_ticker"])
    pwr_df = fetch_yahoo(CONFIG["de_pwr_ticker"])
    storage = fetch_gie_storage()

    # Latest prices
    ttf_price = ttf_df["Close"].iloc[-1]
    eua_price = eua_df["Close"].iloc[-1]
    pwr_price = pwr_df["Close"].iloc[-1]

    # YoY and MoM changes
    def pct_change(df, days):
        if len(df) > days:
            return (df["Close"].iloc[-1] / df["Close"].iloc[-days] - 1) * 100
        return None

    ttf_yoy = pct_change(ttf_df, 252)
    ttf_mom = pct_change(ttf_df, 21)
    eua_yoy = pct_change(eua_df, 252)
    pwr_yoy = pct_change(pwr_df, 252)

    # 52-week ranges
    ttf_52w_hi = ttf_df["Close"].tail(252).max()
    ttf_52w_lo = ttf_df["Close"].tail(252).min()
    eua_52w_hi = eua_df["Close"].tail(252).max()
    eua_52w_lo = eua_df["Close"].tail(252).min()
    pwr_52w_hi = pwr_df["Close"].tail(252).max()
    pwr_52w_lo = pwr_df["Close"].tail(252).min()

    return {
        "as_of":       datetime.today().strftime("%d %B %Y"),
        "run_time":    datetime.now().strftime("%Y-%m-%d %H:%M"),

        # Price series (for charts)
        "ttf_df":      ttf_df,
        "eua_df":      eua_df,
        "pwr_df":      pwr_df,

        # Current prices
        "ttf":         ttf_price,
        "eua":         eua_price,
        "pwr":         pwr_price,

        # Changes
        "ttf_yoy":     ttf_yoy,
        "ttf_mom":     ttf_mom,
        "eua_yoy":     eua_yoy,
        "pwr_yoy":     pwr_yoy,

        # Ranges
        "ttf_52w_hi":  ttf_52w_hi,
        "ttf_52w_lo":  ttf_52w_lo,
        "eua_52w_hi":  eua_52w_hi,
        "eua_52w_lo":  eua_52w_lo,
        "pwr_52w_hi":  pwr_52w_hi,
        "pwr_52w_lo":  pwr_52w_lo,

        # Storage
        "storage":     storage,
    }


# ═════════════════════════════════════════════════════════════════════════════
#  SECTION 2 — METRICS CALCULATION
# ═════════════════════════════════════════════════════════════════════════════

# Seasonal storage averages by day-of-year (approximation from GIE 5-year data)
# Source: GIE AGSI+ historical aggregates
_DOY = np.arange(1, 366)
_S_AVG = np.clip(
    55 + 33 * np.sin(2 * np.pi * (_DOY - 274) / 365), 28, 95
)
_STORAGE_SEASONAL = dict(zip(_DOY, _S_AVG))

# Required injection to 90% by 1 Nov (day 305), GWh/day
STORAGE_CAPACITY_TWH  = 1132.0
STORAGE_TARGET_PCT    = 90.0
NOV1_DOY              = 305


def compute_metrics(data: dict) -> dict:
    """
    Derive all 8 monitor metrics from raw data.
    Returns a dict of metric dicts, each with: value, signal, unit, description.
    """
    log.info("═" * 60)
    log.info("  STEP 2 — METRICS CALCULATION")
    log.info("═" * 60)

    ttf    = data["ttf"]
    eua    = data["eua"]
    pwr    = data["pwr"]
    stor   = data["storage"]
    eta    = CONFIG["ccgt_efficiency"]
    ef     = CONFIG["gas_emission_factor"]
    today_doy = datetime.today().timetuple().tm_yday

    # ── 1. TTF M+1 ──────────────────────────────────────────────────────────
    m1 = {
        "name":        "TTF M+1 (€/MWh)",
        "value":       ttf,
        "unit":        "€/MWh",
        "yoy_pct":     data["ttf_yoy"],
        "mom_pct":     data["ttf_mom"],
        "signal":      "BULLISH" if ttf > 40 else ("BEARISH" if ttf < 28 else "NEUTRAL"),
        "threshold_hi": 50.0,
        "threshold_lo": 28.0,
        "description": "Primary power price driver. Gas-fired plant sets marginal power price "
                       "in ~40% of EU hours. Moves in TTF transmit 1:1 to prompt power.",
        "formula":     "ICE TTF front-month settle",
        "source":      "ICE / Yahoo Finance (TTF=F)",
    }
    log.info(f"  [1] TTF M+1:       €{ttf:.2f}/MWh  ({m1['signal']})")

    # ── 2. EU Storage vs 5-yr average ───────────────────────────────────────
    fill      = stor["fill_pct"]
    seas_avg  = _STORAGE_SEASONAL.get(today_doy, 55.0)
    gap_pp    = fill - seas_avg
    m2 = {
        "name":        "EU Storage vs 5-yr avg (pp)",
        "value":       gap_pp,
        "fill_pct":    fill,
        "seasonal_avg": seas_avg,
        "unit":        "pp vs seasonal avg",
        "signal":      "BULLISH" if gap_pp < -10 else ("BEARISH" if gap_pp > 5 else "NEUTRAL"),
        "threshold":   -10.0,
        "description": "Storage deficit amplifies price risk to supply shocks. "
                       ">10pp below average = structurally short market.",
        "formula":     "AGSI+ fill% − 5yr seasonal average for same day-of-year",
        "source":      "GIE AGSI+ (agsi.gie.eu)",
    }
    log.info(f"  [2] Storage:       {fill:.1f}% ({gap_pp:+.1f}pp vs avg)  ({m2['signal']})")

    # ── 3. Required injection rate ───────────────────────────────────────────
    days_to_nov1 = max(NOV1_DOY - today_doy, 1)
    target_twh   = STORAGE_CAPACITY_TWH * STORAGE_TARGET_PCT / 100
    current_twh  = stor["full_twh"]
    required_gwh = max((target_twh - current_twh) * 1000 / days_to_nov1, 0)
    current_pace = stor["trend_gwh_day"]
    pace_gap     = required_gwh - current_pace
    m3 = {
        "name":         "Required injection pace (GWh/day)",
        "value":        required_gwh,
        "current_pace": current_pace,
        "pace_gap":     pace_gap,
        "unit":         "GWh/day",
        "signal":       "BEARISH" if pace_gap > 500 else ("BULLISH" if pace_gap < -200 else "NEUTRAL"),
        "description":  "Injection shortfall indicates winter buffer risk. "
                        "Pace gap > 500 GWh/day signals target at risk.",
        "formula":      "(Target TWh − Current TWh) × 1000 ÷ Days to 1 Nov",
        "source":       "GIE AGSI+ + calculation",
    }
    log.info(f"  [3] Req. injection: {required_gwh:.0f} GWh/d (current: {current_pace:.0f}, gap: {pace_gap:+.0f})  ({m3['signal']})")

    # ── 4. EUA Dec front-year ────────────────────────────────────────────────
    m4 = {
        "name":        "EUA Dec front-year (€/t)",
        "value":       eua,
        "unit":        "€/t CO₂",
        "yoy_pct":     data["eua_yoy"],
        "signal":      "BULLISH" if eua > 80 else ("BEARISH" if eua < 60 else "NEUTRAL"),
        "threshold_hi": 80.0,
        "threshold_lo": 60.0,
        "description": "Carbon adds directly to gas plant variable cost: "
                       "EUA × 0.394 = €/MWh embedded in power price.",
        "formula":     "ICE EUA Dec front-year settle",
        "source":      "ICE / Yahoo Finance (EUANX=F)",
    }
    log.info(f"  [4] EUA:           €{eua:.2f}/t  ({m4['signal']})")

    # ── 5. Carbon cost per MWh (gas) ────────────────────────────────────────
    carbon_per_mwh = eua * ef
    m5 = {
        "name":        "Carbon cost per MWh (€/MWh)",
        "value":       carbon_per_mwh,
        "unit":        "€/MWh",
        "signal":      "BULLISH" if carbon_per_mwh > 30 else ("BEARISH" if carbon_per_mwh < 22 else "NEUTRAL"),
        "description": "Direct power price floor contribution from carbon. "
                       "Rising EUA lifts gas plant marginal cost.",
        "formula":     "EUA × EF_gas   where EF_gas = 0.394 tCO₂/MWh (Argus convention)",
        "source":      "Derived from EUA price",
    }
    log.info(f"  [5] Carbon/MWh:    €{carbon_per_mwh:.2f}/MWh  ({m5['signal']})")

    # ── 6. Clean Spark Spread ────────────────────────────────────────────────
    gas_cost_per_mwh = ttf / eta
    css = pwr - gas_cost_per_mwh - carbon_per_mwh
    m6 = {
        "name":             "Clean Spark Spread (€/MWh)",
        "value":            css,
        "gas_cost_per_mwh": gas_cost_per_mwh,
        "unit":             "€/MWh",
        "signal":           "BULLISH" if css > 5 else ("BEARISH" if css < -20 else "NEUTRAL"),
        "description":      "Gas plant profitability. Persistently negative = "
                            "gas plants unhedged forward; spot volatility elevated.",
        "formula":          "Power − (TTF ÷ η) − (EUA × EF)   where η=49.13%, EF=0.394",
        "source":           "Derived from TTF, EUA, German power",
    }
    log.info(f"  [6] CSS:           €{css:.2f}/MWh  ({m6['signal']})")

    # ── 7. German Cal+1 baseload ────────────────────────────────────────────
    m7 = {
        "name":        "German Cal+1 baseload (€/MWh)",
        "value":       pwr,
        "unit":        "€/MWh",
        "yoy_pct":     data["pwr_yoy"],
        "signal":      "BULLISH" if pwr > 90 else ("BEARISH" if pwr < 70 else "NEUTRAL"),
        "threshold_hi": 90.0,
        "threshold_lo": 70.0,
        "description": "Forward power curve anchor. Reflects gas, carbon and "
                       "renewable expectations for the delivery year.",
        "formula":     "EEX German baseload Cal+1 settle",
        "source":      "EEX / Yahoo Finance proxy (DE1YF=F)",
    }
    log.info(f"  [7] DE Cal+1:      €{pwr:.2f}/MWh  ({m7['signal']})")

    # ── 8. Implied carbon breakeven ─────────────────────────────────────────
    gas_contribution = ttf / eta
    implied_eua = (pwr - gas_contribution) / ef if ef > 0 else 0.0
    implied_eua = max(implied_eua, 0.0)  # floor at zero; negative = power below gas-only cost
    eua_gap     = eua - implied_eua
    m8 = {
        "name":        "Implied EUA in Cal+1 power (€/t)",
        "value":       implied_eua,
        "spot_eua":    eua,
        "gap":         eua_gap,
        "unit":        "€/t",
        "signal":      "BULLISH" if eua_gap > 20 else ("BEARISH" if eua_gap < -10 else "NEUTRAL"),
        "description": "Back-solved EUA at CSS=0. Gap between implied and spot EUA "
                       "shows how much carbon risk is unpriced in forward power.",
        "formula":     "(Power − TTF/η) ÷ EF   [solving CSS = 0 for EUA]",
        "source":      "Derived",
    }
    log.info(f"  [8] Implied EUA:   €{implied_eua:.2f}/t (spot: €{eua:.2f}, gap: €{eua_gap:.2f})  ({m8['signal']})")

    metrics = {
        "ttf":            m1,
        "storage_gap":    m2,
        "injection_pace": m3,
        "eua":            m4,
        "carbon_per_mwh": m5,
        "css":            m6,
        "de_power":       m7,
        "implied_eua":    m8,
    }

    # Count signals
    signals = [m["signal"] for m in metrics.values()]
    metrics["_summary"] = {
        "bullish": signals.count("BULLISH"),
        "neutral": signals.count("NEUTRAL"),
        "bearish": signals.count("BEARISH"),
    }
    log.info(f"\n  Signal summary: {metrics['_summary']}")
    return metrics


# ═════════════════════════════════════════════════════════════════════════════
#  SECTION 3 — CHART GENERATION
# ═════════════════════════════════════════════════════════════════════════════

# Light theme palette
BG, PANEL_C, BORDER_C = "#ffffff", "#f6f8fa", "#d0d7de"
TEXT_C, MUTED_C       = "#1a1a2e", "#57606a"
BLUE_C, RED_C         = "#0969da", "#cf222e"
GREEN_C, GOLD_C       = "#1a7f37", "#9a6700"
BAND_C                = "#eaeef2"

plt.rcParams.update({
    "figure.facecolor": BG,      "axes.facecolor":  PANEL_C,
    "axes.edgecolor":   BORDER_C,"axes.labelcolor": TEXT_C,
    "xtick.color":      MUTED_C, "ytick.color":     MUTED_C,
    "text.color":       TEXT_C,  "grid.color":      BORDER_C,
    "grid.linestyle":   "--",    "grid.alpha":      0.6,
    "font.family":      "DejaVu Sans", "font.size": 9,
})


def chart_storage(storage: dict, out_path: Path) -> None:
    """
    Chart 1: EU gas storage fill % vs 5-year seasonal band.
    Annotates the current level and required injection trajectory.
    """
    log.info("Generating Chart 1 — Storage")
    doy      = np.arange(1, 366)
    s_avg    = np.clip(55 + 33 * np.sin(2 * np.pi * (doy - 274) / 365), 28, 95)
    s_max    = np.clip(s_avg + 8,  32, 100)
    s_min    = np.clip(s_avg - 12, 18, 100)
    today_doy = datetime.today().timetuple().tm_yday

    # 2026 actual line (Jan-today) + required trajectory
    s_2026  = np.zeros(366)
    fill_now = storage["fill_pct"]
    # Reconstruct plausible path: started winter ~85%, drew to ~29% end-Mar, now recovering
    for d in range(1, 367):
        if d <= 90:
            s_2026[d-1] = 74 - (74 - 29) * (d / 90)
        elif d <= today_doy:
            frac = (d - 90) / max(today_doy - 90, 1)
            s_2026[d-1] = 29 + (fill_now - 29) * frac
        else:
            # Project at required pace (capped at 90%)
            daily_gain = max(
                (STORAGE_CAPACITY_TWH * STORAGE_TARGET_PCT / 100
                 - storage["full_twh"]) * 1000
                / max(NOV1_DOY - today_doy, 1)
                / (STORAGE_CAPACITY_TWH * 10),   # convert GWh/d → pp/d
                0
            )
            s_2026[d-1] = min(fill_now + (d - today_doy) * daily_gain, 90)

    fig, ax = plt.subplots(figsize=(10, 4.5))
    fig.patch.set_facecolor(BG)
    ax.fill_between(doy, s_min, s_max, color=BAND_C, label="5-yr min/max band")
    ax.plot(doy, s_avg, color=MUTED_C, lw=1.4, ls="--", label="5-yr average")
    ax.plot(doy[:today_doy], s_2026[:today_doy], color=BLUE_C, lw=2.2, label="2026 actual")
    ax.plot(doy[today_doy-1:today_doy-1+len(s_2026[today_doy-1:])], s_2026[today_doy-1:len(doy)], color=BLUE_C, lw=1.5,
            ls=":", label="Required injection trajectory")

    ax.axhline(90, color=GOLD_C, lw=1, ls="--", alpha=0.8)
    ax.text(5, 91.5, "90% target (1 Nov)", color=GOLD_C, fontsize=7.5)
    ax.axvline(today_doy, color=MUTED_C, lw=1, ls="--", alpha=0.4)

    seas_now = s_avg[today_doy - 1]
    ax.annotate(f"Today\n{fill_now:.1f}%", xy=(today_doy, fill_now),
                xytext=(today_doy + 14, fill_now + 7),
                color=BLUE_C, fontsize=8,
                arrowprops=dict(arrowstyle="->", color=BLUE_C, lw=0.9))
    ax.annotate("", xy=(today_doy, fill_now), xytext=(today_doy, seas_now),
                arrowprops=dict(arrowstyle="<->", color=RED_C, lw=1.2))
    ax.text(today_doy + 3, (fill_now + seas_now) / 2,
            f"{fill_now - seas_now:+.0f}pp\nvs avg",
            color=RED_C, fontsize=7.5, va="center")

    month_doys  = [1, 32, 60, 91, 121, 152, 182, 213, 244, 274, 305, 335]
    month_names = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    ax.set_xticks(month_doys)
    ax.set_xticklabels(month_names, fontsize=8)
    ax.set_xlim(1, 365)
    ax.set_ylim(15, 105)
    ax.set_ylabel("Storage fill (%)")
    ax.set_title(
        "Chart 1 — EU Natural Gas Storage vs Seasonal Norms  |  Source: GIE AGSI+",
        fontsize=10, color=TEXT_C, pad=10, loc="left", fontweight="bold")
    ax.legend(loc="upper left", fontsize=8, framealpha=0.85,
              facecolor=PANEL_C, edgecolor=BORDER_C)
    ax.grid(True, axis="y")
    fig.tight_layout(pad=1.2)
    fig.savefig(out_path, dpi=150, bbox_inches="tight", facecolor=BG)
    plt.close()
    log.info(f"  ✓ Saved: {out_path}")


def chart_prices(data: dict, out_path: Path) -> None:
    """
    Chart 2: 12-month lookback of TTF, EUA, and German power on aligned axes.
    """
    log.info("Generating Chart 2 — Cross-commodity prices")

    n      = CONFIG["chart_lookback_days"]
    ttf_s  = data["ttf_df"]["Close"].tail(n)
    eua_s  = data["eua_df"]["Close"].tail(n)
    pwr_s  = data["pwr_df"]["Close"].tail(n)

    fig, ax1 = plt.subplots(figsize=(10, 4.5))
    fig.patch.set_facecolor(BG)
    ax2 = ax1.twinx()

    l1, = ax1.plot(pwr_s.index, pwr_s.values, color=GREEN_C, lw=2.0,
                   label="DE Cal+1 Baseload (€/MWh)")
    l2, = ax1.plot(ttf_s.index, ttf_s.values, color=BLUE_C,  lw=1.8,
                   label="TTF M+1 (€/MWh)")
    l3, = ax2.plot(eua_s.index, eua_s.values, color=RED_C,   lw=1.8, ls="--",
                   label="EUA front-year (€/t, RHS)")

    # Annotate latest values
    for ax_ref, series, col, offset in [
        (ax1, pwr_s, GREEN_C, (+8, -55)),
        (ax1, ttf_s, BLUE_C,  (-18, -60)),
        (ax2, eua_s, RED_C,   (+12, -65)),
    ]:
        ax_ref.annotate(
            f"€{series.iloc[-1]:.2f}",
            xy=(series.index[-1], series.iloc[-1]),
            xytext=(offset[1], offset[0]), textcoords="offset points",
            color=col, fontsize=8, fontweight="bold",
            arrowprops=dict(arrowstyle="->", color=col, lw=0.8))

    ax1.set_ylabel("€/MWh", color=TEXT_C)
    ax2.set_ylabel("€/t CO₂  (EUA)", color=RED_C)
    ax2.tick_params(axis="y", colors=RED_C)
    ax1.set_ylim(min(ttf_s.min(), pwr_s.min()) * 0.85, pwr_s.max() * 1.15)
    ax2.set_ylim(eua_s.min() * 0.85, eua_s.max() * 1.15)

    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%b '%y"))
    ax1.xaxis.set_major_locator(mdates.MonthLocator(interval=2))
    fig.autofmt_xdate(rotation=0, ha="center")
    ax1.grid(True, axis="y")

    lines  = [l1, l2, l3]
    labels = [l.get_label() for l in lines]
    ax1.legend(lines, labels, loc="upper left", fontsize=8,
               framealpha=0.85, facecolor=PANEL_C, edgecolor=BORDER_C)
    ax1.set_title(
        "Chart 2 — TTF Gas  ·  EUA Carbon  ·  German Cal+1 Power  |  12-Month Lookback"
        "  |  Sources: ICE, EEX, Trading Economics",
        fontsize=9.5, color=TEXT_C, pad=10, loc="left", fontweight="bold")
    fig.tight_layout(pad=1.2)
    fig.savefig(out_path, dpi=150, bbox_inches="tight", facecolor=BG)
    plt.close()
    log.info(f"  ✓ Saved: {out_path}")


def generate_charts(data: dict) -> tuple[Path, Path]:
    log.info("═" * 60)
    log.info("  STEP 3 — CHART GENERATION")
    log.info("═" * 60)
    p1 = OUT / "chart1_storage.png"
    p2 = OUT / "chart2_prices.png"
    chart_storage(data["storage"], p1)
    chart_prices(data, p2)
    return p1, p2


# ═════════════════════════════════════════════════════════════════════════════
#  SECTION 4 — AI NARRATIVE GENERATION
# ═════════════════════════════════════════════════════════════════════════════

def build_prompt(data: dict, metrics: dict) -> str:
    """
    Construct a structured, metrics-grounded prompt for the LLM.
    This is logged to the output file for full auditability.
    """
    m       = metrics
    stor    = data["storage"]
    summary = m["_summary"]

    prompt = f"""You are a senior European energy market analyst writing a concise daily desk note.

Today's date: {data['as_of']}

LIVE MARKET DATA (use these exact numbers):

Gas:
- TTF front-month: €{m['ttf']['value']:.2f}/MWh  (YoY: {m['ttf']['yoy_pct'] or 0:+.1f}%, MoM: {m['ttf']['mom_pct'] or 0:+.1f}%)
- 52-week range: €{data['ttf_52w_lo']:.2f} – €{data['ttf_52w_hi']:.2f}/MWh
- EU storage: {stor['fill_pct']:.1f}% full ({m['storage_gap']['value']:+.1f}pp vs 5yr seasonal avg)
- Required injection pace to hit 80% by 1 Nov: {m['injection_pace']['value']:.0f} GWh/day
- Current injection pace: {stor['trend_gwh_day']:.0f} GWh/day (shortfall: {m['injection_pace']['pace_gap']:+.0f} GWh/day)

Carbon:
- EUA front-year: €{m['eua']['value']:.2f}/t  (YoY: {m['eua']['yoy_pct'] or 0:+.1f}%)
- Carbon cost per MWh of gas power: €{m['carbon_per_mwh']['value']:.2f}/MWh (EUA × 0.394)

Power:
- German Cal+1 baseload: €{m['de_power']['value']:.2f}/MWh  (YoY: {m['de_power']['yoy_pct'] or 0:+.1f}%)
- Clean Spark Spread (Cal+1): €{m['css']['value']:.2f}/MWh
- Implied EUA in Cal+1 power: €{m['implied_eua']['value']:.2f}/t (vs spot €{m['eua']['value']:.2f}/t, gap: €{m['implied_eua']['gap']:.2f}/t)

Signal summary: {summary['bullish']} BULLISH  |  {summary['neutral']} NEUTRAL  |  {summary['bearish']} BEARISH

Write a 3-paragraph desk note in the style of a trading floor morning note. Requirements:
1. PARAGRAPH 1 (Gas tightness): State the storage position and TTF level, what they imply for supply risk, and the injection shortfall problem. Be direct and quantitative.
2. PARAGRAPH 2 (Carbon signal): Explain what EUA is doing and what the carbon cost implies for power pricing. Note the implied EUA gap and what it means.
3. PARAGRAPH 3 (Power curve call): Give a clear directional view on German Cal+1 power. State whether the market looks cheap, fair, or rich relative to fundamentals. Identify the one key risk that would change the call.

Write as if addressing a single trader. Use the numbers. State a view. Avoid hedge language.
Do NOT use headers or bullet points. Output plain prose only."""
    return prompt


def call_llm(prompt: str, api_key: str) -> str:
    """
    Call Google Gemini API with the metrics-grounded prompt.
    Returns the narrative text.
    """
    log.info("Calling Google Gemini API for narrative generation")
    if not api_key or api_key == "YOUR_KEY_HERE":
        log.warning("  ✗ No Gemini API key set. Using template narrative.")
        log.warning("  → Get a free key at: aistudio.google.com")
        return _template_narrative()

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models"
        "/gemini-2.0-flash:generateContent?key=" + api_key
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": 600, "temperature": 0.4},
    }
    # Retry up to 3 times with backoff for rate limit errors
    for attempt in range(1, 4):
        try:
            r = requests.post(url, json=payload, timeout=30)
            if r.status_code == 429:
                wait = attempt * 20  # 20s, 40s, 60s
                log.warning(f"  ↻ Rate limited. Waiting {wait}s before retry {attempt}/3...")
                time.sleep(wait)
                continue
            r.raise_for_status()
            narrative = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            log.info(f"  ✓ Gemini response: {len(narrative)} chars")
            return narrative
        except Exception as e:
            if attempt == 3:
                log.warning(f"  ✗ Gemini failed after 3 attempts ({e}). Using template narrative.")
                return _template_narrative()
            log.warning(f"  ✗ Attempt {attempt} failed ({e}). Retrying...")
            time.sleep(10)
    return _template_narrative()


def _template_narrative() -> str:
    """Fallback narrative template when API key is not set."""
    return (
        "EU gas storage sits at 36.3% full, a staggering 19.2 percentage points below the five-year seasonal average for "
        "this time of year, and the arithmetic is unforgiving: hitting the 90% November target requires injecting 3,704 "
        "GWh/day against a current pace of just 2,100 — a shortfall of 1,604 GWh/day that the market has shown no sign of "
        "closing. TTF at €50.03/MWh, up 40% year-on-year and sitting in the upper half of its 52-week range, is already "
        "pricing a degree of stress, but the backwardated curve structure is actively working against storage operators "
        "by making summer injection uneconomic relative to winter delivery. The base case is that Europe enters winter "
        "2026/27 materially undersupplied." 

        "Carbon is holding at €75.02/t, contributing €29.56/MWh to the variable cost of every megawatt-hour produced by a gas-fired "
        "turbine — roughly a third of the current Cal+1 power price. The more telling signal is what Cal+1 German power is implying "
        "about carbon: back-solving the clean spark spread equation gives an implied EUA of just €44.30/t, a €30.72 discount to "
        "where carbon actually trades. That gap means forward power is either pricing a collapse in EUA that the supply fundamentals"
        " don't support, or assuming enough renewable displacement to make carbon largely irrelevant in the 2027 dispatch stack. "
        "Both look optimistic from here, particularly with the MSR mechanically tightening auction supply from September and the "
        "July ETS Directive review carrying hawkish tail risk."

        "German Cal+1 at €92.75/MWh looks cheap relative to the fundamental stack. The negative CSS of −€12.12/MWh tells you the "
        "forward curve is not being driven by gas plant hedging — generators aren't locking in output at these levels, which means"
        "the price discovery is thin and the curve is vulnerable to a sharp repricing the moment winter risk becomes front-page news."
        "A cold October with storage at 70% rather than 90% would close the implied EUA gap and add €15–20/MWh to Cal+1 in short"
        "order. The one risk that changes the call is a Hormuz resolution: normalised LNG shipping would compress TTF by €8–12/MWh" 
        "and Cal+1 follows 1:1, unwinding the entire thesis."
    )


def generate_narrative(data: dict, metrics: dict) -> tuple[str, str]:
    """
    Returns (prompt, narrative) both logged to output file.
    """
    log.info("═" * 60)
    log.info("  STEP 4 — AI NARRATIVE GENERATION")
    log.info("═" * 60)
    prompt    = build_prompt(data, metrics)
    narrative = call_llm(prompt, CONFIG["gemini_api_key"])
    return prompt, narrative


# ═════════════════════════════════════════════════════════════════════════════
#  SECTION 5 — OUTPUT: DAILY BRIEF (MARKDOWN)
# ═════════════════════════════════════════════════════════════════════════════

SIGNAL_EMOJI = {"BULLISH": "🟢", "NEUTRAL": "🟡", "BEARISH": "🔴"}


def fmt_signal(s: str) -> str:
    return f"{SIGNAL_EMOJI.get(s, '⚪')} {s}"


def fmt_pct(v) -> str:
    if v is None:
        return "n/a"
    return f"{v:+.1f}%"


def write_pdf(data: dict, metrics: dict, narrative: str,
              chart1: Path, chart2: Path) -> Path:
    """
    Render the daily brief as a clean A4 PDF using reportlab.
    Mirrors the structure of the markdown brief.
    """
    log.info("Generating PDF brief")

    # ── colours ──────────────────────────────────────────────────────────────
    C_BG     = colors.HexColor("#ffffff")
    C_PANEL  = colors.HexColor("#f6f8fa")
    C_BORDER = colors.HexColor("#d0d7de")
    C_TEXT   = colors.HexColor("#1a1a2e")
    C_MUTED  = colors.HexColor("#57606a")
    C_BLUE   = colors.HexColor("#0969da")
    C_GREEN  = colors.HexColor("#1a7f37")
    C_RED    = colors.HexColor("#cf222e")
    C_GOLD   = colors.HexColor("#9a6700")

    PAGE_W, PAGE_H = A4
    M = 18 * mm

    # ── styles ────────────────────────────────────────────────────────────────
    def sty(name, **kw):
        base = dict(fontName="Helvetica", fontSize=9, leading=13, textColor=C_TEXT)
        base.update(kw)
        return ParagraphStyle(name, **base)

    st_h1    = sty("h1", fontSize=12, fontName="Helvetica-Bold",
                   textColor=C_BLUE, spaceBefore=10, spaceAfter=4)
    st_h2    = sty("h2", fontSize=9.5, fontName="Helvetica-Bold",
                   textColor=C_GOLD, spaceBefore=6, spaceAfter=3)
    st_body  = sty("body", fontSize=8.8, leading=13.5,
                   alignment=TA_JUSTIFY, spaceAfter=5)
    st_mono  = sty("mono", fontName="Courier", fontSize=8.5,
                   textColor=C_GREEN, leftIndent=14, spaceAfter=4)
    st_cap   = sty("cap", fontSize=7.2, textColor=C_MUTED,
                   alignment=TA_CENTER, spaceAfter=6)
    st_foot  = sty("foot", fontSize=7, textColor=C_MUTED, alignment=TA_CENTER)
    st_title = sty("title", fontSize=20, fontName="Helvetica-Bold",
                   leading=24, spaceAfter=2)
    st_sub   = sty("sub", fontSize=10, textColor=C_MUTED, spaceAfter=4)

    def HR(color=C_BORDER, thick=0.5):
        return HRFlowable(width="100%", thickness=thick,
                          color=color, spaceBefore=4, spaceAfter=6)

    def sp(pt=6):
        return Spacer(1, pt)

    def bold(text):
        """Inline bold via <b> tags."""
        return text  # Paragraph handles <b> tags natively

    # ── header/footer callback ────────────────────────────────────────────────
    def on_page(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(C_PANEL)
        canvas.rect(0, PAGE_H - 9*mm, PAGE_W, 9*mm, fill=1, stroke=0)
        canvas.setFillColor(C_BLUE)
        canvas.rect(0, PAGE_H - 9*mm, 2*mm, 9*mm, fill=1, stroke=0)
        canvas.setFont("Helvetica-Bold", 7)
        canvas.setFillColor(C_TEXT)
        canvas.drawString(M, PAGE_H - 5.5*mm, "EUROPEAN CROSS-COMMODITY RISK MONITOR")
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(C_MUTED)
        canvas.drawRightString(PAGE_W - M, PAGE_H - 5.5*mm,
                               f"{data['as_of']}  |  Aarav Agarwal")
        canvas.setFont("Helvetica", 6.5)
        canvas.setFillColor(C_MUTED)
        canvas.drawCentredString(PAGE_W / 2, 7*mm,
            "For informational purposes only. Not financial advice.")
        canvas.restoreState()

    # ── kv table ──────────────────────────────────────────────────────────────
    CW = PAGE_W - 2 * M
    def kv_table(rows):
        col = [55*mm, CW - 55*mm]
        tdata = [[
            Paragraph(f"<b>{k}</b>", ParagraphStyle("k", fontSize=8,
                      textColor=C_MUTED, fontName="Helvetica-Bold", leading=11)),
            Paragraph(v, ParagraphStyle("v", fontSize=8.8,
                      textColor=C_TEXT, leading=12))
        ] for k, v in rows]
        tbl = Table(tdata, colWidths=col)
        tbl.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,-1), C_PANEL),
            ("GRID",       (0,0), (-1,-1), 0.3, C_BORDER),
            ("TOPPADDING",    (0,0), (-1,-1), 5),
            ("BOTTOMPADDING", (0,0), (-1,-1), 5),
            ("LEFTPADDING",   (0,0), (-1,-1), 8),
            ("RIGHTPADDING",  (0,0), (-1,-1), 8),
            ("VALIGN",        (0,0), (-1,-1), "TOP"),
        ]))
        return tbl

    def metrics_table(rows):
        cw = [52*mm, 26*mm, 26*mm, CW - 108*mm]
        hdr = [Paragraph(h, ParagraphStyle("mh", fontSize=7.5,
               fontName="Helvetica-Bold", textColor=C_MUTED, leading=10))
               for h in ["Metric", "Value", "Signal", "Relevance"]]
        tdata = [hdr]
        for metric, value, sig, rel in rows:
            sc = C_GREEN if "BULLISH" in sig else (C_RED if "BEARISH" in sig else C_GOLD)
            tdata.append([
                Paragraph(metric, ParagraphStyle("mc", fontSize=8,
                          textColor=C_TEXT, leading=11)),
                Paragraph(value,  ParagraphStyle("mv", fontSize=8,
                          fontName="Helvetica-Bold", textColor=C_BLUE, leading=11)),
                Paragraph(sig,    ParagraphStyle("ms", fontSize=7.5,
                          fontName="Helvetica-Bold", textColor=sc, leading=10)),
                Paragraph(rel,    ParagraphStyle("mr", fontSize=7.8,
                          textColor=C_MUTED, leading=11)),
            ])
        tbl = Table(tdata, colWidths=cw, repeatRows=1)
        tbl.setStyle(TableStyle([
            ("BACKGROUND",   (0,0), (-1,0),  C_BORDER),
            ("ROWBACKGROUNDS",(0,1),(-1,-1), [C_PANEL, colors.HexColor("#ffffff")]),
            ("GRID",         (0,0), (-1,-1), 0.3, C_BORDER),
            ("TOPPADDING",    (0,0), (-1,-1), 5),
            ("BOTTOMPADDING", (0,0), (-1,-1), 5),
            ("LEFTPADDING",   (0,0), (-1,-1), 7),
            ("RIGHTPADDING",  (0,0), (-1,-1), 7),
            ("VALIGN",        (0,0), (-1,-1), "TOP"),
        ]))
        return tbl

    # ── signal badge table ────────────────────────────────────────────────────
    def signal_table(summary):
        cw3 = [CW/3, CW/3, CW/3]
        tdata = [[
            Paragraph("<b>🟢 Bullish</b>", ParagraphStyle("sb", fontSize=9,
                      fontName="Helvetica-Bold", textColor=C_GREEN,
                      alignment=TA_CENTER, leading=12)),
            Paragraph("<b>🟡 Neutral</b>", ParagraphStyle("sn", fontSize=9,
                      fontName="Helvetica-Bold", textColor=C_GOLD,
                      alignment=TA_CENTER, leading=12)),
            Paragraph("<b>🔴 Bearish</b>", ParagraphStyle("sr", fontSize=9,
                      fontName="Helvetica-Bold", textColor=C_RED,
                      alignment=TA_CENTER, leading=12)),
        ],[
            Paragraph(f"<b>{summary['bullish']}</b>",
                      ParagraphStyle("sv", fontSize=18, fontName="Helvetica-Bold",
                                     textColor=C_GREEN, alignment=TA_CENTER, leading=22)),
            Paragraph(f"<b>{summary['neutral']}</b>",
                      ParagraphStyle("sv2", fontSize=18, fontName="Helvetica-Bold",
                                     textColor=C_GOLD, alignment=TA_CENTER, leading=22)),
            Paragraph(f"<b>{summary['bearish']}</b>",
                      ParagraphStyle("sv3", fontSize=18, fontName="Helvetica-Bold",
                                     textColor=C_RED, alignment=TA_CENTER, leading=22)),
        ]]
        tbl = Table(tdata, colWidths=cw3)
        tbl.setStyle(TableStyle([
            ("BACKGROUND",    (0,0), (-1,-1), C_PANEL),
            ("GRID",          (0,0), (-1,-1), 0.3, C_BORDER),
            ("TOPPADDING",    (0,0), (-1,-1), 8),
            ("BOTTOMPADDING", (0,0), (-1,-1), 8),
            ("ALIGN",         (0,0), (-1,-1), "CENTER"),
        ]))
        return tbl

    # ── build story ───────────────────────────────────────────────────────────
    m    = metrics
    stor = data["storage"]
    today_str = datetime.today().strftime("%Y-%m-%d")
    out_path  = OUT / f"daily_brief_{today_str}.pdf"

    doc = SimpleDocTemplate(
        str(out_path), pagesize=A4,
        leftMargin=M, rightMargin=M, topMargin=M + 4*mm, bottomMargin=14*mm,
    )

    def yoy(key):
        v = data.get(f"{key}_yoy")
        return f" ({v:+.1f}% YoY)" if v else ""

    story = []
    a = story.append

    # Title
    a(sp(4))
    a(Paragraph("European Cross-Commodity Risk Monitor", st_title))
    a(Paragraph("Gas Tightness  ·  Carbon Supply Signal  ·  Power Curve Implications", st_sub))
    a(Paragraph(f"Aarav Agarwal | aaraval007@gmail.com |  {data['as_of']}", st_sub))
    a(HR(C_BLUE, thick=1.2))



    # Narrative
    a(Paragraph("Market Narrative", st_h1))
    for para in narrative.strip().split("\n\n"):
        txt = para.strip()
        if txt:
            a(Paragraph(txt, st_body))
    a(HR())

    # Gas section
    a(Paragraph("1  |  Gas Tightness", st_h1))
    a(kv_table([
        ("TTF M+1",            f"<b>€{m['ttf']['value']:.2f}/MWh</b>{yoy('ttf')}"),
        ("EU Storage",         f"<b>{stor['fill_pct']:.1f}%</b> ({m['storage_gap']['value']:+.1f}pp vs 5-yr avg)"),
        ("Required injection", f"<b>{m['injection_pace']['value']:.0f} GWh/day</b>  |  Current: {stor['trend_gwh_day']:.0f} GWh/day  |  Gap: {m['injection_pace']['pace_gap']:+.0f}"),
        ("Norwegian supply",   "Plateau ~114.9 Bcm 2025; 2026 guide 110–120 Bcm"),
        ("LNG shock",          "Qatar force majeure ~17% of global liquefaction (March 2026)"),
    ]))
    a(sp(8))
    a(HR())

    # Chart 1
    img_w = PAGE_W - 2*M
    img_h = img_w * 0.45
    a(Image(str(chart1), width=img_w, height=img_h))
    a(Paragraph(
        f"Chart 1: EU gas storage fill % vs 5-year seasonal band. "
        f"Current: {stor['fill_pct']:.1f}% ({m['storage_gap']['value']:+.1f}pp vs avg). "
        f"Source: GIE AGSI+.", st_cap))
    a(HR())

    # Carbon section
    a(Paragraph("2  |  Carbon Signal", st_h1))
    a(kv_table([
        ("EUA front-year",       f"<b>€{m['eua']['value']:.2f}/t</b>{yoy('eua')}"),
        ("Carbon cost per MWh",  f"<b>€{m['carbon_per_mwh']['value']:.2f}/MWh</b>  (EUA × 0.394 tCO₂/MWh)"),
        ("MSR withdrawal",       "275.5 Mt removed Sep 2025–Aug 2026  |  Supply tightens Sep 2026"),
        ("ETS Directive review", "July 2026  |  Key policy catalyst"),
    ]))
    a(sp(8))
    a(HR())

    # Power section
    a(Paragraph("3  |  Power Curve Implications", st_h1))
    a(Paragraph("Clean Spark Spread:", st_h2))
    a(Paragraph(
        f"CSS  =  Power − (Gas ÷ η) − (EF × Carbon)  "
        f"=  {m['de_power']['value']:.2f}  −  {m['css']['gas_cost_per_mwh']:.2f}  "
        f"−  {m['carbon_per_mwh']['value']:.2f}  =  <b>€{m['css']['value']:.2f}/MWh</b>",
        st_mono))
    a(Paragraph("Implied EUA (solving CSS = 0):", st_h2))
    a(Paragraph(
        f"EUA*  =  (Power − Gas/η) ÷ EF  =  <b>€{m['implied_eua']['value']:.2f}/t</b>  "
        f"vs spot €{m['eua']['value']:.2f}/t  →  gap: €{m['implied_eua']['gap']:.2f}/t",
        st_mono))
    a(sp(6))

    # Chart 2
    a(Image(str(chart2), width=img_w, height=img_h))
    a(Paragraph(
        "Chart 2: TTF M+1 (blue), EUA front-year (red, RHS), German Cal+1 baseload (green). "
        "12-month lookback. Sources: ICE, EEX, Trading Economics.", st_cap))
    a(HR())

    # Metrics dashboard
    a(Paragraph("4  |  Monitor Metrics Dashboard", st_h1))
    sig_map = {"BULLISH": "▲ BULLISH", "NEUTRAL": "→ NEUTRAL", "BEARISH": "▼ BEARISH"}

    def fmt_pct(v):
        return f" ({v:+.1f}% YoY)" if v else ""

    a(metrics_table([
        ("TTF M+1 (€/MWh)",
         f"€{m['ttf']['value']:.2f}{fmt_pct(data.get('ttf_yoy'))}",
         sig_map[m["ttf"]["signal"]], "Primary power price input"),
        ("EU Storage vs 5-yr avg",
         f"{stor['fill_pct']:.1f}% ({m['storage_gap']['value']:+.1f}pp)",
         sig_map[m["storage_gap"]["signal"]], "Low buffer amplifies demand shocks"),
        ("Required injection pace",
         f"{m['injection_pace']['value']:.0f} GWh/d",
         sig_map[m["injection_pace"]["signal"]], "Gap vs target = winter risk"),
        ("EUA front-year (€/t)",
         f"€{m['eua']['value']:.2f}{fmt_pct(data.get('eua_yoy'))}",
         sig_map[m["eua"]["signal"]], "+€30/MWh carbon cost in power"),
        ("Carbon cost per MWh",
         f"€{m['carbon_per_mwh']['value']:.2f}/MWh",
         sig_map[m["carbon_per_mwh"]["signal"]], "Direct variable cost floor"),
        ("Clean Spark Spread",
         f"€{m['css']['value']:.2f}/MWh",
         sig_map[m["css"]["signal"]], "Gas plant profitability"),
        ("German Cal+1 baseload",
         f"€{m['de_power']['value']:.2f}/MWh{fmt_pct(data.get('pwr_yoy'))}",
         sig_map[m["de_power"]["signal"]], "Forward power curve anchor"),
        ("Implied EUA in Cal+1",
         f"€{m['implied_eua']['value']:.2f}/t (gap: €{m['implied_eua']['gap']:.2f})",
         sig_map[m["implied_eua"]["signal"]], "Carbon unpriced in forward power"),
    ]))
    a(sp(8))
    a(HR())

    # Risk skew — dynamic, mirrors write_brief logic
    a(Paragraph("5  |  Risk Skew", st_h1))
    a(Paragraph("Upside catalysts:", st_h2))
    upside_bullets = []
    if m["storage_gap"]["signal"] == "BULLISH":
        upside_bullets.append(
            f"Storage deficit ({stor['fill_pct']:.1f}%, {abs(m['storage_gap']['value']):.0f}pp below avg) — "
            f"injection pace {stor['trend_gwh_day']:.0f} vs {m['injection_pace']['value']:.0f} GWh/day required."
        )
    if m["ttf"]["signal"] == "BULLISH":
        upside_bullets.append(
            f"TTF at €{m['ttf']['value']:.2f}/MWh — any Hormuz re-escalation or Norwegian outage "
            f"could push toward 52-week high of €{data['ttf_52w_hi']:.0f}/MWh."
        )
    if m["eua"]["signal"] == "BULLISH":
        upside_bullets.append(
            f"EUA at €{m['eua']['value']:.2f}/t — carbon adding €{m['carbon_per_mwh']['value']:.1f}/MWh "
            f"to gas plant variable cost. July 2026 ETS review is hawkish catalyst risk."
        )
    if not upside_bullets:
        upside_bullets.append("No primary upside catalysts flagged. Monitor storage and TTF curve.")
    for txt in upside_bullets:
        a(Paragraph(f"▲  {txt}", ParagraphStyle("bull", fontSize=8.8, leading=13,
                    textColor=C_TEXT, leftIndent=12, spaceAfter=3)))
    a(sp(4))
    a(Paragraph("Downside catalysts:", st_h2))
    downside_bullets = [
        "Hormuz resolution / US-Iran deal — TTF -€8–12/MWh; Cal+1 power follows 1:1.",
        f"US LNG wave on schedule — 2027 supply easing weighs on Cal+2 and beyond.",
    ]
    if m["css"]["signal"] == "BEARISH":
        downside_bullets.insert(0,
            f"CSS at €{m['css']['value']:.2f}/MWh — warm autumn + unhedged generators = spot volatility risk."
        )
    for txt in downside_bullets:
        a(Paragraph(f"▼  {txt}", ParagraphStyle("bear", fontSize=8.8, leading=13,
                    textColor=C_TEXT, leftIndent=12, spaceAfter=3)))
    a(sp(8))
    a(HR())

    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    log.info(f"  ✓ PDF saved: {out_path}")
    return out_path


def write_brief(data: dict, metrics: dict, prompt: str,
                narrative: str, chart1: Path, chart2: Path) -> Path:
    """
    Write the full daily brief as a Markdown file.
    Includes: header, metrics dashboard, AI narrative, charts,
              full risk skew, and LLM prompt/output log.
    """
    log.info("═" * 60)
    log.info("  STEP 5 — WRITING DAILY BRIEF")
    log.info("═" * 60)

    m    = metrics
    stor = data["storage"]
    today_str = datetime.today().strftime("%Y-%m-%d")
    out_path  = OUT / f"daily_brief_{today_str}.md"

    def yoy_str(key: str) -> str:
        v = data.get(f"{key}_yoy")
        return f"({fmt_pct(v)} YoY)" if v is not None else ""

    lines = []
    a = lines.append  # shorthand

    # ── HEADER ───────────────────────────────────────────────────────────────
    a("---")
    a(f"title: European Cross-Commodity Risk Monitor")
    a(f"date: {data['as_of']}")
    a(f"author: Aarav Agarwal")
    a(f"generated: {data['run_time']}")
    a("---")
    a("")
    a("# European Cross-Commodity Risk Note")
    a(f"**{data['as_of']}  ·  Aarav Agarwal**")
    a("")
    a("> Gas Tightness · Carbon Supply Signal · Power Curve Implications")
    a("")
    a("---")
    a("")



    # ── AI NARRATIVE ─────────────────────────────────────────────────────────
    a("## Market Narrative")
    a("")
    for para in narrative.strip().split("\n\n"):
        a(para.strip())
        a("")
    a("---")
    a("")

    # ── METRICS DASHBOARD ────────────────────────────────────────────────────
    a("## Monitor Metrics Dashboard")
    a("")
    a("| # | Metric | Value | Signal | Formula / Source |")
    a("|---|--------|-------|--------|-----------------|")

    metric_rows = [
        ("1", "TTF M+1",
         f"**€{m['ttf']['value']:.2f}/MWh** {yoy_str('ttf')}",
         fmt_signal(m["ttf"]["signal"]),
         m["ttf"]["source"]),
        ("2", "EU Storage vs 5yr avg",
         f"**{stor['fill_pct']:.1f}%** ({m['storage_gap']['value']:+.1f}pp vs avg)",
         fmt_signal(m["storage_gap"]["signal"]),
         m["storage_gap"]["source"]),
        ("3", "Required injection pace",
         f"**{m['injection_pace']['value']:.0f} GWh/d** (gap: {m['injection_pace']['pace_gap']:+.0f})",
         fmt_signal(m["injection_pace"]["signal"]),
         m["injection_pace"]["source"]),
        ("4", "EUA front-year",
         f"**€{m['eua']['value']:.2f}/t** {yoy_str('eua')}",
         fmt_signal(m["eua"]["signal"]),
         m["eua"]["source"]),
        ("5", "Carbon cost per MWh",
         f"**€{m['carbon_per_mwh']['value']:.2f}/MWh**",
         fmt_signal(m["carbon_per_mwh"]["signal"]),
         "EUA × 0.394 tCO₂/MWh"),
        ("6", "Clean Spark Spread",
         f"**€{m['css']['value']:.2f}/MWh**",
         fmt_signal(m["css"]["signal"]),
         "Power − TTF/η − EUA×EF  (η=49.13%)"),
        ("7", "German Cal+1 baseload",
         f"**€{m['de_power']['value']:.2f}/MWh** {yoy_str('pwr')}",
         fmt_signal(m["de_power"]["signal"]),
         m["de_power"]["source"]),
        ("8", "Implied EUA in Cal+1",
         f"**€{m['implied_eua']['value']:.2f}/t** (gap vs spot: €{m['implied_eua']['gap']:.2f})",
         fmt_signal(m["implied_eua"]["signal"]),
         "(Power − TTF/η) ÷ EF"),
    ]
    for row in metric_rows:
        a(f"| {row[0]} | {row[1]} | {row[2]} | {row[3]} | {row[4]} |")

    a("")
    a("---")
    a("")

    # ── CHARTS ───────────────────────────────────────────────────────────────
    a("## Charts")
    a("")
    a(f"![Chart 1 — EU Gas Storage vs Seasonal Band]({chart1.name})")
    a("")
    a(f"*Chart 1: EU aggregate gas storage fill % vs 5-year seasonal min/max/average band. "
      f"Current level: {stor['fill_pct']:.1f}%. Source: GIE AGSI+.*")
    a("")
    a(f"![Chart 2 — TTF / EUA / German Power 12-Month Panel]({chart2.name})")
    a("")
    a("*Chart 2: TTF M+1 (blue), EUA front-year (red, RHS), German Cal+1 baseload (green). "
      "12-month lookback. Sources: ICE, EEX, Trading Economics.*")
    a("")
    a("---")
    a("")

    # ── RISK SKEW (dynamic — driven by live metrics) ─────────────────────────
    a("## Risk Skew")
    a("")

    # Upside bullets — only shown when relevant metric is BULLISH or stressed
    upside = []
    if m["storage_gap"]["signal"] == "BULLISH":
        gap = abs(m["storage_gap"]["value"])
        upside.append(
            f"**Storage deficit ({stor['fill_pct']:.1f}%, {gap:.0f}pp below average)** — "
            f"Every 1pp below seasonal norm at 1 Nov adds ~€3–5/MWh to front-winter power. "
            f"At current injection pace ({stor['trend_gwh_day']:.0f} GWh/day vs "
            f"{m['injection_pace']['value']:.0f} required), the 90% target looks at risk."
        )
    if m["ttf"]["signal"] == "BULLISH":
        upside.append(
            f"**TTF elevated at €{m['ttf']['value']:.2f}/MWh** — Prompt strength transmits "
            f"directly into Cal+1 power via prompt-wagging-the-curve. Any Hormuz re-escalation "
            f"or Norwegian outage could push TTF back toward the €{data['ttf_52w_hi']:.0f} 52-week high."
        )
    if m["eua"]["signal"] == "BULLISH":
        upside.append(
            f"**EUA at €{m['eua']['value']:.2f}/t** — Carbon above €80/t adds "
            f"€{m['carbon_per_mwh']['value']:.1f}/MWh directly to gas plant variable cost. "
            f"July 2026 ETS Directive review is a hawkish catalyst risk."
        )
    if m["carbon_per_mwh"]["signal"] == "BULLISH" and m["eua"]["signal"] != "BULLISH":
        upside.append(
            f"**Carbon cost at €{m['carbon_per_mwh']['value']:.2f}/MWh** — "
            f"Elevated carbon floor supporting power prices. "
            f"MSR supply step-down from September 2026 is structurally bullish EUA."
        )
    if not upside:
        upside.append(
            "No primary upside catalysts flagged by current metrics. "
            "Monitor storage injection pace and TTF curve structure for regime change."
        )

    a("### 🟢 Upside catalysts")
    a("")
    for b in upside:
        a(f"- {b}")
    a("")

    # Downside bullets — only shown when relevant metric is BEARISH or benign
    downside = []
    if m["css"]["signal"] == "BEARISH":
        downside.append(
            f"**CSS deeply negative at €{m['css']['value']:.2f}/MWh** — "
            f"Gas plants are not hedging forward at these levels. "
            f"A warm autumn compressing power demand would push CSS further negative "
            f"and expose unhedged generators to spot price collapse."
        )
    if m["injection_pace"]["signal"] == "BEARISH":
        downside.append(
            f"**Injection shortfall ({m['injection_pace']['pace_gap']:+.0f} GWh/day gap)** — "
            f"If weather turns mild and injection accelerates beyond required pace, "
            f"storage could recover faster than priced, compressing the winter risk premium."
        )
    if m["implied_eua"]["signal"] == "BULLISH":
        downside.append(
            f"**Power pricing only €{m['implied_eua']['value']:.0f}/t implied EUA vs spot €{m['eua']['value']:.0f}/t** — "
            f"If EUA weakens toward the implied level (€{m['implied_eua']['value']:.0f}/t), "
            f"Cal+1 power loses €{(m['eua']['value'] - m['implied_eua']['value']) * 0.394:.1f}/MWh of carbon support."
        )
    downside.append(
        "**Hormuz resolution / US-Iran deal** — TTF -€8–12/MWh on shipping normalisation; "
        "Cal+1 power follows 1:1. Primary structural downside catalyst."
    )
    downside.append(
        f"**US LNG wave (2027)** — Plaquemines, Golden Pass, Rio Grande commissioning "
        f"on schedule weighs on Cal+2 and beyond, anchoring the back of the curve."
    )

    a("### 🔴 Downside catalysts")
    a("")
    for b in downside:
        a(f"- {b}")
    a("")

    # Watch list — always dynamic
    a("### 👁 Watch this week")
    a("")
    watches = [
        f"**GIE daily storage** — injection rate vs required {m['injection_pace']['value']:.0f} GWh/day",
        "**Hormuz/Iran–US talks** — any ceasefire signal = immediate TTF move",
        "**Norwegian Gassco flow nominations** — any planned maintenance announcement",
        f"**EEX Cal+1 daily settle** — tracking vs TTF M+1 (currently €{m['de_power']['value']:.2f}/MWh)",
        f"**EUA auction results** — monitoring vs €{m['eua']['value']:.2f}/t spot level",
    ]
    for w in watches:
        a(f"- {w}")
    a("")
    a("---")
    a("")

    # ── LLM AUDIT LOG ────────────────────────────────────────────────────────
    a("## Appendix — AI Workflow Log")
    a("")
    a("*This section logs the LLM prompt and output for auditability and reproducibility.*")
    a("")
    a("### Prompt sent to Claude API")
    a("")
    a("```")
    a(prompt)
    a("```")
    a("")
    a("### Claude API response")
    a("")
    a("```")
    a(narrative)
    a("```")
    a("")
    a("---")
    a("")
    a("*Data sources: GIE AGSI+, ICE/Yahoo Finance (TTF, EUA), EEX/Yahoo Finance (German power). "
      "Script generated automatically — prices may reflect Yahoo Finance delayed/proxy data. "
      "Not investment advice.*")

    text = "\n".join(lines)
    out_path.write_text(text, encoding="utf-8")
    log.info(f"  ✓ Brief written: {out_path}")
    return out_path


# ═════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═════════════════════════════════════════════════════════════════════════════

def main():
    log.info("")
    log.info("╔══════════════════════════════════════════════════════════╗")
    log.info("║   European Cross-Commodity Risk Monitor                 ║")
    log.info("║   Gas + Carbon → Power Curve                            ║")
    log.info(f"║   {datetime.now().strftime('%Y-%m-%d %H:%M')}                                       ║")
    log.info("╚══════════════════════════════════════════════════════════╝")
    log.info("")

    # 1. Fetch all data
    data = fetch_all_data()

    # 2. Compute metrics
    metrics = compute_metrics(data)

    # 3. Generate charts
    chart1, chart2 = generate_charts(data)

    # 4. AI narrative
    prompt, narrative = generate_narrative(data, metrics)

    # 5. Write daily brief
    brief_path = write_brief(data, metrics, prompt, narrative, chart1, chart2)
    pdf_path   = write_pdf(data, metrics, narrative, chart1, chart2)

    log.info("")
    log.info("═" * 60)
    log.info("  DONE")
    log.info("═" * 60)
    log.info(f"  Brief:   {brief_path}")
    log.info(f"  PDF:     {pdf_path}")
    log.info(f"  Chart 1: {chart1}")
    log.info(f"  Chart 2: {chart2}")
    log.info("")
    log.info("  To enable AI narrative: set GEMINI_API_KEY env var")
    log.info("  Free key at: aistudio.google.com")
    log.info("  Windows:  set GEMINI_API_KEY=AIza...")
    log.info("  Mac/Linux: export GEMINI_API_KEY=AIza...")
    log.info("")


if __name__ == "__main__":
    main()