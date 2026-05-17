"""
run_all.py
==========
Launches bots in order: Crypto → Stocks → Weather.
Waits for each bot to finish initialising before starting the next.
Auto-restarts any bot that crashes (up to MAX_RESTARTS times).

Signals from all bots are collected and printed at the bottom every 60s.
Weather bot scanning noise is suppressed — only key lines shown.

RUN:
    python run_all.py

STOP:
    Ctrl+C — stops all bots cleanly
"""

import subprocess
import sys
import os
import time
import threading
from collections import deque

BASE = "D:/Desktop/Trading Folder"

BOTS = [
    {
        "name":         "Crypto Signal Bot",
        "script":       f"{BASE}/crypto_signal_bot.py",
        "ready_marker": "Thread started: resolver",
        "verbose":      True,   # show all output
    },
    # stocks_signal_bot.py — removed: redundant, SLTP covers all same strategies
    {
        "name":         "Stocks SLTP Bot",
        "script":       f"{BASE}/stocks_sltp_bot.py",
        "ready_marker": "1h loop started",
        "verbose":      True,
    },
    # stocks_fixed_bot.py   — removed: 42.5% WR, redundant (SLTP covers it)
    # stocks_combined_bot.py — removed: 43.0% WR, -9.64% PnL, redundant
    {
        "name":         "Stocks Volume Bot",
        "script":       f"{BASE}/stocks_volume_bot.py",
        "ready_marker": "1h loop started",
        "verbose":      True,
    },
    {
        "name":         "Liquidation Bot",
        "script":       f"{BASE}/liquidation_bot.py",
        "ready_marker": "Connected. Listening",
        "verbose":      True,
    },
    {
        "name":         "AI Filter Bot",
        "script":       f"{BASE}/ai_filter_bot.py",
        "ready_marker": "AI Filter Bot | PAPER MODE",
        "verbose":      True,
    },
    # auto_manager.py — disabled: 50.2% WR on 201 live trades, no edge
    {
        "name":         "SEC 8-K Bot",
        "script":       f"{BASE}/sec_8k_bot.py",
        "ready_marker": "SEC 8-K Bot | PAPER MODE",
        "verbose":      True,
    },
    {
        "name":         "SEC Signals Bot",
        "script":       f"{BASE}/sec_signals_bot.py",
        "ready_marker": "sec_signals_bot.py | 6 SEC EDGAR feeds",
        "verbose":      True,
    },
    {
        "name":         "Gov News Bot",
        "script":       f"{BASE}/gov_news_bot.py",
        "ready_marker": "Gov News Bot | PAPER MODE",
        "verbose":      True,
    },
    {
        "name":         "Insider Bot",
        "script":       f"{BASE}/insider_bot.py",
        "ready_marker": "Insider Trading Bot | PAPER MODE",
        "verbose":      True,
    },
    {
        "name":         "FDA Bot",
        "script":       f"{BASE}/fda_bot.py",
        "ready_marker": "FDA Approvals Bot starting",
        "verbose":      True,
    },
    {
        "name":         "CEX Listing Bot",
        "script":       f"{BASE}/cex_listing_bot.py",
        "ready_marker": "CEX Listing Bot | PAPER MODE",
        "verbose":      True,
    },
    # wallet_tracker.py — disabled: broken dollar amounts, no outcome resolution
    {
        "name":         "Coin Monitor",
        "script":       f"{BASE}/coin_monitor.py",
        "ready_marker": "Coin Monitor | 5m candles",
        "verbose":      True,
    },
    {
        "name":         "Earnings Bot",
        "script":       f"{BASE}/earnings_bot.py",
        "ready_marker": "Earnings Bot | PAPER MODE",
        "verbose":      True,
    },
    {
        "name":         "Short Squeeze Bot",
        "script":       f"{BASE}/short_squeeze_bot.py",
        "ready_marker": "Short Squeeze Bot | PAPER MODE",
        "verbose":      True,
    },
    {
        "name":         "Alpha Bot",
        "script":       f"{BASE}/alpha_bot.py",
        "ready_marker": "Alpha Bot | PAPER MODE",
        "verbose":      True,
    },
    {
        "name":         "Volume Scanner",
        "script":       f"{BASE}/volume_scanner_bot.py",
        "ready_marker": "Volume Scanner Bot | PAPER MODE",
        "verbose":      True,
    },
    {
        "name":         "Meme Sniper",
        "script":       f"{BASE}/meme_sniper_bot.py",
        "ready_marker": "Meme Sniper Bot | PAPER MODE",
        "verbose":      True,
    },
    # smart_wallet_bot.py  — disabled: no data file, not generating signals
    # hl_whale_copier.py   — disabled: 0 signals caught, $50k threshold too high
    {
        "name":         "HIP-3 Funding Bot",
        "script":       f"{BASE}/hip3_funding_bot.py",
        "ready_marker": "HIP-3 Funding Rate Bot started",
        "verbose":      True,
    },
    {
        "name":         "Crypto 5m Bot",
        "script":       f"{BASE}/crypto_signal_bot_5m.py",
        "ready_marker": "5m loop started",
        "verbose":      True,
    },
    {
        "name":         "Global Gov Bot",
        "script":       f"{BASE}/global_gov_bot.py",
        "ready_marker": "Global Gov Bot | PAPER MODE",
        "verbose":      True,
    },
    {
        "name":         "Scanner Bot",
        "script":       f"{BASE}/scanner_bot.py",
        "ready_marker": "Scanner Bot | running every 24h",
        "verbose":      True,
    },
    # experiment_15m_manager.py    — removed: paper only, no edge
    # experiment_15m_timefiltered.py — removed: 51.8% WR vs 64.9% for live bot
    # Poly 15m Live — disabled (47% WR, losing)
    # Poly 5m Live  — disabled (replaced by poly_best_bot)
    {
        "name":         "Poly Best Bot",
        "script":       f"{BASE}/poly_best_bot.py",
        "ready_marker": "poly_best_bot.py | LIVE",
        "verbose":      True,
    },
    {
        "name":         "Poly Best 15m Bot",
        "script":       f"{BASE}/poly_best_15m_bot.py",
        "ready_marker": "poly_best_15m_bot.py | PAPER",
        "verbose":      True,
    },
    {
        "name":         "Poly 1h Live",
        "script":       f"{BASE}/experiment_1h_timefiltered.py",
        "ready_marker": "Experiment 1h TimeFiltered | LIVE",
        "verbose":      True,
    },
    # experiment_wallet_copier.py — disabled: BUST $20 -> $0, 0/20 wins on May 11
    {
        "name":         "Slow Copier Bot",
        "script":       f"{BASE}/slow_copier_bot.py",
        "ready_marker": "slow_copier_bot.py | LIVE",
        "verbose":      True,
    },
    {
        "name":         "IBKR Paper Bot",
        "script":       f"{BASE}/ibkr_paper_bot.py",
        "ready_marker": "IBKR Paper Bot | 8-K BEARISH signals",
        "verbose":      True,
    },
    {
        "name":         "Stocks 1h Best Bot",
        "script":       f"{BASE}/stocks_1h_best_bot.py",
        "ready_marker": "STOCKS 1H BEST BOT | PAPER MODE",
        "verbose":      True,
    },
    {
        "name":         "Stocks 1h Flip Bot",
        "script":       f"{BASE}/stocks_1h_flip_bot.py",
        "ready_marker": "STOCKS 1H FLIP BOT | PAPER MODE",
        "verbose":      True,
    },
    {
        "name":         "ATR Scanner Bot",
        "script":       f"{BASE}/atr_scanner_bot.py",
        "ready_marker": "ATR BAND SCANNER BOT",
        "verbose":      True,
    },
    {
        "name":         "Scalp Scanner Bot",
        "script":       f"{BASE}/scalp_scanner_bot.py",
        "ready_marker": "SCALP SCANNER BOT",
        "verbose":      True,
    },
    {
        "name":         "Stock Trader Bot",
        "script":       f"{BASE}/stock_trader_bot.py",
        "ready_marker": "STOCK TRADER BOT",
        "verbose":      True,
    },
]

# focused_bot.py and live_bot.py removed — auto_manager handles all betting decisions directly

# Lines containing these keywords always print regardless of verbose setting
ALWAYS_SHOW = ["ERROR", "WARNING", "WARN", "CRASH", "SIGNAL", "RESOLVED", "BET", "WIN", "LOSS", "OPPORTUNITY", "Next scan"]

# Lines containing these keywords get added to the signals feed at the bottom
SIGNAL_KEYWORDS = ["SIGNAL", "RESOLVED", "WIN", "LOSS", "OPPORTUNITY", "BET"]

MAX_RESTARTS  = 10
RESTART_DELAY = 60
READY_TIMEOUT = 60

processes   = []
proc_lock   = threading.Lock()
signal_feed = deque(maxlen=50)   # last 50 signal lines across all bots
feed_lock   = threading.Lock()
print_lock  = threading.Lock()


# ─────────────────────────────────────────────
# PRINT HELPERS
# ─────────────────────────────────────────────

def bot_print(name: str, line: str):
    with print_lock:
        print(f"[{name}] {line}", end="", flush=True)


def should_show(line: str, verbose: bool) -> bool:
    if verbose:
        return True
    return any(k.lower() in line.lower() for k in ALWAYS_SHOW)


def is_signal_line(line: str) -> bool:
    return any(k.lower() in line.lower() for k in SIGNAL_KEYWORDS)


def print_signal_feed():
    with feed_lock:
        if not signal_feed:
            return
        lines = list(signal_feed)
    with print_lock:
        print()
        print("=" * 60)
        print("  SIGNALS — place bets on these")
        print("=" * 60)
        for line in lines:
            print(line, end="")
        print("=" * 60)
        print()


# ─────────────────────────────────────────────
# LAUNCH HELPERS
# ─────────────────────────────────────────────

def _start_process(bot: dict) -> subprocess.Popen | None:
    if not os.path.exists(bot["script"]):
        print(f"[SKIP] {bot['name']} — script not found: {bot['script']}")
        return None
    return subprocess.Popen(
        [sys.executable, bot["script"]],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )


def _make_reader(bot: dict, proc: subprocess.Popen):
    """Returns a reader function for this bot's stdout."""
    name    = bot["name"]
    verbose = bot["verbose"]

    def _reader():
        for line in iter(proc.stdout.readline, ""):
            if should_show(line, verbose):
                bot_print(name, line)
            if is_signal_line(line):
                with feed_lock:
                    signal_feed.append(f"[{name}] {line}")
    return _reader


def launch_and_wait(bot: dict) -> subprocess.Popen | None:
    proc = _start_process(bot)
    if proc is None:
        return None

    print(f"[STARTED] {bot['name']} (PID {proc.pid}) — waiting for ready signal...")

    ready_event = threading.Event()
    marker      = bot["ready_marker"]

    def _reader():
        for line in iter(proc.stdout.readline, ""):
            if should_show(line, bot["verbose"]):
                bot_print(bot["name"], line)
            if is_signal_line(line):
                with feed_lock:
                    signal_feed.append(f"[{bot['name']}] {line}")
            if not ready_event.is_set() and marker in line:
                ready_event.set()

    t = threading.Thread(target=_reader, daemon=True)
    t.start()

    fired = ready_event.wait(timeout=READY_TIMEOUT)
    if fired:
        print(f"[READY]   {bot['name']} initialised.")
    else:
        print(f"[READY?]  {bot['name']} — marker not seen in {READY_TIMEOUT}s, continuing anyway.")

    return proc


# ─────────────────────────────────────────────
# MONITOR / RESTART
# ─────────────────────────────────────────────

def monitor_and_restart(bot: dict, proc: subprocess.Popen):
    restart_count = 0

    while True:
        time.sleep(10)
        rc = proc.poll()

        if rc is None:
            continue

        if rc == 0:
            print(f"[STOPPED] {bot['name']} exited cleanly.")
            return

        restart_count += 1
        if restart_count > MAX_RESTARTS:
            print(f"[DEAD]    {bot['name']} crashed {MAX_RESTARTS} times — giving up.")
            return

        print(f"[CRASH]   {bot['name']} exited (code {rc}) — "
              f"restarting in {RESTART_DELAY}s (attempt {restart_count}/{MAX_RESTARTS})")
        time.sleep(RESTART_DELAY)

        proc = _start_process(bot)
        if proc is None:
            return

        t = threading.Thread(target=_make_reader(bot, proc), daemon=True)
        t.start()

        with proc_lock:
            for i, (name, _) in enumerate(processes):
                if name == bot["name"]:
                    processes[i] = (bot["name"], proc)
                    break


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    print("=" * 60)
    print("run_all.py — Starting bots: Crypto → Stocks → Weather → Live → SLTP → Fixed → Liquidation → Confluence → AI Signal")
    print("Signals will appear at the bottom every 60s")
    print("=" * 60)

    for bot in BOTS:
        proc = launch_and_wait(bot)
        if proc is None:
            continue

        with proc_lock:
            processes.append((bot["name"], proc))

        t = threading.Thread(target=monitor_and_restart, args=(bot, proc), daemon=True)
        t.start()

    if not processes:
        print("No bots started. Check script paths.")
        return

    print(f"\nAll {len(processes)} bot(s) running. Ctrl+C to stop all.\n")

    try:
        tick = 0
        while True:
            time.sleep(60)
            tick += 1

            with proc_lock:
                running = [(n, p) for n, p in processes if p.poll() is None]
            print(f"\n[STATUS] {len(running)}/{len(BOTS)} bots running: "
                  f"{', '.join(n for n, _ in running)}")

            # Print signal feed at the bottom every minute
            print_signal_feed()

    except KeyboardInterrupt:
        print("\n\nStopping all bots...")
        with proc_lock:
            for name, proc in processes:
                try:
                    proc.terminate()
                    print(f"  Stopped: {name}")
                except Exception:
                    pass
        print("Done.")


if __name__ == "__main__":
    main()
