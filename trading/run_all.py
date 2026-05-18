"""
run_all.py
==========
Launches all bots in sequence.
Waits for each bot to finish initialising before starting the next.
Auto-restarts any bot that crashes (up to MAX_RESTARTS times).

Signals from all bots are collected and printed at the bottom every 60s.

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

BASE = "/path/to/your/bots"  # update this to your folder

BOTS = [
    {
        "name":         "Crypto Signal Bot",
        "script":       f"{BASE}/crypto_signal_bot.py",
        "ready_marker": "Thread started: resolver",
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
    {
        "name":         "CEX Listing Bot",
        "script":       f"{BASE}/cex_listing_bot.py",
        "ready_marker": "CEX Listing Bot | PAPER MODE",
        "verbose":      True,
    },
    {
        "name":         "Coin Monitor",
        "script":       f"{BASE}/coin_monitor.py",
        "ready_marker": "Coin Monitor | 5m candles",
        "verbose":      True,
    },
    {
        "name":         "Meme Sniper",
        "script":       f"{BASE}/meme_sniper_bot.py",
        "ready_marker": "Meme Sniper Bot | PAPER MODE",
        "verbose":      True,
    },
    {
        "name":         "HIP-3 Funding Bot",
        "script":       f"{BASE}/hip3_funding_bot.py",
        "ready_marker": "HIP-3 Funding Rate Bot started",
        "verbose":      True,
    },
    {
        "name":         "Scanner Bot",
        "script":       f"{BASE}/scanner_bot.py",
        "ready_marker": "Scanner Bot | running every 24h",
        "verbose":      True,
    },
]

# Lines containing these keywords always print regardless of verbose setting
ALWAYS_SHOW = ["ERROR", "WARNING", "WARN", "CRASH", "SIGNAL", "RESOLVED", "BET", "WIN", "LOSS", "OPPORTUNITY"]

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
        print("  SIGNALS")
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
    print(f"run_all.py — Starting {len(BOTS)} bots")
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
        while True:
            time.sleep(60)

            with proc_lock:
                running = [(n, p) for n, p in processes if p.poll() is None]
            print(f"\n[STATUS] {len(running)}/{len(BOTS)} bots running: "
                  f"{', '.join(n for n, _ in running)}")

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
