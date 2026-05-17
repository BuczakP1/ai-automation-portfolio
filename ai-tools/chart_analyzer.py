"""
Chart Analyzer v1
=================
Press F8 to screenshot your screen and get instant chart analysis from Claude.
Logs every analysis to CSV for tracking your paper trades.

Usage:
    py -3.12 chart_analyzer.py

Controls:
    F8  = Screenshot + Analyze
    F9  = Log result of last trade (win/loss/amount)
    ESC = Quit

Requirements:
    pip install anthropic pillow keyboard
    API key in D:\Desktop\api_key.txt
"""

import sys
import csv
import time
import base64
import keyboard
from io import BytesIO
from pathlib import Path
from datetime import datetime

# ─── SETTINGS ────────────────────────────────────────────────────────────────

API_KEY_PATH = r"D:\Desktop\api_key.txt"
OUTPUT_DIR = Path(r"D:\Desktop\Chart_Analysis")
SCREENSHOTS_DIR = OUTPUT_DIR / "screenshots"
CSV_FILE = OUTPUT_DIR / "trade_log.csv"

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

HOTKEY_ANALYZE = "f8"
HOTKEY_LOG_RESULT = "f9"
HOTKEY_QUIT = "esc"

# ─── ANALYSIS PROMPT ─────────────────────────────────────────────────────────

ANALYSIS_PROMPT = """You are a mechanical trading system. You ONLY trade when specific setups are present. You do NOT freestyle or guess. You scan for the 7 setups below and report which ones are active.

SCAN FOR THESE 7 SETUPS:

SETUP 1 — EMA CROSSOVER + RSI FILTER:
- Look at the moving averages on the chart (EMA 20, 50, 100, 200 if visible)
- BUY signal: Shorter EMA recently crossed ABOVE longer EMA AND RSI is above 50
- SELL signal: Shorter EMA recently crossed BELOW longer EMA AND RSI is below 50
- If no recent crossover, this setup is NOT ACTIVE

SETUP 2 — SUPPORT/RESISTANCE BREAK + RETEST:
- Identify clear horizontal support and resistance levels from recent price action
- BUY signal: Price broke above resistance, pulled back to retest it as new support, and is bouncing
- SELL signal: Price broke below support, pulled back to retest it as new resistance, and is rejecting
- If no break+retest pattern visible, this setup is NOT ACTIVE

SETUP 3 — RSI OVERSOLD/OVERBOUGHT REVERSAL:
- BUY signal: RSI dropped below 30 (oversold) AND price is above the longest visible moving average (uptrend)
- SELL signal: RSI rose above 70 (overbought) AND price is below the longest visible moving average (downtrend)
- If RSI is between 30-70, this setup is NOT ACTIVE

SETUP 4 — CONSECUTIVE CANDLES + TREND ALIGNMENT:
- BUY signal: 3 or more consecutive green candles AND price is above the 50 EMA (trend confirmation)
- SELL signal: 3 or more consecutive red candles AND price is below the 50 EMA (trend confirmation)
- The candles should be decent sized, not tiny dojis
- If no 3+ consecutive same-color candles, this setup is NOT ACTIVE

SETUP 5 — DOUBLE BOTTOM / DOUBLE TOP:
- BUY signal: Price hit approximately the same low twice and is now bouncing up from the second touch
- SELL signal: Price hit approximately the same high twice and is now dropping from the second touch
- The two touches should be separated by a visible rally/dip between them
- If no double bottom/top pattern, this setup is NOT ACTIVE

SETUP 6 — ENGULFING CANDLE AT KEY LEVEL:
- BUY signal: A large green candle completely engulfs the previous red candle, AND this happens at a support level or after a pullback
- SELL signal: A large red candle completely engulfs the previous green candle, AND this happens at a resistance level or after a rally
- If no engulfing pattern at a key level, this setup is NOT ACTIVE

SETUP 7 — EMA 200 TREND + PULLBACK:
- BUY signal: Price is generally above the 200 EMA (uptrend) and has pulled back to touch or nearly touch it, showing signs of bouncing
- SELL signal: Price is generally below the 200 EMA (downtrend) and has rallied up to touch or nearly touch it, showing signs of rejection
- If price is not near the 200 EMA or no 200 EMA visible, this setup is NOT ACTIVE

YOUR RESPONSE FORMAT (follow this exactly):

ASSET: [what you see on the chart]
PRICE: [current price if visible]

SETUP SCAN:
1. EMA Crossover: [ACTIVE BUY / ACTIVE SELL / NOT ACTIVE] — [brief reason]
2. S/R Break+Retest: [ACTIVE BUY / ACTIVE SELL / NOT ACTIVE] — [brief reason]
3. RSI Reversal: [ACTIVE BUY / ACTIVE SELL / NOT ACTIVE] — [brief reason]
4. Consecutive Candles: [ACTIVE BUY / ACTIVE SELL / NOT ACTIVE] — [brief reason]
5. Double Bottom/Top: [ACTIVE BUY / ACTIVE SELL / NOT ACTIVE] — [brief reason]
6. Engulfing Candle: [ACTIVE BUY / ACTIVE SELL / NOT ACTIVE] — [brief reason]
7. EMA 200 Pullback: [ACTIVE BUY / ACTIVE SELL / NOT ACTIVE] — [brief reason]

CONFIRMATION COUNT: [X out of 7 say BUY / Y out of 7 say SELL]

DECISION:
- If 3+ setups agree on BUY → ACTION: BUY
- If 3+ setups agree on SELL → ACTION: SELL
- If fewer than 3 agree or mixed signals → ACTION: WAIT
- If setups conflict (some say BUY, some say SELL) → ACTION: WAIT

ACTION: [BUY / SELL / WAIT]
CONFIDENCE: [Low / Medium / High — based on how many setups agree]
ENTRY: [price]
STOP LOSS: [price — below recent swing low for buys, above recent swing high for sells]
TAKE PROFIT: [price — minimum 1.5x the stop loss distance]
RISK/REWARD: [ratio]

RULES:
- NEVER force a trade. If the setups aren't there, say WAIT.
- Be accurate about what you see. If an indicator isn't visible on the chart, mark that setup as NOT ACTIVE.
- Only mark a setup as ACTIVE if it clearly matches the rules above. Close enough is NOT enough.
- This is for paper trading and education only."""

# ─── FUNCTIONS ───────────────────────────────────────────────────────────────

def load_api_key() -> str:
    key_path = Path(API_KEY_PATH)
    if not key_path.exists():
        print(f"\n  ERROR: API key not found at {API_KEY_PATH}")
        sys.exit(1)
    return key_path.read_text().strip()


def take_screenshot() -> tuple[bytes, Path]:
    """Take a screenshot and save it."""
    try:
        from PIL import ImageGrab
    except ImportError:
        print("\n  ERROR: pip install pillow")
        sys.exit(1)

    # Capture screen
    screenshot = ImageGrab.grab()

    # Save to file
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"chart_{timestamp}.png"
    filepath = SCREENSHOTS_DIR / filename
    screenshot.save(filepath, "PNG")

    # Convert to base64 for API
    buffer = BytesIO()
    screenshot.save(buffer, format="PNG")
    image_bytes = buffer.getvalue()

    print(f"  Screenshot saved: {filename}")
    return image_bytes, filepath


def analyze_chart(client, image_bytes: bytes) -> str:
    """Send screenshot to Claude for analysis."""

    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2000,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": image_b64,
                    },
                },
                {
                    "type": "text",
                    "text": ANALYSIS_PROMPT,
                },
            ],
        }],
    )

    return response.content[0].text


def init_csv():
    """Create CSV file with headers if it doesn't exist."""
    if not CSV_FILE.exists():
        with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "timestamp",
                "screenshot",
                "action",
                "confidence",
                "confirmations",
                "active_setups",
                "analysis",
                "result",
                "profit_loss",
                "notes",
            ])


def log_analysis(screenshot_path: Path, analysis: str):
    """Log the analysis to CSV."""

    action = "unknown"
    confidence = "unknown"
    confirmations = "0"
    active_setups = ""

    analysis_lower = analysis.lower()

    # Extract action - find the LAST occurrence of "ACTION:" which is the actual decision
    # (earlier ones are in the explanation text)
    action_lines = [line.strip() for line in analysis.split("\n") if line.strip().upper().startswith("ACTION:")]
    if action_lines:
        last_action = action_lines[-1].lower()
        if "wait" in last_action:
            action = "wait"
        elif "buy" in last_action:
            action = "buy"
        elif "sell" in last_action:
            action = "sell"

    # Extract confidence
    if "confidence: high" in analysis_lower:
        confidence = "high"
    elif "confidence: medium" in analysis_lower:
        confidence = "medium"
    elif "confidence: low" in analysis_lower:
        confidence = "low"

    # Count active setups
    buy_count = analysis_lower.count("active buy")
    sell_count = analysis_lower.count("active sell")
    confirmations = f"{buy_count}B/{sell_count}S"

    # Extract which setups are active
    setup_names = ["EMA Crossover", "S/R Break", "RSI Reversal", "Consecutive Candles", "Double Bottom/Top", "Engulfing", "EMA 200"]
    active = []
    for name in setup_names:
        if name.lower() in analysis_lower:
            # Check if it's active
            idx = analysis_lower.find(name.lower())
            nearby = analysis_lower[idx:idx+100]
            if "active buy" in nearby:
                active.append(f"{name}:BUY")
            elif "active sell" in nearby:
                active.append(f"{name}:SELL")
    active_setups = " | ".join(active) if active else "none"

    with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            screenshot_path.name,
            action,
            confidence,
            confirmations,
            active_setups,
            analysis.replace("\n", " | "),
            "",  # result - filled later with F9
            "",  # profit_loss - filled later
            "",  # notes - filled later
        ])

    return action, confidence, confirmations


def log_trade_result():
    """Update the last trade entry with result."""
    if not CSV_FILE.exists():
        print("  No trades logged yet.")
        return

    # Read all rows
    with open(CSV_FILE, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        rows = list(reader)

    if len(rows) < 2:
        print("  No trades logged yet.")
        return

    # Find last row without a result
    last_row_index = None
    for i in range(len(rows) - 1, 0, -1):
        if rows[i][7] == "":  # result column is now index 7
            last_row_index = i
            break

    if last_row_index is None:
        print("  All trades already have results logged.")
        return

    print(f"\n  Logging result for: {rows[last_row_index][0]}")
    print(f"  Signal was: {rows[last_row_index][2].upper()} | Confidence: {rows[last_row_index][3]} | Setups: {rows[last_row_index][4]}")
    print(f"  Active: {rows[last_row_index][5]}")

    # Get result
    print()
    result = input("  Result (win/loss/skip): ").strip().lower()
    if result not in ["win", "loss", "skip"]:
        print("  Invalid. Use: win, loss, or skip")
        return

    profit_loss = ""
    if result in ["win", "loss"]:
        profit_loss = input("  Amount (e.g. +15 or -10): ").strip()

    notes = input("  Notes (optional, press Enter to skip): ").strip()

    # Update row
    rows[last_row_index][7] = result
    rows[last_row_index][8] = profit_loss
    rows[last_row_index][9] = notes

    # Save
    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerows(rows)

    print(f"\n  Logged: {result} {profit_loss}")


def print_stats():
    """Print quick win/loss stats from the CSV."""
    if not CSV_FILE.exists():
        return

    with open(CSV_FILE, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        rows = list(reader)

    if len(rows) < 2:
        return

    total = 0
    wins = 0
    losses = 0
    skips = 0
    total_pnl = 0

    for row in rows[1:]:
        total += 1
        result = row[7].lower() if len(row) > 7 else ""
        pnl = row[8] if len(row) > 8 else ""

        if result == "win":
            wins += 1
        elif result == "loss":
            losses += 1
        elif result == "skip":
            skips += 1

        if pnl:
            try:
                total_pnl += float(pnl.replace("+", ""))
            except ValueError:
                pass

    decided = wins + losses
    win_rate = (wins / decided * 100) if decided > 0 else 0

    print(f"\n  ─── STATS ───")
    print(f"  Total analyses: {total}")
    print(f"  Wins: {wins} | Losses: {losses} | Skips: {skips} | Pending: {total - wins - losses - skips}")
    if decided > 0:
        print(f"  Win rate: {win_rate:.1f}%")
    print(f"  P&L: {total_pnl:+.2f}")
    print(f"  ──────────────")


# ─── MAIN ────────────────────────────────────────────────────────────────────

def main():
    print("\n" + "=" * 60)
    print("  CHART ANALYZER v1")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)
    print(f"\n  Screenshots: {SCREENSHOTS_DIR}")
    print(f"  Trade log:   {CSV_FILE}")
    print(f"\n  Controls:")
    print(f"    F8  = Screenshot + Analyze")
    print(f"    F9  = Log trade result (win/loss)")
    print(f"    ESC = Quit")

    # Load API key
    api_key = load_api_key()

    try:
        from anthropic import Anthropic
        client = Anthropic(api_key=api_key)
    except ImportError:
        print("\n  ERROR: pip install anthropic")
        sys.exit(1)

    # Init CSV
    init_csv()

    # Print existing stats
    print_stats()

    print(f"\n  Ready. Open your chart and press F8 to analyze.")
    print(f"  {'─' * 50}")

    # Main loop
    while True:
        event = keyboard.read_event(suppress=False)

        # Only trigger on key DOWN (not release)
        if event.event_type != keyboard.KEY_DOWN:
            continue

        if event.name == HOTKEY_QUIT:
            print("\n  Quitting...")
            print_stats()
            break

        elif event.name == HOTKEY_ANALYZE:
            print(f"\n  {'─' * 50}")
            print(f"  Analyzing... ({datetime.now().strftime('%H:%M:%S')})")

            try:
                # Screenshot
                image_bytes, screenshot_path = take_screenshot()

                # Analyze
                print("  Sending to Claude...")
                analysis = analyze_chart(client, image_bytes)

                # Log
                action, confidence, confirmations = log_analysis(screenshot_path, analysis)

                # Display
                print(f"\n{'=' * 60}")
                print(analysis)
                print(f"{'=' * 60}")
                print(f"\n  SIGNAL: {action.upper()} | Confidence: {confidence} | Setups: {confirmations}")
                print(f"  Logged to CSV. Press F9 to record the result later.")
                print(f"  Press F8 for another analysis.")

            except Exception as e:
                print(f"\n  ERROR: {e}")

        elif event.name == HOTKEY_LOG_RESULT:
            log_trade_result()
            print_stats()
            print(f"\n  Press F8 to analyze, ESC to quit.")


if __name__ == "__main__":
    main()
