# AI Automation Portfolio

103 production Python systems across algorithmic trading, lead generation, AI agents, blockchain, and media pipelines.

## What's here

### /trading
Live bots running 24/7 on real markets:
- `crypto_signal_bot.py` — multi-strategy crypto signal generator across Binance/Hyperliquid (5m/15m/1h)
- `liquidation_bot.py` — Binance futures liquidation monitor via WebSocket, trades momentum cascades
- `ai_filter_bot.py` — Claude API signal validation layer, filters low-confidence trades
- `meme_sniper_bot.py` — Solana meme coin scorer, identifies promising tokens in first 2 hours
- `cex_listing_bot.py` — monitors CEX listing announcements, flags tradeable assets
- `scanner_bot.py` — on-chain wallet scanner, discovers pre-pump wallets
- `hip3_funding_bot.py` — Hyperliquid perpetual funding rate scanner
- `coin_monitor.py` — 5m candle price tracker across 20+ assets
- `run_all.py` — bot orchestration system, auto-restarts on crash

### /lead-gen
Data pipelines producing 1.79M+ records:
- `Accountants.py` — Google Maps scraper (6 niches, 4 countries)
- `uk_business_scraper.py` — Companies House registry (69,521 enriched records)
- `uk_dissolution_scraper.py` — 1.79M dissolution records, updated daily
- `care_pipeline.py` — UK CQC + Irish HIQA poorly-rated facility tracker
- `podcast_enricher.py` — 39,605 podcasts scraped, 16,400 email-verified
- `Enrichment.py` — email extraction + DNS/SMTP validation pipeline
- `Lead tier separator.py` — contact quality tiering

### /ai-tools
Claude API integrations:
- `chart_analyzer.py` — F8 hotkey → Claude Vision chart analysis in seconds
- `code_reader.py` — reads entire codebase, generates portfolio report
- `transcript_cleaner.py` — Claude-powered transcript cleaning and formatting
- `linkedin_content_generator.py` — AI post generator from raw notes
- `pinterest_metadata.py` — SEO metadata generator for pin campaigns
- `build_knowledge_base.py` — auto-builds knowledge base from YouTube transcripts

### /media
- `twitch_clipper.py` — VOD highlight extractor using audio analysis + chat engagement scoring
- `transcriber.py` — Whisper transcription pipeline

## Tech stack

Python · Claude API (Anthropic) · CCXT · Hyperliquid SDK · Polymarket CLOB · pandas · asyncio · WebSockets · BeautifulSoup · Whisper · yfinance · XGBoost · Solana · Polygon · EIP-712

## Setup

```bash
pip install -r requirements.txt
cp config_example.py config.py
# Add your API keys to config.py
python run_all.py
```

## Status

Trading bots: Live (real money)
Lead gen pipelines: Production
AI tools: Active
