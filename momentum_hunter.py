# -*- coding: utf-8 -*-
"""
ATLAS MOMENTUM HUNTER — Yapışkan Mod Sistemi
=============================================
Amaç: SYN gibi coinleri %3-5'te yakala, %50-100'e kadar takip et.
Normal sinyal sisteminden BAĞIMSIZ çalışır.

Akış:
1. Her 60sn: MEXC + Gate.io + KuCoin + Huobi + Coinbase'den tüm coinleri çek
2. ch1h >= %2 + vol_chg >= %100 → RADAR
3. ch1h >= %3 + vol_chg >= %150 + RSI 55-80 → YAPIŞKAN MOD
4. Yapışkan modda her 10 dakikada push
5. %50+ geçince → EFSANE sinyali
6. Peak'ten %8 geri çekilince → çıkış
"""

import os
import time
import requests
import json
import statistics
from datetime import datetime, timezone, timedelta
from supabase import create_client
import firebase_admin
from firebase_admin import credentials, messaging

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
FIREBASE_SERVICE_ACCOUNT = os.environ.get("FIREBASE_SERVICE_ACCOUNT")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

try:
    if FIREBASE_SERVICE_ACCOUNT and not firebase_admin._apps:
        cred = credentials.Certificate(json.loads(FIREBASE_SERVICE_ACCOUNT))
        firebase_admin.initialize_app(cred)
        print("✅ Firebase başlatıldı")
except Exception as e:
    print(f"⚠️ Firebase: {e}")

# ── YAPIŞ KAN MOD PARAMETRELERİ ──────────────────────────────────
STICKY_ENTRY_CH1H    = 3.0    # %3+ 1 saatlik değişim
STICKY_ENTRY_VOL     = 150    # %150+ hacim artışı
STICKY_ENTRY_RSI_MIN = 50     # RSI alt sınır
STICKY_ENTRY_RSI_MAX = 82     # RSI üst sınır (aşırı alım sınırı)
STICKY_PUSH_INTERVAL = 600    # 10 dakikada bir push (saniye)
STICKY_EXIT_DRAWDOWN = 8.0    # Peak'ten %8 geri çekilince çıkış
STICKY_LEGENDARY     = 50.0   # %50 geçince EFSANE push
STICKY_MAX_HOLD_H    = 48     # Maksimum 48 saat tut

# ── RADAR PARAMETRELERİ ───────────────────────────────────────────
RADAR_CH1H  = 2.0   # %2+ hareket
RADAR_VOL   = 100   # %100+ hacim

# Global yapışkan coin listesi
# { symbol: { entry_price, peak_price, entry_time, last_push, push_count,
#             legendary_sent, ch1h_entry, vol_entry, rsi_entry } }
sticky_coins = {}

HEADERS = {'User-Agent': 'Mozilla/5.0'}


# ── VERİ ÇEKME ───────────────────────────────────────────────────

def get_mexc_tickers():
    try:
        r = requests.get("https://api.mexc.com/api/v3/ticker/24hr", timeout=10, headers=HEADERS)
        data = r.json()
        result = {}
        for t in data:
            sym = t.get("symbol", "")
            if not sym.endswith("USDT"):
                continue
            base = sym[:-4]
            try:
                result[base] = {
                    "price": float(t.get("lastPrice", 0)),
                    "vol_chg": float(t.get("quoteVolume", 0)),
                    "ch24h": float(t.get("priceChangePercent", 0)),
                    "high": float(t.get("highPrice", 0)),
                    "low": float(t.get("lowPrice", 0)),
                }
            except:
                continue
        return result
    except Exception as e:
        print(f"⚠️ MEXC: {e}")
        return {}


def get_kucoin_tickers():
    try:
        r = requests.get("https://api.kucoin.com/api/v1/market/allTickers", timeout=10, headers=HEADERS)
        data = r.json().get("data", {}).get("ticker", [])
        result = {}
        for t in data:
            sym = t.get("symbol", "")
            if not sym.endswith("-USDT"):
                continue
            base = sym[:-5]
            try:
                result[base] = {
                    "price": float(t.get("last", 0) or 0),
                    "ch24h": float(t.get("changeRate", 0) or 0) * 100,
                    "vol": float(t.get("volValue", 0) or 0),
                }
            except:
                continue
        return result
    except Exception as e:
        print(f"⚠️ KuCoin: {e}")
        return {}


def get_rsi(symbol, period=14):
    """MEXC'den 1 saatlik kline çekip RSI hesapla."""
    try:
        r = requests.get(
            f"https://api.mexc.com/api/v3/klines",
            params={"symbol": f"{symbol}USDT", "interval": "1h", "limit": period + 2},
            timeout=8, headers=HEADERS
        )
        klines = r.json()
        if not klines or len(klines) < period:
            return None
        closes = [float(k[4]) for k in klines]
        gains, losses = [], []
        for i in range(1, len(closes)):
            diff = closes[i] - closes[i-1]
            gains.append(max(diff, 0))
            losses.append(max(-diff, 0))
        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period
        if avg_loss == 0:
            return 100
        rs = avg_gain / avg_loss
        return round(100 - (100 / (1 + rs)), 1)
    except:
        return None


def get_ch1h(symbol):
    """Son 1 saatlik değişimi hesapla."""
    try:
        r = requests.get(
            f"https://api.mexc.com/api/v3/klines",
            params={"symbol": f"{symbol}USDT", "interval": "1h", "limit": 2},
            timeout=8, headers=HEADERS
        )
        klines = r.json()
        if not klines or len(klines) < 2:
            return None
        prev_close = float(klines[-2][4])
        curr_close = float(klines[-1][4])
        if prev_close == 0:
            return None
        return round(((curr_close - prev_close) / prev_close) * 100, 2)
    except:
        return None


def get_vol_change(symbol):
    """Son 1 saat hacmi vs önceki 1 saat hacmi."""
    try:
        r = requests.get(
            f"https://api.mexc.com/api/v3/klines",
            params={"symbol": f"{symbol}USDT", "interval": "1h", "limit": 6},
            timeout=8, headers=HEADERS
        )
        klines = r.json()
        if not klines or len(klines) < 3:
            return None
        curr_vol = float(klines[-1][5])
        prev_vols = [float(k[5]) for k in klines[-4:-1]]
        avg_prev = sum(prev_vols) / len(prev_vols) if prev_vols else 0
        if avg_prev == 0:
            return None
        return round(((curr_vol - avg_prev) / avg_prev) * 100, 1)
    except:
        return None


def get_current_price(symbol):
    try:
        r = requests.get(
            f"https://api.mexc.com/api/v3/ticker/price",
            params={"symbol": f"{symbol}USDT"},
            timeout=5, headers=HEADERS
        )
        return float(r.json().get("price", 0))
    except:
        return None


# ── PUSH ──────────────────────────────────────────────────────────

def send_push(title, body, symbol=None, extra=None):
    try:
        profiles = supabase.table("profiles").select("fcm_token").not_.is_("fcm_token", "null").execute()
        if not profiles.data:
            return
        tokens = [p["fcm_token"] for p in profiles.data if p.get("fcm_token")]
        for token in tokens:
            try:
                data_payload = {"route": "signals", "click_action": "FLUTTER_NOTIFICATION_CLICK"}
                if symbol:
                    data_payload["symbol"] = symbol
                    data_payload["market"] = "CRYPTO"
                if extra:
                    data_payload.update({k: str(v) for k, v in extra.items()})

                message = messaging.Message(
                    notification=messaging.Notification(title=title, body=body),
                    data=data_payload,
                    android=messaging.AndroidConfig(
                        priority="high",
                        notification=messaging.AndroidNotification(channel_id="atlas_momentum"),
                    ),
                    apns=messaging.APNSConfig(
                        payload=messaging.APNSPayload(aps=messaging.Aps(sound="default", badge=1))
                    ),
                    token=token,
                )
                messaging.send(message)
            except Exception as e:
                print(f"⚠️ Push token hatası: {e}")
        print(f"📱 Push gönderildi: {len(tokens)} kullanıcı — {title}")
    except Exception as e:
        print(f"❌ Push hatası: {e}")


def log_momentum_signal(symbol, price, ch1h, vol_chg, rsi, mode, detail=""):
    """Momentum sinyalini Supabase'e kaydet."""
    try:
        supabase.table("crypto_signals").insert({
            "symbol": symbol,
            "signal_type": f"momentum_{mode}",
            "price": price,
            "description": f"🎯 MOMENTUM {mode.upper()} | {symbol} | ${price} | ch1h:{ch1h:.1f}% | vol:{vol_chg:.0f}% | RSI:{rsi} | {detail}",
            "conviction": "CRITICAL",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as e:
        print(f"⚠️ Log hatası: {e}")


# ── YAPIŞKAN MOD ─────────────────────────────────────────────────

def enter_sticky(symbol, price, ch1h, vol_chg, rsi):
    """Yapiçkan moda gir."""
    now = time.time()
    sticky_coins[symbol] = {
        "entry_price": price,
        "peak_price": price,
        "entry_time": now,
        "last_push": now,
        "push_count": 0,
        "legendary_sent": False,
        "ch1h_entry": ch1h,
        "vol_entry": vol_chg,
        "rsi_entry": rsi,
    }

    send_push(
        title=f"🎯 YAPIŞKAN MOD: {symbol}",
        body=f"${price:.4f} | +{ch1h:.1f}% son 1s | Hacim +{vol_chg:.0f}% | RSI:{rsi} | Takip başladı",
        symbol=symbol,
        extra={"mode": "sticky_entry"}
    )
    log_momentum_signal(symbol, price, ch1h, vol_chg, rsi, "entry", "Yapışkan mod başladı")
    print(f"\n🎯 YAPIŞKAN MOD BAŞLADI: {symbol} @ ${price:.4f}")
    print(f"   ch1h:{ch1h:.1f}% | vol:{vol_chg:.0f}% | RSI:{rsi}")


def update_sticky(symbol):
    """Yapışkan mod takibi — her döngüde çağrılır."""
    coin = sticky_coins.get(symbol)
    if not coin:
        return

    now = time.time()
    price = get_current_price(symbol)
    if not price or price == 0:
        return

    entry_price = coin["entry_price"]
    peak_price = coin["peak_price"]
    gain_from_entry = ((price - entry_price) / entry_price) * 100
    gain_from_peak = ((price - peak_price) / peak_price) * 100
    hold_hours = (now - coin["entry_time"]) / 3600

    # Peak güncelle
    if price > peak_price:
        sticky_coins[symbol]["peak_price"] = price
        peak_price = price

    peak_gain = ((peak_price - entry_price) / entry_price) * 100
    drawdown = ((peak_price - price) / peak_price) * 100 if peak_price > 0 else 0

    # ── ÇIKIŞ KOŞULLARI ──────────────────────────────────────────
    # 1. Peak'ten %8 geri çekildi
    if drawdown >= STICKY_EXIT_DRAWDOWN and peak_gain > 5:
        send_push(
            title=f"🔴 {symbol} — ÇIKIŞ SİNYALİ",
            body=f"Peak ${peak_price:.4f}'den %{drawdown:.1f} geri çekildi | Giriş: ${entry_price:.4f} | K/Z: {gain_from_entry:+.1f}%",
            symbol=symbol,
            extra={"mode": "sticky_exit"}
        )
        log_momentum_signal(symbol, price, 0, 0, 0, "exit",
                           f"Peak'ten %{drawdown:.1f} geri çekildi | K/Z:{gain_from_entry:+.1f}%")
        print(f"🔴 {symbol} ÇIKIŞ: Peak ${peak_price:.4f} → ${price:.4f} (-{drawdown:.1f}%) | K/Z:{gain_from_entry:+.1f}%")
        del sticky_coins[symbol]
        return

    # 2. 48 saat geçti
    if hold_hours >= STICKY_MAX_HOLD_H:
        send_push(
            title=f"⏰ {symbol} — 48s doldu",
            body=f"K/Z: {gain_from_entry:+.1f}% | Peak: +{peak_gain:.1f}%",
            symbol=symbol
        )
        del sticky_coins[symbol]
        return

    # ── MİLESTONE PUSHLAR ───────────────────────────────────────
    milestones = [10, 25, 50, 100]
    for ms in milestones:
        ms_key = f"milestone_{ms}"
        if gain_from_entry >= ms and not coin.get(ms_key):
            if ms >= 50:
                emoji = "🔥🔥"
                msg = f"Hâlâ tutuyoruz! Stop koruyor — zirvede çık"
            elif ms >= 25:
                emoji = "🔥"
                msg = f"Güçlü gidiş — trailing stop aktif"
            else:
                emoji = "📈"
                msg = f"Devam ediyor — stop yukarı çekildi"
            send_push(
                title=f"{emoji} {symbol} +%{ms} geçti!",
                body=f"${price:.4f} | Peak: +{peak_gain:.1f}% | {msg}",
                symbol=symbol,
                extra={"mode": f"milestone_{ms}"}
            )
            sticky_coins[symbol][ms_key] = True
            print(f"{emoji} {symbol} milestone +%{ms}: {gain_from_entry:.1f}%")

    # ── PERIYODIK PUSH (10 dakikada bir) ────────────────────────
    if now - coin["last_push"] >= STICKY_PUSH_INTERVAL:
        push_count = coin["push_count"] + 1

        if gain_from_entry >= 30:
            emoji = "🔥🔥"
            durum = f"MUHTEŞEM yürüyüş"
        elif gain_from_entry >= 15:
            emoji = "🔥"
            durum = "Güçlü yürüyüş"
        elif gain_from_entry >= 5:
            emoji = "📈"
            durum = "Devam ediyor"
        elif gain_from_entry > 0:
            emoji = "⏳"
            durum = "Bekleniyor"
        else:
            emoji = "⚠️"
            durum = "Dikkat"

        send_push(
            title=f"{emoji} {symbol} {gain_from_entry:+.1f}% — #{push_count}",
            body=f"${price:.4f} | Peak: +{peak_gain:.1f}% | {durum} | {hold_hours:.1f}s tutuldu",
            symbol=symbol,
            extra={"mode": "sticky_update", "push_count": str(push_count)}
        )
        sticky_coins[symbol]["last_push"] = now
        sticky_coins[symbol]["push_count"] = push_count
        print(f"📱 {symbol} güncelleme #{push_count}: {gain_from_entry:+.1f}% (peak:{peak_gain:+.1f}%)")


# ── ANA TARAMA ────────────────────────────────────────────────────

def scan_for_momentum():
    """Tüm borsaları tara, momentum coinleri bul."""
    print(f"\n🔍 Momentum taraması... {datetime.now(timezone.utc).strftime('%H:%M UTC')}")

    mexc = get_mexc_tickers()
    kucoin = get_kucoin_tickers()

    # Ortak coinler — her iki borsada da var (güvenilirlik)
    # Ama tek borsada da olsa güçlü sinyal varsa dahil et
    all_symbols = set(mexc.keys())

    radar_hits = []

    for symbol in all_symbols:
        # Daha önce yapışkan modda mı?
        if symbol in sticky_coins:
            continue

        # Leveraged token filtresi
        LEV = ("3S","5S","3L","5L","2S","2L","10S","10L","UP","DOWN","BEAR","BULL")
        if any(symbol.endswith(s) for s in LEV):
            continue

        # Fiyat filtresi
        price = mexc.get(symbol, {}).get("price", 0)
        if not price or price < 0.000001:
            continue

        # 24s değişim — çok yüksekse zaten geç kalmış olabiliriz
        ch24h = mexc.get(symbol, {}).get("ch24h", 0)
        if ch24h > 200:  # %200+ zaten pompalandı
            continue

        # 1 saatlik değişim
        ch1h = get_ch1h(symbol)
        if ch1h is None or ch1h < RADAR_CH1H:
            continue

        # Hacim değişimi
        vol_chg = get_vol_change(symbol)
        if vol_chg is None or vol_chg < RADAR_VOL:
            continue

        radar_hits.append({
            "symbol": symbol,
            "price": price,
            "ch1h": ch1h,
            "vol_chg": vol_chg,
            "ch24h": ch24h,
        })

        time.sleep(0.1)

    print(f"  📡 Radar: {len(radar_hits)} aday")

    # Radar adayları için RSI çek ve yapışkan mod kontrolü
    for hit in sorted(radar_hits, key=lambda x: x["ch1h"], reverse=True)[:20]:
        symbol = hit["symbol"]
        ch1h = hit["ch1h"]
        vol_chg = hit["vol_chg"]
        price = hit["price"]

        # Yapışkan mod eşiği
        if ch1h >= STICKY_ENTRY_CH1H and vol_chg >= STICKY_ENTRY_VOL:
            rsi = get_rsi(symbol)
            if rsi is None:
                rsi = 60  # Default — bilinmiyorsa orta kabul et

            print(f"  🎯 {symbol}: ch1h:{ch1h:.1f}% vol:{vol_chg:.0f}% RSI:{rsi}")

            if STICKY_ENTRY_RSI_MIN <= rsi <= STICKY_ENTRY_RSI_MAX:
                enter_sticky(symbol, price, ch1h, vol_chg, rsi)
            elif rsi > STICKY_ENTRY_RSI_MAX:
                # RSI çok yüksek ama hacim inanılmaz güçlüyse yine de al
                if vol_chg >= 300:
                    print(f"  ⚡ {symbol}: RSI yüksek ({rsi}) ama hacim çok güçlü ({vol_chg:.0f}%) — yapışkan!")
                    enter_sticky(symbol, price, ch1h, vol_chg, rsi)
                else:
                    print(f"  ⏭️ {symbol}: RSI {rsi} çok yüksek, hacim yetersiz — atlandı")
        else:
            print(f"  👁️ Radar: {symbol} ch1h:{ch1h:.1f}% vol:{vol_chg:.0f}%")

        time.sleep(0.3)


def main():
    print("=" * 60)
    print("🎯 ATLAS MOMENTUM HUNTER — Yapışkan Mod Sistemi")
    print(f"Eşikler: ch1h>={STICKY_ENTRY_CH1H}% | vol>={STICKY_ENTRY_VOL}% | RSI {STICKY_ENTRY_RSI_MIN}-{STICKY_ENTRY_RSI_MAX}")
    print(f"Push: her {STICKY_PUSH_INTERVAL//60} dakika | Çıkış: peak'ten -%{STICKY_EXIT_DRAWDOWN}")
    print("=" * 60)

    scan_cycle = 0

    while True:
        try:
            # Yapışkan coinleri güncelle — her döngüde
            if sticky_coins:
                print(f"\n📌 {len(sticky_coins)} yapışkan coin takip ediliyor: {list(sticky_coins.keys())}")
                for symbol in list(sticky_coins.keys()):
                    update_sticky(symbol)
                    time.sleep(0.5)

            # Her 60 saniyede yeni momentum tara
            scan_cycle += 1
            if scan_cycle % 1 == 0:  # Her döngüde tara
                scan_for_momentum()

            time.sleep(60)

        except Exception as e:
            print(f"❌ Ana döngü hatası: {e}")
            import traceback
            traceback.print_exc()
            time.sleep(30)


if __name__ == "__main__":
    main()
