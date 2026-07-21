#!/usr/bin/env python3
"""
Phase 3 Paper Trading Engine
=============================
Extends the original phase_3 backtest script into a stateful, daily-runnable
PAPER trading engine (no real money, no brokerage connection).
"""

import argparse
import json
import os
from datetime import datetime, date
from curl_cffi import requests

import numpy as np
import pandas as pd
import yfinance as yf

# ----------------------------------------------------------------------
# CONFIG — mirrors the original phase_3 script
# ----------------------------------------------------------------------
TICKERS = ["NVDA", "TQQQ", "SMH", "USD", "IBIT", "UPRO"]
BENCHMARK = "SPY"
STARTING_CAPITAL = 1000.0
TXN_COST = 0.0005          # 0.05% friction per rebalance trade
CASH_YIELD_ANNUAL = 0.045  # 4.5% annual yield on uninvested cash
DAILY_CASH_RATE = CASH_YIELD_ANNUAL / 252
HISTORY_DAYS = 500         # trading days of history pulled each run
TOP_N = 2                  # top-N assets by trend intensity get allocated

HORIZONS = [
    {"sma": 40,  "max": 12, "min": 6,  "weight": 0.35},
    {"sma": 120, "max": 22, "min": 8,  "weight": 0.65},
]

DATA_DIR = os.environ.get("DATA_DIR", ".")
STATE_FILE = os.path.join(DATA_DIR, "paper_state.json")
DASHBOARD_FILE = os.path.join(DATA_DIR, "dashboard_data.json")
os.makedirs(DATA_DIR, exist_ok=True)


# ----------------------------------------------------------------------
# 1. DATA
# ----------------------------------------------------------------------
def fetch_price_history():
    """Pull recent daily closes for the universe + benchmark fast with curl_cffi."""
    all_tickers = TICKERS + [BENCHMARK]
    session = requests.Session(impersonate="chrome")
    raw = yf.download(all_tickers, period=f"{HISTORY_DAYS}d", session=session, progress=False)
    close_df = raw["Close"].ffill().bfill()
    return close_df


# ----------------------------------------------------------------------
# 2. SIGNAL ENGINE
# ----------------------------------------------------------------------
def compute_master_signals(close_df, tickers, horizons):
    """Replicates the dual-horizon trend-state detector."""
    close_vals = close_df[tickers].values
    shifted_close = close_df[tickers].shift(1).values
    num_days, num_assets = close_vals.shape
    master_signals = np.zeros_like(close_vals)

    for horizon in horizons:
        sma_w, t_max, t_min, strat_w = horizon["sma"], horizon["max"], horizon["min"], horizon["weight"]
        sma_vals = close_df[tickers].rolling(sma_w).mean().values
        trend_signals = np.zeros_like(close_vals)
        trend_states = np.zeros(num_assets)

        for t in range(max(sma_w, t_max), num_days):
            max_ch = np.max(shifted_close[t - t_max:t], axis=0)
            min_ch = np.min(shifted_close[t - t_min:t], axis=0)
            for asset in range(num_assets):
                if close_vals[t, asset] > max_ch[asset]:
                    trend_states[asset] = 1.0
                elif close_vals[t, asset] < min_ch[asset]:
                    trend_states[asset] = 0.0

                if close_vals[t, asset] <= sma_vals[t, asset]:
                    trend_signals[t, asset] = 0.0
                else:
                    trend_signals[t, asset] = trend_states[asset] * strat_w

        master_signals += trend_signals

    return master_signals


def compute_target_weights_today(close_df, tickers=TICKERS, horizons=HORIZONS, top_n=TOP_N):
    master_signals = compute_master_signals(close_df, tickers, horizons)
    num_assets = len(tickers)

    today_signal = master_signals[-1]

    rolling_vol = close_df[tickers].pct_change().rolling(21).std().fillna(0.01).values[-1]
    inv_vol = 1.0 / np.where(rolling_vol == 0, 0.01, rolling_vol)

    sma_120 = close_df[tickers].rolling(120).mean().values[-1]
    close_today = close_df[tickers].values[-1]
    trend_intensity = np.where(sma_120 > 0, (close_today - sma_120) / sma_120, 0)
    active_intensity = trend_intensity * (today_signal > 0).astype(float)

    top_indices = np.argsort(active_intensity)[-top_n:]
    rank_mask = np.zeros(num_assets)
    rank_mask[top_indices] = 1.0

    filtered_signals = today_signal * rank_mask
    weighted_signals = filtered_signals * inv_vol
    total_vol_weight = np.sum(weighted_signals)

    if total_vol_weight == 0:
        base_weights = np.zeros(num_assets)
    else:
        base_weights = weighted_signals / total_vol_weight

    total_active_signal = np.clip(np.sum(filtered_signals), 0.0, 1.0)
    final_weights = base_weights * total_active_signal

    return {t: float(w) for t, w in zip(tickers, final_weights)}


# ----------------------------------------------------------------------
# 3. PAPER PORTFOLIO STATE
# ----------------------------------------------------------------------
def default_state():
    return {
        "created": datetime.now().strftime("%Y-%m-%d"),
        "starting_capital": STARTING_CAPITAL,
        "cash": STARTING_CAPITAL,
        "last_run": None,
        "positions": {},
        "closed_trades": [],
        "equity_curve": [],
        "spy_shares_ref": None,
    }


def load_state(path=STATE_FILE):
    if not os.path.exists(path):
        return default_state()
    with open(path, "r") as f:
        return json.load(f)


def save_state(state, path=STATE_FILE):
    with open(path, "w") as f:
        json.dump(state, f, indent=2, default=str)


# ----------------------------------------------------------------------
# 4. REBALANCE
# ----------------------------------------------------------------------
def rebalance(state, target_weights, prices, run_date):
    state["cash"] += state["cash"] * DAILY_CASH_RATE

    equity = state["cash"] + sum(
        pos["shares"] * prices[t] for t, pos in state["positions"].items() if t in prices
    )

    for ticker in TICKERS:
        price = prices.get(ticker)
        if price is None or price <= 0:
            continue

        target_value = equity * target_weights.get(ticker, 0.0)
        pos = state["positions"].get(ticker, {
            "shares": 0.0, "avg_cost": 0.0, "open_date": run_date,
            "realized_pl_accum": 0.0, "cost_basis_accum": 0.0,
        })
        current_value = pos["shares"] * price
        delta_value = target_value - current_value
        if abs(delta_value) < 1.0:
            continue

        delta_shares = delta_value / price
        friction_cost = abs(delta_value) * TXN_COST
        state["cash"] -= friction_cost

        if delta_shares > 0:
            new_shares = pos["shares"] + delta_shares
            pos["avg_cost"] = (
                (pos["shares"] * pos["avg_cost"] + delta_shares * price) / new_shares
                if new_shares > 0 else price
            )
            pos["cost_basis_accum"] += delta_shares * price
            if pos["shares"] == 0:
                pos["open_date"] = run_date
            pos["shares"] = new_shares
            state["cash"] -= delta_value
        else:
            sell_shares = min(-delta_shares, pos["shares"])
            realized = sell_shares * (price - pos["avg_cost"])
            pos["realized_pl_accum"] += realized
            pos["shares"] -= sell_shares
            state["cash"] += sell_shares * price

            if pos["shares"] <= 1e-6:
                total_cost_basis = pos["cost_basis_accum"] if pos["cost_basis_accum"] > 0 else 1e-9
                pl_pct = (pos["realized_pl_accum"] / total_cost_basis) * 100
                state["closed_trades"].append({
                    "ticker": ticker,
                    "open_date": pos["open_date"],
                    "close_date": run_date,
                    "pl": round(pos["realized_pl_accum"], 2),
                    "pl_pct": round(pl_pct, 2),
                    "win": pos["realized_pl_accum"] > 0,
                })
                pos = {"shares": 0.0, "avg_cost": 0.0, "open_date": None,
                       "realized_pl_accum": 0.0, "cost_basis_accum": 0.0}

        state["positions"][ticker] = pos

    state["positions"] = {t: p for t, p in state["positions"].items() if p["shares"] > 1e-6}
    state["last_run"] = run_date
    return state


# ----------------------------------------------------------------------
# 5. METRICS + EXPORT
# ----------------------------------------------------------------------
def compute_metrics(state, prices, spy_price):
    positions_out = []
    open_value = 0.0
    for ticker, pos in state["positions"].items():
        price = prices.get(ticker, pos["avg_cost"])
        mkt_val = pos["shares"] * price
        open_value += mkt_val
        unreal_pl = mkt_val - pos["shares"] * pos["avg_cost"]
        unreal_pl_pct = (unreal_pl / (pos["shares"] * pos["avg_cost"]) * 100) if pos["avg_cost"] > 0 else 0.0
        positions_out.append({
            "ticker": ticker,
            "shares": round(pos["shares"], 4),
            "avg_cost": round(pos["avg_cost"], 2),
            "last_price": round(price, 2),
            "market_value": round(mkt_val, 2),
            "unrealized_pl": round(unreal_pl, 2),
            "unrealized_pl_pct": round(unreal_pl_pct, 2),
        })

    equity = state["cash"] + open_value
    for p in positions_out:
        p["weight_pct"] = round((p["market_value"] / equity * 100) if equity else 0.0, 2)

    total_pl = equity - state["starting_capital"]
    total_pl_pct = (total_pl / state["starting_capital"]) * 100

    closed = state["closed_trades"]
    wins = [t for t in closed if t["win"]]
    losses = [t for t in closed if not t["win"]]
    win_rate = (len(wins) / len(closed) * 100) if closed else 0.0
    best = max((t["pl_pct"] for t in closed), default=0.0)
    worst = min((t["pl_pct"] for t in closed), default=0.0)

    if state.get("spy_shares_ref") is None and spy_price:
        state["spy_shares_ref"] = state["starting_capital"] / spy_price
    spy_equity = (state["spy_shares_ref"] * spy_price) if state.get("spy_shares_ref") and spy_price else None

    metrics = {
        "total_equity": round(equity, 2),
        "cash": round(state["cash"], 2),
        "total_pl": round(total_pl, 2),
        "total_pl_pct": round(total_pl_pct, 2),
        "win_rate_pct": round(win_rate, 2),
        "num_closed_trades": len(closed),
        "num_wins": len(wins),
        "num_losses": len(losses),
        "best_trade_pct": round(best, 2),
        "worst_trade_pct": round(worst, 2),
        "spy_equity": round(spy_equity, 2) if spy_equity else None,
        "spy_total_pl_pct": round((spy_equity - state["starting_capital"]) / state["starting_capital"] * 100, 2) if spy_equity else None,
    }
    return positions_out, metrics, equity, spy_equity


def export_dashboard(state, positions_out, metrics, run_date, target_weights=None):
    payload = {
        "engine_name": "Phase 3: Trend Ensemble",
        "tickers": TICKERS,
        "benchmark": BENCHMARK,
        "starting_capital": state["starting_capital"],
        "as_of": run_date,
        "positions": positions_out,
        "closed_trades": state["closed_trades"],
        "equity_curve": state["equity_curve"],
        "metrics": metrics,
        "target_weights_today": target_weights or {},
    }
    with open(DASHBOARD_FILE, "w") as f:
        json.dump(payload, f, indent=2, default=str)
    return payload


# ----------------------------------------------------------------------
# 6. CLI
# ----------------------------------------------------------------------
def print_summary(metrics, run_date):
    print(f"\n================ PHASE 3 PAPER PORTFOLIO — {run_date} ================")
    print(f"Equity           : ${metrics['total_equity']:,.2f}  (cash ${metrics['cash']:,.2f})")
    print(f"Total P/L        : ${metrics['total_pl']:,.2f}  ({metrics['total_pl_pct']:+.2f}%)")
    if metrics["spy_total_pl_pct"] is not None:
        print(f"SPY buy&hold P/L : {metrics['spy_total_pl_pct']:+.2f}%")
    print(f"Win rate         : {metrics['win_rate_pct']:.1f}%  ({metrics['num_wins']}W / {metrics['num_losses']}L, {metrics['num_closed_trades']} closed trades)")
    print("=======================================================================\n")


def run():
    run_date = date.today().strftime("%Y-%m-%d")
    state = load_state()

    if state.get("last_run") == run_date:
        print(f"Already ran today ({run_date}). Use 'status' to view current stats without re-trading.")
        if os.path.exists(DASHBOARD_FILE):
            with open(DASHBOARD_FILE, "r") as f:
                return json.load(f)
        return None

    print("Fetching latest price history (fast batch mode)...")
    close_df = fetch_price_history()

    prices = {t: float(close_df[t].dropna().iloc[-1]) for t in TICKERS if t in close_df}
    spy_price = float(close_df[BENCHMARK].dropna().iloc[-1]) if BENCHMARK in close_df else None
    print("Fetched prices:", prices)

    print("Computing today's target allocation from the signal engine...")
    target_weights = compute_target_weights_today(close_df)

    print("Rebalancing paper portfolio...")
    state = rebalance(state, target_weights, prices, run_date)

    positions_out, metrics, equity, spy_equity = compute_metrics(state, prices, spy_price)
    state["equity_curve"].append({"date": run_date, "equity": round(equity, 2), "spy_equity": round(spy_equity, 2) if spy_equity else None})

    save_state(state)
    payload = export_dashboard(state, positions_out, metrics, run_date, target_weights)
    print_summary(metrics, run_date)
    print(f"Target weights today: {json.dumps({k: round(v,3) for k,v in target_weights.items()})}")
    print(f"Wrote {DASHBOARD_FILE} — import this into the dashboard to see it visually.")
    return payload


def status():
    if not os.path.exists(STATE_FILE):
        print("No paper portfolio yet. Run 'python phase3_paper_trader.py run' first.")
        return
    state = load_state()
    if not state.get("last_run"):
        print("Portfolio initialized but never run. Run 'python phase3_paper_trader.py run'.")
        return
    print("Fetching latest prices for a read-only status check...")
    close_df = fetch_price_history()
    prices = {t: float(close_df[t].dropna().iloc[-1]) for t in TICKERS}
    spy_price = float(close_df[BENCHMARK].dropna().iloc[-1])
    positions_out, metrics, equity, spy_equity = compute_metrics(state, prices, spy_price)
    print_summary(metrics, state["last_run"])


def reset():
    for f in (STATE_FILE, DASHBOARD_FILE):
        if os.path.exists(f):
            os.remove(f)
    print("Paper portfolio reset. Next 'run' will start fresh with $%.2f." % STARTING_CAPITAL)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Phase 3 paper trading engine")
    parser.add_argument("command", choices=["run", "status", "reset"], nargs="?", default="run")
    args = parser.parse_args()

    if args.command == "run":
        run()
    elif args.command == "status":
        status()
    elif args.command == "reset":
        reset()
