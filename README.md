
# SOL Anomaly Alerts → Telegram

Real‑time alerts to a Telegram chat when **Solana (SOL)** shows **abnormally high trading volume** or a **sharp price move** on Binance (spot) 1‑minute candles.  
WebSocket first; auto‑reconnect; REST fallback; cooldown to avoid spam.

---

## What you get
- Realtime from `wss://stream.binance.com:9443/ws/solusdt@kline_1m`
- Rolling z‑score on minute‑volume (configurable window & threshold)
- Price‑spike detection on rolling window (% change threshold)
- Telegram alerts via Bot API (HTML formatting)
- Cooldown per signal type
- Graceful WS reconnects with exponential backoff

---

## Quick start (Windows / PowerShell)

1) **Install Python 3.11+** (add to PATH).  
2) In PowerShell:
```powershell
cd /mnt/data/sol_anomaly_alert_bot
python -m venv .venv
. .venv/Scripts/Activate.ps1
pip install -r requirements.txt
```
3) Set your secrets in **.env** (see below).  
4) Run:
```powershell
python main.py
```

### .env template
Create a file named `.env` in the project root:
```
TELEGRAM_BOT_TOKEN=123456:AA...your...token
TELEGRAM_CHAT_ID=-1001234567890

# Detection settings
SYMBOL=SOLUSDT
KLINE_INTERVAL=1m

VOLUME_WINDOW=30          # minutes for rolling stats
VOLUME_ZSCORE=3.0         # z >= this → anomaly
MINUTE_VOLUME_MIN=0       # optional absolute volume floor (in SOL), 0 to disable

PRICE_WINDOW=10           # minutes to compare against baseline
PRICE_PCT_CHANGE=2.0      # % move in either direction triggers

ALERT_COOLDOWN_MIN=10     # mins per signal type (volume/price)
MERGE_SIGNALS_WINDOW=1    # mins: if both trigger in same minute → single merged alert
```

> **TELEGRAM_CHAT_ID** can be your user ID or a group/channel ID (bot must be a member; for channels, bot must be admin to post).

---

## How it works (overview)

- Subscribes to Binance 1m kline for `SOLUSDT`.
- Each closed candle updates:
  - Rolling **volume** deque → mean/stdev → z‑score
  - Rolling **price** deque → compares current close to `PRICE_WINDOW` minutes ago
- If thresholds are exceeded and cooldown allows → posts an alert.
- If both price & volume fire within the same minute → merged alert.

### Reconnects
- If WS drops, reconnect with exponential backoff (cap 60s).  
- Fallback: REST polling (`/api/v3/klines`) until WS returns.

---

## Troubleshooting

- If you see **401** from Telegram → token or chat id wrong / bot not in chat.
- If you get "SSL" or "timeout" → network filtering; it will auto‑retry.
- To change sensitivity:
  - Lower `VOLUME_ZSCORE` or raise `PRICE_PCT_CHANGE` (and vice‑versa).
  - Increase windows (`VOLUME_WINDOW`, `PRICE_WINDOW`) to make it calmer.

---

## Run as a background process (Windows)
Use another PowerShell window:
```powershell
Start-Process powershell -ArgumentList '-NoExit','-Command','cd /mnt/data/sol_anomaly_alert_bot; . .venv/Scripts/Activate.ps1; python main.py'
```
Stop with **Ctrl+C** in that window.

---

## Security
This script only sends messages via Telegram Bot API (outbound). No keys for exchanges are required.
