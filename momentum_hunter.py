# -*- coding: utf-8 -*-
"""
ATLAS MOMENTUM HUNTER V2 — Çoklu Borsa Konfirmasyon Sistemi
=============================================================
Amaç: SYN gibi coinleri %3-5'te yakala, %50-100'e kadar yapışkan takip.

SINYAL KRİTERLERİ (3'ü birden = ALTIN SİNYAL):
1. MEXC + Gate.io'da aynı anda yükseliş (çift borsa konfirmasyon)
2. Son 1s hacim > önceki 3s ortalamasının 2x+ (hacim ivmesi)
3. Son 4s en yüksek fiyatı kırdı (fiyat kırılması)

+ RSI 50-82 (momentum bölgesi, tükenme değil)
+ ch1h >= %3 (yeterli hareket)
"""

import os
import time
import requests
import json
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
        print("✅ Firebase başlatıldı (Momentum Hunter)")
except Exception as e:
    print(f"⚠️ Firebase: {e}")

# ── PARAMETRELER ──────────────────────────────────────────────────
STICKY_ENTRY_CH1H    = 3.0    # %3+ 1 saatlik değişim
STICKY_ENTRY_VOL_MUL = 2.0    # Son 1s hacim > önceki 3s ort. x2
STICKY_RSI_MIN       = 50
STICKY_RSI_MAX       = 82
STICKY_PUSH_INTERVAL = 600    # 10 dakikada bir push
STICKY_EXIT_DRAWDOWN = 8.0    # Peak'ten %8 → çıkış
STICKY_MAX_HOLD_H    = 48

HEADERS = {'User-Agent': 'Mozilla/5.0'}
LEV_SUFFIXES = ("3S","5S","3L","5L","2S","2L","10S","10L","UP","DOWN","BEAR","BULL")

sticky_coins = {}


# ── VERİ ÇEKME ───────────────────────────────────────────────────

def get_mexc_tickers():
    try:
        r = requests.get("https://api.mexc.com/api/v3/ticker/24hr", timeout=10, headers=HEADERS)
        result = {}
        for t in r.json():
            sym = t.get("symbol","")
            if not sym.endswith("USDT"): continue
            base = sym[:-4]
            if any(base.endswith(s) for s in LEV_SUFFIXES): continue
            try:
                result[base] = {
                    "price": float(t.get("lastPrice",0)),
                    "ch24h": float(t.get("priceChangePercent",0)),
                }
            except: continue
        return result
    except Exception as e:
        print(f"⚠️ MEXC ticker: {e}")
        return {}


def get_gate_tickers():
    try:
        r = requests.get("https://api.gateio.ws/api/v4/spot/tickers", timeout=10, headers=HEADERS)
        result = {}
        for t in r.json():
            sym = t.get("currency_pair","")
            if not sym.endswith("_USDT"): continue
            base = sym[:-5]
            if any(base.endswith(s) for s in LEV_SUFFIXES): continue
            try:
                result[base] = {
                    "price": float(t.get("last",0)),
                    "ch24h": float(t.get("change_percentage",0)),
                    "vol": float(t.get("quote_volume",0)),
                }
            except: continue
        return result
    except Exception as e:
        print(f"⚠️ Gate.io ticker: {e}")
        return {}


def get_klines(symbol, interval="1h", limit=6):
    """MEXC kline verisi — RSI, hacim ivmesi, fiyat kırılması için."""
    try:
        r = requests.get(
            "https://api.mexc.com/api/v3/klines",
            params={"symbol": f"{symbol}USDT", "interval": interval, "limit": limit},
            timeout=8, headers=HEADERS
        )
        return r.json()
    except:
        return []


def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period
    if al == 0: return 100
    return round(100 - (100 / (1 + ag/al)), 1)


def analyze_coin(symbol):
    """
    Coin için 3 kriter analizi:
    1. Hacim ivmesi: son 1s hacim > önceki 3s ort. x2
    2. Fiyat kırılması: son 4s en yükseği aştı
    3. RSI momentum bölgesinde (50-82)
    """
    klines = get_klines(symbol, interval="1h", limit=18)
    if not klines or len(klines) < 6:
        return None

    try:
        closes = [float(k[4]) for k in klines]
        volumes = [float(k[5]) for k in klines]
        highs = [float(k[2]) for k in klines]

        curr_vol = volumes[-1]
        prev_vols = volumes[-4:-1]
        avg_prev_vol = sum(prev_vols) / len(prev_vols) if prev_vols else 0

        if avg_prev_vol == 0:
            return None

        vol_multiplier = curr_vol / avg_prev_vol

        # Son 1 saatlik değişim
        ch1h = ((closes[-1] - closes[-2]) / closes[-2]) * 100 if closes[-2] > 0 else 0

        # Fiyat kırılması: son 4s yükseklerinin maksimumunu aştı mı?
        prev_high = max(highs[-5:-1])
        breakout = closes[-1] > prev_high

        # RSI
        rsi_closes = get_klines(symbol, interval="1h", limit=16)
        rsi_closes_prices = [float(k[4]) for k in rsi_closes] if rsi_closes else closes
        rsi = calc_rsi(rsi_closes_prices)

        return {
            "ch1h": round(ch1h, 2),
            "vol_multiplier": round(vol_multiplier, 2),
            "breakout": breakout,
            "rsi": rsi,
            "price": closes[-1],
            "prev_high": prev_high,
        }
    except:
        return None


def get_current_price(symbol):
    try:
        r = requests.get(
            "https://api.mexc.com/api/v3/ticker/price",
            params={"symbol": f"{symbol}USDT"},
            timeout=5, headers=HEADERS
        )
        return float(r.json().get("price", 0))
    except:
        return None


# ── PUSH ─────────────────────────────────────────────────────────

def send_push(title, body, symbol=None, extra=None):
    try:
        profiles = supabase.table("profiles").select("fcm_token").not_.is_("fcm_token","null").execute()
        if not profiles.data: return
        tokens = [p["fcm_token"] for p in profiles.data if p.get("fcm_token")]
        for token in tokens:
            try:
                data_payload = {"route": "signals", "click_action": "FLUTTER_NOTIFICATION_CLICK"}
                if symbol: data_payload["symbol"] = symbol
                if extra: data_payload.update({k: str(v) for k,v in extra.items()})
                msg = messaging.Message(
                    notification=messaging.Notification(title=title, body=body),
                    data=data_payload,
                    android=messaging.AndroidConfig(priority="high",
                        notification=messaging.AndroidNotification(channel_id="atlas_momentum")),
                    apns=messaging.APNSConfig(payload=messaging.APNSPayload(
                        aps=messaging.Aps(sound="default", badge=1))),
                    token=token,
                )
                messaging.send(msg)
            except: pass
        print(f"📱 Push: {title}")
    except Exception as e:
        print(f"❌ Push: {e}")


# ── YAPIŞKAN MOD ──────────────────────────────────────────────────

def enter_sticky(symbol, price, analysis, confirmed_by):
    now = time.time()
    ch1h = analysis["ch1h"]
    vol_mul = analysis["vol_multiplier"]
    rsi = analysis["rsi"]
    breakout = analysis["breakout"]

    sticky_coins[symbol] = {
        "entry_price": price,
        "peak_price": price,
        "entry_time": now,
        "last_push": now,
        "push_count": 0,
        "milestones": set(),
        "ch1h_entry": ch1h,
        "vol_entry": vol_mul,
        "rsi_entry": rsi,
        "confirmed_by": confirmed_by,
    }

    konfirm = " + ".join(confirmed_by)
    send_push(
        title=f"🎯 YAPIŞKAN MOD: {symbol}",
        body=f"${price:.5f} | +{ch1h:.1f}% (1s) | Hacim {vol_mul:.1f}x | RSI:{rsi} | {'📈 Kırılım!' if breakout else ''} | {konfirm}",
        symbol=symbol,
        extra={"mode": "sticky_entry"}
    )

    try:
        supabase.table("crypto_signals").insert({
            "symbol": symbol,
            "signal_type": "momentum_sticky",
            "price": price,
            "description": f"🎯 YAPIŞKAN | {symbol} | ${price:.5f} | ch1h:{ch1h:.1f}% | vol:{vol_mul:.1f}x | RSI:{rsi} | {konfirm}",
            "conviction": "CRITICAL",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
    except: pass

    print(f"\n🎯 YAPIŞKAN MOD: {symbol} @ ${price:.5f}")
    print(f"   ch1h:{ch1h:.1f}% | vol:{vol_mul:.1f}x | RSI:{rsi} | kırılım:{breakout}")
    print(f"   Konfirmasyon: {konfirm}")


def update_sticky(symbol):
    coin = sticky_coins.get(symbol)
    if not coin: return

    now = time.time()
    price = get_current_price(symbol)
    if not price or price == 0: return

    entry_price = coin["entry_price"]
    peak_price = coin["peak_price"]
    gain = ((price - entry_price) / entry_price) * 100
    hold_hours = (now - coin["entry_time"]) / 3600

    if price > peak_price:
        sticky_coins[symbol]["peak_price"] = price
        peak_price = price

    peak_gain = ((peak_price - entry_price) / entry_price) * 100
    drawdown = ((peak_price - price) / peak_price) * 100 if peak_price > 0 else 0

    # ── ÇIKIŞ ────────────────────────────────────────────────────
    if drawdown >= STICKY_EXIT_DRAWDOWN and peak_gain > 3:
        send_push(
            title=f"🔴 {symbol} — ÇIK",
            body=f"Zirve ${peak_price:.5f} → ${price:.5f} (-{drawdown:.1f}%) | K/Z: {gain:+.1f}% | {hold_hours:.1f}s tutuldu",
            symbol=symbol,
            extra={"mode": "sticky_exit"}
        )
        print(f"🔴 {symbol} ÇIKIŞ: -{drawdown:.1f}% zirveden | K/Z:{gain:+.1f}%")
        del sticky_coins[symbol]
        return

    if hold_hours >= STICKY_MAX_HOLD_H:
        send_push(title=f"⏰ {symbol} — 48s doldu", body=f"K/Z: {gain:+.1f}% | Peak: +{peak_gain:.1f}%", symbol=symbol)
        del sticky_coins[symbol]
        return

    # ── MİLESTONE'LAR ────────────────────────────────────────────
    for ms, emoji, msg in [
        (10, "📈", "Devam ediyor — stop yukarı çekildi"),
        (25, "🔥", "Güçlü gidiş — trailing stop aktif"),
        (50, "🔥🔥", "Hâlâ tutuyoruz! Stop koruyor"),
        (100, "🚀🚀", "2x yaptı — stop koruyor, devam"),
    ]:
        if gain >= ms and ms not in coin["milestones"]:
            send_push(
                title=f"{emoji} {symbol} +%{ms} geçti!",
                body=f"${price:.5f} | Peak: +{peak_gain:.1f}% | {msg}",
                symbol=symbol,
                extra={"mode": f"milestone_{ms}"}
            )
            sticky_coins[symbol]["milestones"].add(ms)
            print(f"{emoji} {symbol} milestone +%{ms}")

    # ── PERİYODİK PUSH ───────────────────────────────────────────
    if now - coin["last_push"] >= STICKY_PUSH_INTERVAL:
        push_count = coin["push_count"] + 1
        emoji = "🔥🔥" if gain >= 30 else "🔥" if gain >= 15 else "📈" if gain >= 5 else "⏳"
        send_push(
            title=f"{emoji} {symbol} {gain:+.1f}% — #{push_count}",
            body=f"${price:.5f} | Peak: +{peak_gain:.1f}% | {hold_hours:.1f}s tutuldu",
            symbol=symbol,
            extra={"mode": "sticky_update"}
        )
        sticky_coins[symbol]["last_push"] = now
        sticky_coins[symbol]["push_count"] = push_count
        print(f"📱 {symbol} #{push_count}: {gain:+.1f}% (peak:{peak_gain:+.1f}%)")


# ── ANA TARAMA ────────────────────────────────────────────────────

def scan():
    print(f"\n🔍 Momentum taraması... {datetime.now(timezone.utc).strftime('%H:%M UTC')}")

    mexc = get_mexc_tickers()
    gate = get_gate_tickers()

    # Ortak coinler — hem MEXC hem Gate.io'da var
    common = set(mexc.keys()) & set(gate.keys())

    # MEXC'de olup Gate'de olmayan ama çok güçlü olan coinler de dahil
    mexc_only = set(mexc.keys()) - set(gate.keys())

    candidates = []

    # Ön filtre — hızlı
    for symbol in common:
        if any(symbol.endswith(s) for s in LEV_SUFFIXES): continue
        if symbol in sticky_coins: continue
        m = mexc.get(symbol, {})
        g = gate.get(symbol, {})
        mp = m.get("price", 0)
        gp = g.get("price", 0)
        if not mp or not gp: continue
        if mp < 0.000001: continue

        # Fiyat tutarlılığı — iki borsa arasında max %10 fark
        ratio = max(mp, gp) / min(mp, gp) if min(mp, gp) > 0 else 999
        if ratio > 1.10: continue

        # Her iki borsada da pozitif 24s değişim
        if m.get("ch24h", 0) < 3 and g.get("ch24h", 0) < 3: continue

        candidates.append({"symbol": symbol, "price": mp, "dual_confirmed": True})

    # MEXC only — çok güçlü hacimli olanlar
    for symbol in list(mexc_only)[:200]:
        if any(symbol.endswith(s) for s in LEV_SUFFIXES): continue
        if symbol in sticky_coins: continue
        m = mexc.get(symbol, {})
        if m.get("ch24h", 0) < 5: continue  # Tek borsada daha sıkı filtre
        mp = m.get("price", 0)
        if not mp or mp < 0.000001: continue
        candidates.append({"symbol": symbol, "price": mp, "dual_confirmed": False})

    print(f"  📡 {len(candidates)} aday ({sum(1 for c in candidates if c['dual_confirmed'])} çift borsa)")

    # Detaylı analiz — en güçlü adaylar
    hits = 0
    for c in candidates:
        symbol = c["symbol"]
        price = c["price"]
        dual = c["dual_confirmed"]

        analysis = analyze_coin(symbol)
        if not analysis:
            time.sleep(0.1)
            continue

        ch1h = analysis["ch1h"]
        vol_mul = analysis["vol_multiplier"]
        rsi = analysis.get("rsi") or 60
        breakout = analysis["breakout"]

        # Çift borsa: daha düşük eşik
        # Tek borsa: daha yüksek eşik
        vol_threshold = STICKY_ENTRY_VOL_MUL if dual else STICKY_ENTRY_VOL_MUL * 1.5
        ch1h_threshold = STICKY_ENTRY_CH1H if dual else STICKY_ENTRY_CH1H + 1.0

        if ch1h < ch1h_threshold: 
            time.sleep(0.05)
            continue
        if vol_mul < vol_threshold:
            time.sleep(0.05)
            continue
        if not (STICKY_RSI_MIN <= rsi <= STICKY_RSI_MAX):
            # RSI çok yüksek ama hacim çok güçlüyse yine de al
            if not (rsi > STICKY_RSI_MAX and vol_mul >= 4.0):
                time.sleep(0.05)
                continue

        # Konfirmasyon listesi
        confirmed_by = []
        if dual: confirmed_by.append("MEXC+Gate.io")
        if vol_mul >= vol_threshold: confirmed_by.append(f"Hacim {vol_mul:.1f}x")
        if breakout: confirmed_by.append("Fiyat kırılımı")
        if rsi and STICKY_RSI_MIN <= rsi <= 70: confirmed_by.append(f"RSI {rsi} ideal")

        # Kaç kriter karşılandı?
        score = len(confirmed_by)
        min_score = 2 if dual else 3

        if score >= min_score:
            print(f"  🎯 {symbol}: ch1h:{ch1h:.1f}% vol:{vol_mul:.1f}x RSI:{rsi} kırılım:{breakout} | {confirmed_by}")
            enter_sticky(symbol, price, analysis, confirmed_by)
            hits += 1
        else:
            print(f"  👁️ {symbol}: ch1h:{ch1h:.1f}% vol:{vol_mul:.1f}x RSI:{rsi} | skor:{score}/{min_score}")

        time.sleep(0.3)

    if hits == 0:
        print(f"  ✅ Tarama bitti. Yapışkan mod adayı yok.")


def main():
    print("=" * 60)
    print("🎯 ATLAS MOMENTUM HUNTER V2")
    print("Çoklu Borsa Konfirmasyon + Hacim İvmesi + Fiyat Kırılması")
    print(f"ch1h>={STICKY_ENTRY_CH1H}% | Hacim>{STICKY_ENTRY_VOL_MUL}x | RSI {STICKY_RSI_MIN}-{STICKY_RSI_MAX}")
    print("=" * 60)

    while True:
        try:
            # Yapışkan coinleri güncelle
            if sticky_coins:
                print(f"\n📌 Yapışkan: {list(sticky_coins.keys())}")
                for symbol in list(sticky_coins.keys()):
                    update_sticky(symbol)
                    time.sleep(0.5)

            scan()
            time.sleep(60)

        except Exception as e:
            print(f"❌ Hata: {e}")
            import traceback
            traceback.print_exc()
            time.sleep(30)


if __name__ == "__main__":
    main()
