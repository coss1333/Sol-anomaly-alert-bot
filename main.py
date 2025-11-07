
import asyncio
import json
import math
import os
import time
from collections import deque, defaultdict
from dataclasses import dataclass
from typing import Deque, Dict, Optional, Tuple

import aiohttp
from aiogram import Bot
from dotenv import load_dotenv

BINANCE_WS = "wss://stream.binance.com:9443/ws"
BINANCE_REST = "https://api.binance.com/api/v3/klines"  # symbol=SOLUSDT&interval=1m&limit=1
SYMBOL_DEFAULT = "SOLUSDT"  # Uppercase as per Binance

load_dotenv()

def env_str(key: str, default: str) -> str:
    v = os.getenv(key, default)
    return v

def env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except Exception:
        return default

def env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except Exception:
        return default

@dataclass
class Settings:
    telegram_token: str
    telegram_chat_id: str
    symbol: str
    kline_interval: str
    volume_window: int
    volume_zscore: float
    minute_volume_min: float
    price_window: int
    price_pct_change: float
    alert_cooldown_min: int
    merge_signals_window: int

    @classmethod
    def load(cls) -> "Settings":
        return cls(
            telegram_token=env_str("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=env_str("TELEGRAM_CHAT_ID", ""),
            symbol=env_str("SYMBOL", SYMBOL_DEFAULT).upper(),
            kline_interval=env_str("KLINE_INTERVAL", "1m"),
            volume_window=env_int("VOLUME_WINDOW", 30),
            volume_zscore=env_float("VOLUME_ZSCORE", 3.0),
            minute_volume_min=env_float("MINUTE_VOLUME_MIN", 0.0),
            price_window=env_int("PRICE_WINDOW", 10),
            price_pct_change=env_float("PRICE_PCT_CHANGE", 2.0),
            alert_cooldown_min=env_int("ALERT_COOLDOWN_MIN", 10),
            merge_signals_window=env_int("MERGE_SIGNALS_WINDOW", 1),
        )

class AnomalyDetector:
    def __init__(self, settings: Settings):
        self.s = settings
        self.volumes: Deque[float] = deque(maxlen=self.s.volume_window)
        self.prices: Deque[float] = deque(maxlen=self.s.price_window + 1)  # store baseline + current
        self.last_alert_ts: Dict[str, float] = defaultdict(lambda: 0.0)  # keys: "volume" / "price" / "merged"
        self.last_minute_ts: Optional[int] = None  # unix minute for de-dup

    @staticmethod
    def _mean_std(vals: Deque[float]) -> Tuple[float, float]:
        n = len(vals)
        if n == 0:
            return 0.0, 0.0
        m = sum(vals) / n
        var = sum((x - m) ** 2 for x in vals) / n if n > 1 else 0.0
        return m, math.sqrt(var)

    def check(self, kline: dict) -> Dict[str, bool]:
        """
        Input: Binance kline dict with fields:
          t (open time ms), T (close time ms), o,h,l,c,v (strings), x (is final)
        Returns dict of signals { "volume": bool, "price": bool, "merged": bool }
        """
        # Only evaluate on closed klines
        if not kline.get("x", False):
            return {"volume": False, "price": False, "merged": False}

        close_time_ms = int(kline["T"])
        minute_bucket = close_time_ms // 60000
        # De‑dup per minute
        if self.last_minute_ts == minute_bucket:
            return {"volume": False, "price": False, "merged": False}
        self.last_minute_ts = minute_bucket

        close_price = float(kline["c"])
        volume_sol = float(kline["v"])  # volume in SOL for the minute

        # ----- Volume z-score -----
        vol_signal = False
        if self.s.minute_volume_min <= 0 or volume_sol >= self.s.minute_volume_min:
            mean_v, std_v = self._mean_std(self.volumes)
            z = (volume_sol - mean_v) / std_v if std_v > 0 else 0.0
            vol_signal = (len(self.volumes) >= max(5, int(self.s.volume_window * 0.3))) and (z >= self.s.volume_zscore)
        # Update after computing z so we don't leak the current minute
        self.volumes.append(volume_sol)

        # ----- Price pct change vs baseline window -----
        price_signal = False
        baseline = self.prices[0] if len(self.prices) > 0 else close_price
        if len(self.prices) >= self.s.price_window:
            pct = (close_price - baseline) / baseline * 100.0 if baseline != 0 else 0.0
            price_signal = abs(pct) >= self.s.price_pct_change
        self.prices.append(close_price)

        # ----- Cooldowns -----
        now = time.time()
        cooldown = self.s.alert_cooldown_min * 60
        if vol_signal and now - self.last_alert_ts["volume"] < cooldown:
            vol_signal = False
        if price_signal and now - self.last_alert_ts["price"] < cooldown:
            price_signal = False

        merged = False
        if vol_signal and price_signal:
            # Merge if both within configured window (same minute in this implementation)
            merged = True
            vol_signal = False
            price_signal = False

        # Update last_alert_ts only for triggers we will actually send
        if merged:
            self.last_alert_ts["merged"] = now
        elif vol_signal:
            self.last_alert_ts["volume"] = now
        elif price_signal:
            self.last_alert_ts["price"] = now

        return {"volume": vol_signal, "price": price_signal, "merged": merged}

def format_alert_html(s: Settings, k: dict, signals: Dict[str, bool]) -> str:
    close_price = float(k["c"])
    volume_sol = float(k["v"])
    high = float(k["h"]); low = float(k["l"])
    t_close = int(k["T"]) // 1000
    when = f"<code>{time.strftime('%Y-%m-%d %H:%M', time.gmtime(t_close))} UTC</code>"

    parts = []
    if signals.get("merged"):
        parts.append("🚨 <b>SOL anomaly</b>: <b>VOLUME + PRICE</b>")
    elif signals.get("volume"):
        parts.append("📈 <b>SOL Volume Spike</b>")
    elif signals.get("price"):
        parts.append("⚡ <b>SOL Price Spike</b>")

    parts.append(f"Symbol: <b>{s.symbol}</b>  |  Interval: <b>{s.kline_interval}</b>")
    parts.append(f"Close: <b>{close_price:.4f}</b>  High/Low: <b>{high:.4f}</b>/<b>{low:.4f}</b>")
    parts.append(f"Minute Volume: <b>{volume_sol:.2f} SOL</b>")
    parts.append(f"Time: {when}")
    parts.append("Source: Binance 1m kline")
    return "\n".join(parts)

async def fetch_latest_rest(session: aiohttp.ClientSession, symbol: str, interval: str) -> Optional[dict]:
    params = {"symbol": symbol.upper(), "interval": interval, "limit": 1}
    try:
        async with session.get(BINANCE_REST, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data:
                    # REST kline array → wrap to WS-like keys
                    o = data[-1]
                    return {
                        "t": int(o[0]), "T": int(o[6]),
                        "o": str(o[1]), "h": str(o[2]), "l": str(o[3]), "c": str(o[4]),
                        "v": str(o[5]),
                        "x": True  # REST returns finished candles
                    }
    except Exception:
        return None
    return None

async def send_telegram(bot: Bot, chat_id: str, html: str):
    await bot.send_message(chat_id=chat_id, text=html, parse_mode="HTML", disable_web_page_preview=True)

async def kline_stream_url(symbol: str, interval: str) -> str:
    # binance requires lowercase stream name
    return f"{BINANCE_WS}/{symbol.lower()}@kline_{interval}"

async def ws_loop(settings: Settings):
    detector = AnomalyDetector(settings)
    bot = Bot(token=settings.telegram_token, parse_mode="HTML")

    backoff = 1.0
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                url = await kline_stream_url(settings.symbol, settings.kline_interval)
                async with aiohttp.ClientSession() as wssess:
                    async with wssess.ws_connect(url, heartbeat=25) as ws:
                        backoff = 1.0  # reset on successful connect
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                payload = json.loads(msg.data)
                                k = payload.get("k") or {}
                                # only evaluate when candle closed
                                if k.get("x", False):
                                    signals = detector.check(k)
                                    if any(signals.values()):
                                        html = format_alert_html(settings, k, signals)
                                        await send_telegram(bot, settings.telegram_chat_id, html)
                            elif msg.type == aiohttp.WSMsgType.ERROR:
                                break
                            elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
                                break
                # if we exit loop, trigger reconnect
            except Exception:
                # fallback: poll REST once to keep stats moving (optional)
                k = await fetch_latest_rest(session, settings.symbol, settings.kline_interval)
                if k:
                    signals = detector.check(k)
                    if any(signals.values()):
                        html = format_alert_html(settings, k, signals)
                        await send_telegram(bot, settings.telegram_chat_id, html)
            # backoff before reconnect
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

async def main():
    s = Settings.load()
    if not s.telegram_token or not s.telegram_chat_id:
        print("Please set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")
        return
    print(f"Starting SOL anomaly alerts for {s.symbol} @ {s.kline_interval}")
    await ws_loop(s)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Stopped by user")
