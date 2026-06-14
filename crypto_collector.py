# -*- coding: utf-8 -*-
"""
ATLAS KRİPTO KARTAL GÖZÜ — V12 SAVAŞ MİMARİSİ
================================================
HEDEF: Az sinyal, yüksek kalite. Gerçek dünya simülasyonu.

3 KATMAN:
1. SİNYAL LİSTESİ (Keşfet/anlık) — CRITICAL+HIGH, kullanıcı görür, push alır.
   Bot kararını etkilemez, sadece bilgilendirme.
2. BOT — sadece CRITICAL+BİRİKİM, tarama başına TEK aday (en yüksek skor).
3. TRAILING STOP — coin yükseldikçe stop yukarı çekilir, sabit hedef YOK.

KURALLAR:
- Max 3 açık pozisyon (Pro: +2 istisna = max 5)
- Tarama başına MAX 1 alım (en yüksek skorlu CRITICAL+BİRİKİM)
- Trailing stop:
    kâr <%3      → stop = giriş - %8 (sabit)
    kâr %3-%8    → stop = giriş (breakeven)
    kâr %8-%15   → stop = zirveden -%4
    kâr >=%15    → stop = zirveden -%6
- Min tutma: 4 saat (doğrulama katmanı çıkışları için — stop her zaman aktif)
- Kapatma: stop/trailing HER ZAMAN aktif. Doğrulama katmanı ≥5 puan + 4 saat
- Rotasyon: TAMAMEN KALDIRILDI
- Push: Sadece AL ve SAT anında

DOĞRULAMA KATMANI (4 saat sonrası, stop dışı çıkışlar için):
- OBV 4h "down" geçti          → +3 puan
- RSI giriş - 15+ düştü        → +2 puan
- Order book giriş oranının
  yarısına düştü               → +3 puan
- Zirveden %7+ çekilme (kârda) → +2 puan
- 4h hacim %70+ azaldı         → +1 puan
- Kâr < %3                     → -3 puan (erken çıkışı engeller)
Minimum: ≥5 puan VE tutma ≥4 saat

VERİ KAYNAKLARI:
- MEXC, Gate.io, Binance (teknik)
- CMC Basic (hacim/mcap/momentum)
- Fear & Greed (alternative.me)
- Order Book (MEXC depth)
"""

import os
import json
import time
import math
import requests
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from supabase import create_client
import firebase_admin
from firebase_admin import credentials, messaging
import threading

# ============================================================
# KURULUM
# ============================================================

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
GROQ_KEY = os.environ.get("GROQ_API_KEY")
FIREBASE_SERVICE_ACCOUNT = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
CMC_API_KEY = os.environ.get("CMC_API_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

signal_cache = {}
balance_lock = threading.Lock()

try:
    if FIREBASE_SERVICE_ACCOUNT and not firebase_admin._apps:
        cred = credentials.Certificate(json.loads(FIREBASE_SERVICE_ACCOUNT))
        firebase_admin.initialize_app(cred)
        print("✅ Firebase başlatıldı")
except Exception as e:
    print(f"⚠️ Firebase: {e}")

CMC_HEADERS = {
    "Accepts": "application/json",
    "X-CMC_PRO_API_KEY": CMC_API_KEY,
}

BINANCE_URLS = [
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
    "https://api4.binance.com",
]

# ============================================================
# SABİT PARAMETRELER
# ============================================================

STOP_PCT = 0.08              # %8 sabit stop (kâr <%3 iken)
TRAIL_ACTIVATE_PCT = 0.03    # %3 kârdan sonra breakeven
TRAIL_4_PCT = 0.08           # %8-15 kâr arası → zirveden -%4
TRAIL_6_PCT = 0.15           # %15+ kâr → zirveden -%6
MIN_HOLD_HOURS = 4            # 4 saat tutmadan doğrulama katmanı çıkışı yok
CLOSE_SCORE_MIN = 5           # Doğrulama katmanı minimum puan
MIN_PROFIT_PCT = 0.03         # Doğrulama çıkışı için min %3 kâr
MAX_OPEN_FREE = 3             # Free kullanıcı max açık pozisyon
MAX_OPEN_PRO = 3              # Pro max normal pozisyon
MAX_OPEN_PRO_EXCEPTIONAL = 5  # Pro istisna ile max
INVEST_PCT_CRITICAL = 0.20    # CRITICAL sinyalde bakiyenin %20'si
INVEST_PCT_HIGH = 0.15        # HIGH sinyalde %15
MAX_INVEST_USD = 50           # Pozisyon başı max $50
MIN_INVEST_USD = 5            # Pozisyon başı min $5
SIGNAL_COOLDOWN_H = 4         # Aynı coin için min 4 saat arayla sinyal

# ── İZLEME LİSTESİ (13'ün katları) ──────────────────────────
WATCHLIST_MAX = 39             # 3x13 — maksimum kapasite
WATCHLIST_TIER1 = 13           # 0-13: serbest giriş
WATCHLIST_TIER2 = 26           # 13-26: orta skor da girebilir
WATCHLIST_TTL_DAYS = 7         # 7 gün hareketsiz kalan silinir
WATCHLIST_MIN_SCORE = 9        # İzlemeye girmek için min skor (MEDIUM eşiği)
# "Hareketlendi" eşikleri — ilk görüldüğüne göre kıyas
WATCH_MOVE_RSI_DROP = 15        # RSI bu kadar düşerse hareketlendi
WATCH_MOVE_PRICE_PCT = 0.05     # Fiyat %5+ değiştiyse hareketlendi
WATCH_MOVE_VOL_PCT = 200        # Hacim değişimi bu kadar farklılaştıysa hareketlendi

# ── OTOMATİK ÖĞRENME SİSTEMİ ─────────────────────────────────
# 90 gün veri toplar, yeterli örnek + istatistiksel anlamlılık
# varsa score_coin'e küçük bir katsayı olarak otomatik uygulanır.
# Manuel müdahale gerektirmez.
LEARNING_MIN_SAMPLES = 30        # Grup başına min örnek (akademik standart: 30+)
LEARNING_MIN_ABS_Z = 1.96         # ~p<0.05 için z-skor eşiği
LEARNING_MAX_BONUS = 3            # score_coin'e uygulanacak max ek/eksi puan
LEARNING_BASELINE_WINRATE = 0.45  # "Başarı" referansı: 24s içinde >%2 kâr oranı
OUTCOME_CHECK_HOURS = [24, 72, 168]   # 24s, 72s, 7g sonuç ölçümü


STABLECOIN_BASES = {
    "USDT", "USDC", "FDUSD", "TUSD", "USDP", "GUSD", "PYUSD", "RLUSD",
    "USDG", "USDGO", "USD1", "BUSD", "USDD", "DAI", "USDS", "SUSDS",
    "USDX", "CRVUSD", "LISUSD", "USDE", "SUSDE", "USDY", "USDM", "USDL",
    "USDO", "USDN", "USD0", "USD0PP", "SFRXUSD", "FRAX", "SYRUPUSDC",
    "BUIDL", "USTC", "USDF", "EURT", "EURS", "EURC", "XAUT", "PAXG",
}

# ============================================================
# BOT AKTİVİTE LOGU — canlı izleme için (Flutter ekranı)
# ============================================================

def log_activity(event_type, symbol=None, price=None, pnl=None, pnl_pct=None,
                  detail=None, conviction=None, layer=None, market="CRYPTO"):
    """ALIM/SATIM/SİNYAL/ÖĞRENME/İZLEME olaylarını bot_activity_log'a yazar."""
    try:
        supabase.table("bot_activity_log").insert({
            "event_type": event_type,
            "symbol": symbol,
            "market": market,
            "price": price,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "detail": detail,
            "conviction": conviction,
            "layer": layer,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as e:
        print(f"⚠️ log_activity: {e}")

# ============================================================
# PUSH BİLDİRİM — Sadece AL ve SAT
# ============================================================

def send_push(title, body, signal_id=None, market="CRYPTO"):
    try:
        profiles = supabase.table("profiles").select("fcm_token") \
            .not_.is_("fcm_token", "null").execute()
        if not profiles.data:
            return
        tokens = [p["fcm_token"] for p in profiles.data if p.get("fcm_token")]
        for token in tokens:
            try:
                msg = messaging.Message(
                    notification=messaging.Notification(title=title, body=body),
                    data={
                        "market": market,
                        "signal_id": str(signal_id) if signal_id else "",
                        "click_action": "FLUTTER_NOTIFICATION_CLICK",
                    },
                    android=messaging.AndroidConfig(
                        priority="high",
                        notification=messaging.AndroidNotification(
                            channel_id="atlas_signals"
                        ),
                    ),
                    apns=messaging.APNSConfig(
                        payload=messaging.APNSPayload(
                            aps=messaging.Aps(sound="default", badge=1)
                        )
                    ),
                    token=token,
                )
                messaging.send(msg)
            except Exception:
                pass
        print(f"📱 Push: {title}")
    except Exception as e:
        print(f"❌ Push hatası: {e}")

# ============================================================
# VERİ KAYNAKLARI
# ============================================================

def get_mexc_tickers():
    try:
        r = requests.get(
            "https://api.mexc.com/api/v3/ticker/24hr", timeout=15
        )
        if r.status_code == 200:
            data = r.json()
            print(f"  → MEXC: {len(data)} parite")
            return data
    except Exception as e:
        print(f"⚠️ MEXC ticker: {e}")
    return []


def get_gateio_tickers():
    try:
        r = requests.get(
            "https://api.gateio.ws/api/v4/spot/tickers", timeout=15
        )
        if r.status_code == 200:
            data = r.json()
            print(f"  → Gate.io: {len(data)} parite")
            return data
    except Exception as e:
        print(f"⚠️ Gate.io ticker: {e}")
    return []


def get_cmc_coins():
    try:
        all_coins = {}
        r1 = requests.get(
            "https://pro-api.coinmarketcap.com/v1/cryptocurrency/listings/latest",
            headers=CMC_HEADERS,
            params={"limit": 500, "convert": "USD", "sort": "volume_24h", "sort_dir": "desc"},
            timeout=30,
        )
        if r1.status_code == 200:
            for c in r1.json().get("data", []):
                all_coins[c["id"]] = c
            print(f"  → CMC hacim: {len(all_coins)} coin")
        time.sleep(2)
        r2 = requests.get(
            "https://pro-api.coinmarketcap.com/v1/cryptocurrency/listings/latest",
            headers=CMC_HEADERS,
            params={"limit": 500, "convert": "USD", "sort": "percent_change_1h", "sort_dir": "desc"},
            timeout=30,
        )
        if r2.status_code == 200:
            for c in r2.json().get("data", []):
                if c["id"] not in all_coins:
                    all_coins[c["id"]] = c
            print(f"  → CMC momentum: toplam {len(all_coins)} coin")
        return list(all_coins.values())
    except Exception as e:
        print(f"❌ CMC: {e}")
        return []


def get_fear_greed():
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=5)
        d = r.json()["data"][0]
        return {"value": int(d["value"]), "label": d["value_classification"]}
    except Exception:
        return None

# ============================================================
# TEKNİK ANALİZ
# ============================================================

def get_klines(symbol, interval="4h", limit=100):
    sym_usdt = symbol.upper() + "USDT" if not symbol.upper().endswith("USDT") else symbol.upper()
    sym_gate = symbol.upper() + "_USDT"

    try:
        r = requests.get(
            "https://api.mexc.com/api/v3/klines",
            params={"symbol": sym_usdt, "interval": interval, "limit": limit},
            timeout=5,
        )
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list) and len(data) > 10:
                return data, "MEXC"
    except Exception:
        pass

    try:
        imap = {"1h": "3600", "4h": "14400", "15m": "900", "1d": "86400"}
        gi = imap.get(interval, "14400")
        r = requests.get(
            "https://api.gateio.ws/api/v4/spot/candlesticks",
            params={"currency_pair": sym_gate, "interval": gi, "limit": limit},
            timeout=5,
        )
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list) and len(data) > 10:
                converted = [
                    [int(c[0]) * 1000, c[5], c[3], c[4], c[2], c[1]]
                    for c in data
                ]
                return converted, "Gate.io"
    except Exception:
        pass

    for base in BINANCE_URLS:
        try:
            r = requests.get(
                f"{base}/api/v3/klines",
                params={"symbol": sym_usdt, "interval": interval, "limit": limit},
                timeout=5,
            )
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list) and len(data) > 10:
                    return data, "Binance"
        except Exception:
            continue

    return None, None


def get_orderbook(symbol):
    sym_usdt = symbol.upper() + "USDT"
    try:
        r = requests.get(
            "https://api.mexc.com/api/v3/depth",
            params={"symbol": sym_usdt, "limit": 100},
            timeout=5,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        bids = data.get("bids", [])
        asks = data.get("asks", [])
        if not bids or not asks:
            return None
        mid = (float(bids[0][0]) + float(asks[0][0])) / 2
        bid_wall = sum(float(p) * float(q) for p, q in bids if float(p) >= mid * 0.98)
        ask_wall = sum(float(p) * float(q) for p, q in asks if float(p) <= mid * 1.02)
        ratio = bid_wall / ask_wall if ask_wall > 0 else 0
        return {
            "bid_wall_usd": round(bid_wall, 2),
            "ask_wall_usd": round(ask_wall, 2),
            "bid_ask_ratio": round(ratio, 2),
        }
    except Exception:
        return None


def calculate_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    if len(gains) < period:
        return None
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    return round(100 - (100 / (1 + avg_gain / avg_loss)), 1)


def calculate_obv(closes, volumes):
    if len(closes) < 10:
        return None, None
    obv_list = [0]
    obv = 0
    for i in range(1, len(closes)):
        if closes[i] > closes[i - 1]:
            obv += volumes[i]
        elif closes[i] < closes[i - 1]:
            obv -= volumes[i]
        obv_list.append(obv)
    trend = "up" if obv_list[-1] > obv_list[-10] else "down"
    price_trend = "up" if closes[-1] > closes[-10] else "down"
    divergence = trend == "up" and price_trend == "down"
    return trend, divergence


def get_technical_data(symbol):
    try:
        k4h, exchange = get_klines(symbol, "4h", 100)
        if not k4h or len(k4h) < 20:
            return None
        closes = [float(k[4]) for k in k4h]
        volumes = [float(k[5]) for k in k4h]
        highs = [float(k[2]) for k in k4h]
        lows = [float(k[3]) for k in k4h]

        rsi = calculate_rsi(closes)
        obv_trend, obv_divergence = calculate_obv(closes, volumes)
        ch4h = ((closes[-1] - closes[-2]) / closes[-2]) * 100 if closes[-2] > 0 else 0

        vol_recent = sum(volumes[-5:]) / 5
        vol_prev = sum(volumes[-10:-5]) / 5
        vol_surge = vol_recent / vol_prev if vol_prev > 0 else 1

        avg_body = sum(abs(float(k[4]) - float(k[1])) for k in k4h[-20:]) / 20
        last_body = abs(closes[-1] - float(k4h[-1][1]))
        suspicious = vol_surge > 5 and last_body < avg_body * 0.3

        return {
            "rsi": rsi,
            "obv_trend": obv_trend,
            "obv_divergence": obv_divergence,
            "ch4h": round(ch4h, 2),
            "vol_surge_4h": round(vol_surge, 2),
            "vol_recent": vol_recent,
            "exchange": exchange,
            "suspicious_volume": suspicious,
            "price": closes[-1],
            "closes": closes,
            "volumes": volumes,
        }
    except Exception:
        return None

# ============================================================
# DOĞRULAMA KATLANI — Pozisyon kapatma kararı
# ============================================================

def compute_trailing_stop(buy_price, current, peak):
    """
    Coin yükseldikçe stop yukarı çekilir. Sabit hedef YOK.

    - Kâr < %3        → stop = giriş - %8 (sabit stop)
    - Kâr %3 - %8     → stop = giriş (breakeven koruması)
    - Kâr %8 - %15    → stop = zirveden %4 geri çekilme
    - Kâr >= %15      → stop = zirveden %6 geri çekilme (büyük harekete nefes alanı)

    Döner: (stop_price, etiket)
    """
    profit_pct = (peak - buy_price) / buy_price

    if profit_pct < TRAIL_ACTIVATE_PCT:
        return buy_price * (1 - STOP_PCT), "sabit_stop"
    elif profit_pct < TRAIL_4_PCT:
        return buy_price, "breakeven"
    elif profit_pct < TRAIL_6_PCT:
        return peak * (1 - 0.04), "trail_4pct"
    else:
        return peak * (1 - 0.06), "trail_6pct"


def close_confirmation_score(trade, tech, ob):
    """
    Kapatma kararını çok kaynaktan doğrula.
    Döner: (puan, [gerekçe listesi], veto_var_mı, trailing_stop_price)

    - Trailing/sabit stop HER ZAMAN aktif — veto'dan etkilenmez.
    - Doğrulama katmanı (puan >= CLOSE_SCORE_MIN) sadece tutma süresi
      >= 4 saat ise devreye girer.
    """
    score = 0
    reasons = []

    if not tech:
        return 0, [], True, None

    buy_price = float(trade["buy_price"])
    current = tech["price"]
    peak = max(float(trade.get("peak_price") or buy_price), current)
    entry_rsi = float(trade.get("entry_rsi") or 50)
    entry_ob_ratio = float(trade.get("entry_ob_ratio") or 0)
    entry_vol = float(trade.get("entry_vol_recent") or 0)

    trailing_stop, trail_label = compute_trailing_stop(buy_price, current, peak)

    # ── STOP/TRAILING — her zaman aktif ─────────────────────
    if current <= trailing_stop:
        label_map = {
            "sabit_stop": "Sabit stop (%8) vuruldu",
            "breakeven": "Breakeven stop vuruldu (kâr korunuyor)",
            "trail_4pct": "Trailing stop (zirveden %4) vuruldu",
            "trail_6pct": "Trailing stop (zirveden %6) vuruldu",
        }
        return 99, [label_map.get(trail_label, "Stop vuruldu")], False, trailing_stop

    # Tutma süresi — Supabase tarihleri tz bilgisi olmadan dönebilir
    try:
        buy_dt = datetime.fromisoformat(trade["buy_date"].replace("Z", "+00:00"))
        if buy_dt.tzinfo is None:
            buy_dt = buy_dt.replace(tzinfo=timezone.utc)
        hold_hours = (datetime.now(timezone.utc) - buy_dt).total_seconds() / 3600
    except Exception:
        hold_hours = MIN_HOLD_HOURS  # parse hatasında güvenli taraf: kilitleme

    # VETO: 4 saat dolmadan doğrulama katmanı çıkışı yok (stop hariç)
    if hold_hours < MIN_HOLD_HOURS:
        return 0, [f"VETO: {hold_hours:.1f}s/{MIN_HOLD_HOURS}s tutma — sadece stop aktif"], True, trailing_stop

    profit_pct = (current - buy_price) / buy_price

    # ── KAYNAK 1: OBV dönüşü ────────────────────────────────
    if trade.get("entry_obv") == "up" and tech.get("obv_trend") == "down":
        score += 3
        reasons.append("OBV down geçti (birikim bitti)")

    # ── KAYNAK 2: RSI düşüşü ────────────────────────────────
    cur_rsi = tech.get("rsi")
    if cur_rsi and entry_rsi and (entry_rsi - cur_rsi) >= 15:
        score += 2
        reasons.append(f"RSI {entry_rsi:.0f}→{cur_rsi:.0f} (momentum tepe yaptı)")

    # ── KAYNAK 3: Order book bozulması ──────────────────────
    if ob and entry_ob_ratio > 0:
        cur_ob = ob.get("bid_ask_ratio", 0)
        if entry_ob_ratio >= 1.5 and cur_ob <= entry_ob_ratio * 0.5:
            score += 3
            reasons.append(f"OB oranı {entry_ob_ratio:.2f}x → {cur_ob:.2f}x (satış baskısı)")
        elif entry_ob_ratio >= 1.5 and cur_ob <= 0.7:
            score += 2
            reasons.append(f"OB oranı {cur_ob:.2f}x (alım duvarı çöktü)")

    # ── KAYNAK 4: Zirveden çekilme (kâr varken) ─────────────
    if peak > buy_price and current < peak:
        drawdown = (peak - current) / peak
        if drawdown >= 0.07:
            score += 2
            reasons.append(f"Zirveden %{drawdown*100:.1f} geri çekildi")

    # ── KAYNAK 5: Hacim kuruması ─────────────────────────────
    if entry_vol > 0:
        vol_drop = 1 - (tech.get("vol_recent", entry_vol) / entry_vol)
        if vol_drop >= 0.70:
            score += 1
            reasons.append(f"Hacim %{vol_drop*100:.0f} azaldı")

    # ── Kâr <%3 → erken çıkışı caydır ───────────────────────
    if profit_pct < MIN_PROFIT_PCT:
        score -= 3
        reasons.append(f"Kâr %{profit_pct*100:.1f} < %{MIN_PROFIT_PCT*100:.0f} (erken çıkış değil)")

    return score, reasons, False, trailing_stop

# ============================================================
# SCAM FİLTRESİ
# ============================================================

def scam_check(symbol, price, ch1h, ch24h, vol_chg, tech):
    if price < 0.000001:
        return True, "Fiyat çok düşük"
    if ch24h >= 300:
        return True, "24s %300+ pump & dump"
    if ch1h >= 6:
        return True, "1s %6+ zaten geç"
    if not tech:
        return True, "Teknik veri yok"
    if tech.get("suspicious_volume"):
        return True, "Sahte hacim şüphesi"
    if tech.get("obv_trend") == "down" and ch1h > 10 and vol_chg > 200:
        return True, "OBV düşüyor — dağıtım"
    if tech.get("ch4h", 0) < -10:
        return True, "4s %10+ düşüş"
    rsi = tech.get("rsi")
    if rsi and rsi > 80 and tech.get("obv_trend") == "down":
        return True, "RSI 80+ OBV düşüyor"
    return False, ""


def is_stablecoin(symbol, price=None, ch1h=None, ch24h=None):
    if symbol.upper().strip() in STABLECOIN_BASES:
        return True
    if price and 0.97 <= price <= 1.03:
        if ch1h is not None and ch24h is not None:
            if abs(ch1h) < 0.3 and abs(ch24h) < 1.0:
                return True
    return False

# ============================================================
# PROJE YAŞ KONTROLÜ
# ============================================================

_cg_list_cache = {"data": None, "ts": 0}


def get_coin_age_days(symbol, date_added=None):
    if date_added:
        try:
            added = datetime.fromisoformat(date_added.replace("Z", "+00:00"))
            return (datetime.now(timezone.utc) - added).days
        except Exception:
            pass
    global _cg_list_cache
    now = time.time()
    if _cg_list_cache["data"] is None or now - _cg_list_cache["ts"] > 86400:
        try:
            r = requests.get("https://api.coingecko.com/api/v3/coins/list", timeout=15)
            if r.status_code == 200:
                _cg_list_cache["data"] = r.json()
                _cg_list_cache["ts"] = now
        except Exception:
            return None
    if not _cg_list_cache["data"]:
        return None
    sym_lower = symbol.lower()
    matches = [c for c in _cg_list_cache["data"] if c["symbol"] == sym_lower]
    if not matches:
        return None
    try:
        r = requests.get(
            f"https://api.coingecko.com/api/v3/coins/{matches[0]['id']}",
            params={"localization": "false", "tickers": "false",
                    "market_data": "false", "community_data": "false",
                    "developer_data": "false"},
            timeout=8,
        )
        if r.status_code == 200:
            genesis = r.json().get("genesis_date")
            if genesis:
                added = datetime.fromisoformat(genesis).replace(tzinfo=timezone.utc)
                return (datetime.now(timezone.utc) - added).days
    except Exception:
        pass
    return None

# ============================================================
# PUANLAMA SİSTEMİ
# ============================================================

def score_coin(symbol, name, price, ch1h, ch4h, ch24h, ch7d,
               vol_chg, mcap, cmc_rank, tech, fg, orderbook=None):
    score = 0
    reasons = []
    layer = "MOMENTUM"

    rsi = tech.get("rsi") if tech else None
    obv_trend = tech.get("obv_trend") if tech else None
    obv_div = tech.get("obv_divergence") if tech else False
    vol_surge = tech.get("vol_surge_4h", 1) if tech else 1

    # ── RSI ──────────────────────────────────────────────────
    if rsi is not None:
        if rsi < 25:
            score += 6
            reasons.append(f"📉 RSI {rsi} — Aşırı satım, dip")
        elif rsi < 35:
            score += 4
            reasons.append(f"📉 RSI {rsi} — Satım bölgesi")
        elif rsi < 45:
            score += 2
            reasons.append(f"📊 RSI {rsi} — Toparlanıyor")
        elif rsi > 75:
            score -= 3
            reasons.append(f"⚠️ RSI {rsi} — Aşırı alım")
        elif rsi > 60:
            score += 1

    # ── OBV ──────────────────────────────────────────────────
    if obv_div:
        score += 8
        layer = "BIRIKIM"
        reasons.append("🐋 OBV bullish diverjans — whale sessizce birikiyor")
    elif obv_trend == "up" and ch1h < 5:
        if rsi and rsi > 70:
            score += 2
            reasons.append("📈 OBV yükseliyor ama RSI yüksek — momentum")
        else:
            score += 6
            layer = "BIRIKIM"
            reasons.append("🐋 OBV yükseliyor, fiyat sessiz — birikim")
    elif obv_trend == "up":
        score += 3
        reasons.append("📈 OBV yükseliyor — alım baskısı")
    elif obv_trend == "down" and ch1h > 3:
        score -= 2

    if obv_trend == "down":
        layer = "MOMENTUM"

    # ── HACİM ────────────────────────────────────────────────
    if vol_chg >= 1000:
        score += 7
        if obv_trend != "down":
            layer = "BIRIKIM"
        reasons.append(f"🔥 Hacim %{vol_chg:.0f} — olağandışı whale")
    elif vol_chg >= 500:
        score += 5
        reasons.append(f"⚡ Hacim %{vol_chg:.0f} — güçlü ilgi")
    elif vol_chg >= 200:
        score += 3
        reasons.append(f"Hacim %{vol_chg:.0f} — kurumsal ilgi")
    elif vol_chg >= 100:
        score += 2
        reasons.append(f"Hacim %{vol_chg:.0f} — artış")
    elif vol_chg >= 50:
        score += 1

    if vol_surge >= 3 and ch1h < 5:
        score += 3
        if layer != "BIRIKIM" and obv_trend != "down":
            layer = "BIRIKIM"
        reasons.append(f"🐋 4s hacim {vol_surge:.1f}x — sessiz birikim")
    elif vol_surge >= 2:
        score += 1

    # Kataliz zorunlu — hiçbir şey yoksa eleme
    if vol_chg < 30 and (rsi is None or rsi > 55) and obv_trend != "up":
        return None

    # ── FİYAT HAREKET (ERKEN YAKALAMA) ───────────────────────
    if 0 <= ch1h <= 3:
        score += 2
        reasons.append(f"%{ch1h:.1f} — erken aşama, henüz oynamamış")
    elif ch1h <= 6:
        score += 1
        reasons.append(f"%{ch1h:.1f} — hafif hareket başladı")
    elif ch1h > 6:
        score -= 2

    if ch4h >= 10:
        score += 4
        reasons.append(f"%{ch4h:.1f} güçlü 4s trend")
    elif ch4h >= 5:
        score += 3
        reasons.append(f"%{ch4h:.1f} 4s trend")
    elif ch4h >= 2:
        score += 2
    elif ch4h >= 0:
        score += 1

    if ch24h >= 30:
        score += 3
        reasons.append(f"%{ch24h:.1f} güçlü 24s trend")
    elif ch24h >= 15:
        score += 2
    elif ch24h >= 5:
        score += 1

    # ── MARKET CAP ───────────────────────────────────────────
    if 0 < mcap < 5_000_000:
        score += 4
        reasons.append(f"💎 Micro cap (${mcap/1e6:.1f}M) — yüksek potansiyel")
    elif mcap < 20_000_000:
        score += 3
        reasons.append(f"💎 Küçük cap (${mcap/1e6:.1f}M)")
    elif mcap < 100_000_000:
        score += 2
        reasons.append(f"Cap: ${mcap/1e6:.0f}M")
    elif mcap < 500_000_000:
        score += 1

    # ── KORKU & AÇGÖZLÜLÜK ───────────────────────────────────
    if fg:
        if fg["value"] <= 15:
            score += 3
            reasons.append(f"😱 Aşırı Korku ({fg['value']}) — dip fırsatı")
        elif fg["value"] <= 25:
            score += 2
            reasons.append(f"😟 Korku ({fg['value']})")
        elif fg["value"] >= 80:
            score -= 1

    # ── ORDER BOOK ───────────────────────────────────────────
    if orderbook:
        ratio = orderbook.get("bid_ask_ratio", 0)
        bid_wall = orderbook.get("bid_wall_usd", 0)
        ask_wall = orderbook.get("ask_wall_usd", 0)
        if ratio >= 2 and bid_wall >= 20000:
            score += 4
            reasons.append(f"🧱 Alım duvarı ${bid_wall/1000:.0f}K — bid/ask {ratio}x")
        elif ratio >= 1.5:
            score += 2
            reasons.append(f"📥 Alım baskısı — bid/ask {ratio}x")
        elif ratio <= 0.5 and ask_wall >= 20000:
            score -= 3

    if score < 6:
        return None

    # ── OTOMATİK ÖĞRENME KATSAYISI ───────────────────────────
    # 90 gün veri biriktikçe otomatik devreye girer, manuel müdahale yok.
    learning_bonus = get_learning_bonus(layer, rsi, obv_trend)
    if learning_bonus != 0:
        score += learning_bonus
        reasons.append(f"🧠 Öğrenme katsayısı: {learning_bonus:+d}")

    if score >= 20:
        conviction = "CRITICAL"
    elif score >= 14:
        conviction = "HIGH"
    elif score >= 9:
        conviction = "MEDIUM"
    else:
        return None

    return {
        "symbol": symbol, "name": name, "price": price,
        "ch1h": ch1h, "ch4h": ch4h, "ch24h": ch24h, "ch7d": ch7d,
        "vol_chg": vol_chg, "mcap": mcap, "cmc_rank": cmc_rank,
        "rsi": rsi, "obv_trend": obv_trend, "obv_div": obv_div,
        "vol_recent": tech.get("vol_recent", 0) if tech else 0,
        "exchange": tech.get("exchange", "?") if tech else "?",
        "orderbook": orderbook,
        "conviction": conviction, "reasons": reasons,
        "score": score, "layer": layer,
    }

# ============================================================
# OTOMATİK ÖĞRENME SİSTEMİ
# ============================================================
# Akış:
# 1. Her bot alımında signal_outcomes'a kayıt atılır (record_signal_outcome)
# 2. Her taramada check_signal_outcomes() 24s/72s/7g dolan kayıtların
#    gerçek sonucunu ölçer (fiyat şu an ne oldu, kâr/zarar %)
# 3. update_learning_weights() yeterli örnek (>=30) + istatistiksel
#    anlamlılık (|z|>=1.96) varsa learning_weights tablosuna katsayı yazar
# 4. score_coin() bu katsayıyı okuyup ±LEARNING_MAX_BONUS uygular
# Hiçbir adım manuel müdahale gerektirmez — sistem kendi kendine evrilir.

def rsi_bucket(rsi):
    if rsi is None:
        return "RSI_YOK"
    if rsi < 30:
        return "RSI<30"
    if rsi < 50:
        return "RSI30-50"
    if rsi < 70:
        return "RSI50-70"
    return "RSI70+"


def record_signal_outcome(signal_id, symbol, layer, conviction, rsi, obv_trend, entry_price):
    """Bot alımı yapıldığında çağrılır — 24s/72s/7g sonuç takibi için kayıt at."""
    try:
        supabase.table("signal_outcomes").insert({
            "signal_id": signal_id,
            "symbol": symbol,
            "layer": layer,
            "conviction": conviction,
            "entry_rsi": rsi,
            "entry_obv": obv_trend,
            "entry_price": entry_price,
            "checked_24h": False,
            "checked_72h": False,
            "checked_7d": False,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as e:
        print(f"⚠️ signal_outcomes kayıt: {e}")


def check_signal_outcomes():
    """
    24s/72s/7g eşiklerini dolduran kayıtların gerçek sonucunu ölçer.
    Her taramada bir kerelik küçük bir iş — ağır değil (max ~20 sorgu).
    """
    try:
        now = datetime.now(timezone.utc)
        field_map = {24: "24h", 72: "72h", 168: "7d"}

        for hours in OUTCOME_CHECK_HOURS:
            field = field_map[hours]
            cutoff = (now - timedelta(hours=hours)).isoformat()

            rows = supabase.table("signal_outcomes") \
                .select("*") \
                .eq(f"checked_{field}", False) \
                .lte("created_at", cutoff) \
                .limit(20).execute()

            if not rows.data:
                continue

            for row in rows.data:
                try:
                    tech = get_technical_data(row["symbol"])
                    if not tech:
                        supabase.table("signal_outcomes").update({
                            f"checked_{field}": True
                        }).eq("id", row["id"]).execute()
                        continue

                    current = tech["price"]
                    entry = float(row["entry_price"])
                    pct = ((current - entry) / entry) * 100 if entry > 0 else 0

                    supabase.table("signal_outcomes").update({
                        f"price_{field}": current,
                        f"pct_{field}": round(pct, 2),
                        f"checked_{field}": True,
                    }).eq("id", row["id"]).execute()

                except Exception:
                    continue
                time.sleep(0.2)

    except Exception as e:
        print(f"❌ check_signal_outcomes: {e}")


def update_learning_weights():
    """
    layer + RSI bucket + OBV kombinasyonu başına 24s sonuçlarını analiz eder.
    >=30 örnek VE |z|>=1.96 (yaklaşık p<0.05) ise learning_weights'e
    küçük bir katsayı yazar. Aksi halde dokunmaz (veri yetersiz).
    """
    try:
        rows = supabase.table("signal_outcomes") \
            .select("layer, entry_rsi, entry_obv, pct_24h") \
            .eq("checked_24h", True) \
            .not_.is_("pct_24h", "null") \
            .execute()

        if not rows.data or len(rows.data) < LEARNING_MIN_SAMPLES:
            return

        groups = defaultdict(list)
        for r in rows.data:
            bucket = rsi_bucket(r.get("entry_rsi"))
            key = (r.get("layer") or "?", bucket, r.get("entry_obv") or "?")
            groups[key].append(r["pct_24h"])

        updated = 0
        for (layer, bucket, obv), pcts in groups.items():
            n = len(pcts)
            if n < LEARNING_MIN_SAMPLES:
                continue

            wins = sum(1 for p in pcts if p > 2)
            win_rate = wins / n

            # z-test: gözlenen oran vs baseline (0.45)
            p0 = LEARNING_BASELINE_WINRATE
            se = math.sqrt(p0 * (1 - p0) / n)
            z = (win_rate - p0) / se if se > 0 else 0

            if abs(z) < LEARNING_MIN_ABS_Z:
                continue  # istatistiksel olarak anlamsız, dokunma

            # z=1.96 → bonus=1, z=3+ → bonus=LEARNING_MAX_BONUS, doğrusal ölçek
            magnitude = min(abs(z) / 3.0, 1.0) * LEARNING_MAX_BONUS
            bonus = round(magnitude) if z > 0 else -round(magnitude)
            bonus = max(-LEARNING_MAX_BONUS, min(LEARNING_MAX_BONUS, bonus))

            if bonus == 0:
                continue

            key_str = f"{layer}|{bucket}|{obv}"
            supabase.table("learning_weights").upsert({
                "pattern_key": key_str,
                "layer": layer,
                "rsi_bucket": bucket,
                "obv_trend": obv,
                "sample_size": n,
                "win_rate": round(win_rate * 100, 1),
                "z_score": round(z, 2),
                "bonus": bonus,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }, on_conflict="pattern_key").execute()

            updated += 1
            print(f"  🧠 ÖĞRENME: {key_str} → bonus:{bonus:+d} "
                  f"(n={n}, win_rate=%{win_rate*100:.1f}, z={z:.2f})")

            log_activity("OGRENME", detail=f"{key_str} → bonus:{bonus:+d} "
                          f"(n={n}, win_rate=%{win_rate*100:.1f}, z={z:.2f})",
                          layer=layer)

        if updated:
            print(f"  🧠 {updated} patern güncellendi")

    except Exception as e:
        print(f"❌ update_learning_weights: {e}")


_learning_cache = {"data": {}, "ts": 0}


def get_learning_bonus(layer, rsi, obv_trend):
    """
    score_coin tarafından çağrılır. learning_weights'ten önceden
    hesaplanmış katsayıyı okur. 10 dakika cache'lenir — her coin
    için ayrı sorgu atmaz.
    """
    global _learning_cache
    now = time.time()
    if now - _learning_cache["ts"] > 600:
        try:
            rows = supabase.table("learning_weights").select("*").execute()
            _learning_cache["data"] = {r["pattern_key"]: r["bonus"] for r in (rows.data or [])}
            _learning_cache["ts"] = now
        except Exception:
            pass

    bucket = rsi_bucket(rsi)
    key = f"{layer}|{bucket}|{obv_trend}"
    return _learning_cache["data"].get(key, 0)


# Kapasite: max 39 (3x13). Doluyken yeni aday, en düşük skorlu
# kayıttan yüksekse onun yerine geçer. 7 gün hareketsiz kalan silinir.
# "Hareketlendi" tespit edilirse status='signaled' yapılır (push yok,
# sadece Flutter tarafında etiketleme için).

def watchlist_update(symbol, price, rsi, obv_trend, vol_chg, score, source="CMC"):
    """MEDIUM (9-13) skorlu, sinyal eşiğini geçemeyen adayı izleme listesine ekle/güncelle."""
    try:
        existing = supabase.table("crypto_watchlist") \
            .select("id, last_score, observation_count") \
            .eq("symbol", symbol).eq("status", "watching") \
            .limit(1).execute()

        if existing.data:
            row = existing.data[0]
            supabase.table("crypto_watchlist").update({
                "last_seen": datetime.now(timezone.utc).isoformat(),
                "last_price": price,
                "last_rsi": rsi,
                "last_obv": obv_trend,
                "last_vol_chg": vol_chg,
                "last_score": score,
                "observation_count": row["observation_count"] + 1,
            }).eq("id", row["id"]).execute()
            return

        # Yeni kayıt — kapasite kontrolü
        count_res = supabase.table("crypto_watchlist") \
            .select("id").eq("status", "watching").execute()
        current_count = len(count_res.data or [])

        if current_count >= WATCHLIST_MAX:
            # En düşük skorlu kaydı bul
            weakest = supabase.table("crypto_watchlist") \
                .select("id, symbol, last_score") \
                .eq("status", "watching") \
                .order("last_score", desc=False) \
                .limit(1).execute()
            if not weakest.data:
                return
            weak = weakest.data[0]
            if score <= (weak["last_score"] or 0):
                # Yeni aday mevcut en zayıftan güçlü değil — alma
                return
            # Zayıfı sil, yenisi için yer aç
            supabase.table("crypto_watchlist").delete().eq("id", weak["id"]).execute()
            print(f"  🔄 İzleme listesi: {weak['symbol']} (skor:{weak['last_score']}) "
                  f"çıktı, {symbol} (skor:{score}) girdi")

        supabase.table("crypto_watchlist").insert({
            "symbol": symbol,
            "source": source,
            "first_seen": datetime.now(timezone.utc).isoformat(),
            "last_seen": datetime.now(timezone.utc).isoformat(),
            "first_price": price,
            "last_price": price,
            "observation_count": 1,
            "last_rsi": rsi,
            "last_obv": obv_trend,
            "last_vol_chg": vol_chg,
            "last_score": score,
            "status": "watching",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
        print(f"  👁️ İZLEMEYE ALINDI: {symbol} (skor:{score}) @ ${price}")

    except Exception as e:
        print(f"⚠️ Watchlist update {symbol}: {e}")


def check_watchlist_movement():
    """
    Her taramanın BAŞINDA çalışır:
    1. İzlemedeki coinlerin güncel RSI/OBV/fiyat/hacmini kontrol et.
    2. "Hareketlendi" eşiğini geçen varsa status='signaled' yap (push yok).
    3. 7 gün hareketsiz kalan 'watching' kayıtları sil.
    """
    try:
        watching = supabase.table("crypto_watchlist") \
            .select("*").eq("status", "watching").execute()
        if not watching.data:
            return

        print(f"👁️ İzleme listesi: {len(watching.data)} coin kontrol ediliyor...")
        now = datetime.now(timezone.utc)
        moved = 0
        expired = 0

        for row in watching.data:
            symbol = row["symbol"]
            try:
                # TTL kontrolü
                first_seen = datetime.fromisoformat(row["first_seen"].replace("Z", "+00:00"))
                if first_seen.tzinfo is None:
                    first_seen = first_seen.replace(tzinfo=timezone.utc)
                age_days = (now - first_seen).total_seconds() / 86400

                if age_days >= WATCHLIST_TTL_DAYS:
                    supabase.table("crypto_watchlist").delete().eq("id", row["id"]).execute()
                    expired += 1
                    continue

                tech = get_technical_data(symbol)
                if not tech:
                    continue

                cur_price = tech["price"]
                cur_rsi = tech.get("rsi")
                cur_vol_surge = tech.get("vol_surge_4h", 1) * 100

                # Referans = ÖNCEKİ KONTROL (last_*), ilk görülme değil.
                # observation_count == 1 ise henüz ikinci kontrol yok,
                # bu turda sadece referans güncellenir, hareket ölçülmez.
                prev_price = row.get("last_price")
                prev_rsi = row.get("last_rsi")
                prev_vol = row.get("last_vol_chg")

                hareketlendi = False
                trigger = ""

                if row.get("observation_count", 0) >= 1 and prev_price:
                    prev_price_f = float(prev_price)
                    price_change = abs(cur_price - prev_price_f) / prev_price_f if prev_price_f > 0 else 0

                    rsi_drop = 0
                    if cur_rsi is not None and prev_rsi is not None:
                        rsi_drop = float(prev_rsi) - cur_rsi

                    vol_jump = 0
                    if prev_vol is not None:
                        vol_jump = cur_vol_surge - float(prev_vol)

                    if price_change >= WATCH_MOVE_PRICE_PCT:
                        hareketlendi = True
                        trigger = f"Fiyat %{price_change*100:.1f} değişti (son kontrolden)"
                    elif rsi_drop >= WATCH_MOVE_RSI_DROP:
                        hareketlendi = True
                        trigger = f"RSI {float(prev_rsi):.0f}→{cur_rsi:.0f} düştü (son kontrolden)"
                    elif vol_jump >= WATCH_MOVE_VOL_PCT:
                        hareketlendi = True
                        trigger = f"Hacim sıçraması %{vol_jump:.0f} (son kontrolden)"

                if hareketlendi:
                    supabase.table("crypto_watchlist").update({
                        "status": "signaled",
                        "signal_date": now.isoformat(),
                        "last_seen": now.isoformat(),
                        "last_price": cur_price,
                        "last_rsi": cur_rsi,
                        "last_vol_chg": cur_vol_surge,
                    }).eq("id", row["id"]).execute()
                    moved += 1
                    print(f"  🔥 HAREKETLENDİ: {symbol} — {trigger}")
                    log_activity("IZLEME", symbol=symbol, price=cur_price,
                                  detail=trigger)
                else:
                    supabase.table("crypto_watchlist").update({
                        "last_seen": now.isoformat(),
                        "last_price": cur_price,
                        "last_rsi": cur_rsi,
                        "last_vol_chg": cur_vol_surge,
                        "observation_count": row["observation_count"] + 1,
                    }).eq("id", row["id"]).execute()

                time.sleep(0.2)

            except Exception:
                continue

        if moved or expired:
            print(f"  📊 İzleme: {moved} hareketlendi, {expired} süresi doldu (silindi)")

    except Exception as e:
        print(f"❌ check_watchlist_movement: {e}")



def merge_exchange_data(mexc_tickers, gateio_tickers, cmc_coins):
    merged = {}

    for c in cmc_coins:
        q = c.get("quote", {}).get("USD", {})
        symbol = c.get("symbol", "")
        price = float(q.get("price", 0) or 0)
        if price <= 0 or price > 2.0:
            continue
        if is_stablecoin(symbol, price,
                         float(q.get("percent_change_1h", 0) or 0),
                         float(q.get("percent_change_24h", 0) or 0)):
            continue
        vol = float(q.get("volume_24h", 0) or 0)
        if vol < 100_000:
            continue
        merged[symbol] = {
            "symbol": symbol,
            "name": c.get("name", symbol),
            "price": price,
            "ch1h": float(q.get("percent_change_1h", 0) or 0),
            "ch4h": float(q.get("percent_change_4h", 0) or 0),
            "ch24h": float(q.get("percent_change_24h", 0) or 0),
            "ch7d": float(q.get("percent_change_7d", 0) or 0),
            "vol_chg": float(q.get("volume_change_24h", 0) or 0),
            "mcap": float(q.get("market_cap", 0) or 0),
            "cmc_rank": c.get("cmc_rank", 9999),
            "date_added": c.get("date_added"),
            "sources": ["CMC"],
        }

    for t in mexc_tickers:
        sym = t.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        base = sym[:-4]
        price = float(t.get("lastPrice", 0) or 0)
        if price <= 0 or price > 2.0:
            continue
        if is_stablecoin(base, price, 0, float(t.get("priceChangePercent", 0) or 0)):
            continue
        if float(t.get("quoteVolume", 0) or 0) < 100_000:
            continue
        if base in merged:
            merged[base]["sources"].append("MEXC")
        else:
            merged[base] = {
                "symbol": base, "name": base, "price": price,
                "ch1h": 0, "ch4h": 0,
                "ch24h": float(t.get("priceChangePercent", 0) or 0),
                "ch7d": 0, "vol_chg": 0, "mcap": 0, "cmc_rank": 9999,
                "sources": ["MEXC"],
            }

    for t in gateio_tickers:
        pair = t.get("currency_pair", "")
        if not pair.endswith("_USDT"):
            continue
        base = pair[:-5]
        price = float(t.get("last", 0) or 0)
        if price <= 0 or price > 2.0:
            continue
        if is_stablecoin(base, price, 0, float(t.get("change_percentage", 0) or 0)):
            continue
        if float(t.get("quote_volume", 0) or 0) < 100_000:
            continue
        if base in merged:
            merged[base]["sources"].append("Gate.io")
        else:
            merged[base] = {
                "symbol": base, "name": base, "price": price,
                "ch1h": 0, "ch4h": 0,
                "ch24h": float(t.get("change_percentage", 0) or 0),
                "ch7d": 0, "vol_chg": 0, "mcap": 0, "cmc_rank": 9999,
                "sources": ["Gate.io"],
            }

    print(f"  → Birleşik evren: {len(merged)} coin")
    return list(merged.values())

# ============================================================
# AI AÇIKLAMA
# ============================================================

def get_ai_explanation(s, fg):
    try:
        price = s["price"]
        if price < 0.0001:
            ps = f"${price:.8f}"
        elif price < 0.01:
            ps = f"${price:.6f}"
        elif price < 1:
            ps = f"${price:.4f}"
        else:
            ps = f"${price:.2f}"

        stop = price * (1 - STOP_PCT)
        fg_str = f"{fg['value']} ({fg['label']})" if fg else "Veri yok"

        layer_ctx = {
            "BIRIKIM": "BİRİKİM: Whale sessizce giriyor, fiyat henüz oynamamış.",
            "MOMENTUM": "MOMENTUM: Fiyat ve hacim birlikte yükseliyor.",
        }.get(s.get("layer", "MOMENTUM"), "")

        ob = s.get("orderbook")
        ob_str = (
            f"Order book: bid/ask {ob['bid_ask_ratio']}x, alım ${ob['bid_wall_usd']/1000:.0f}K"
            if ob else "Order book verisi yok (bu konudan bahsetme)"
        )

        rsi_str = f"{s['rsi']}" if s['rsi'] is not None else "VERİ YOK"
        obv_str = s['obv_trend'] if s['obv_trend'] is not None else "VERİ YOK"

        prompt = f"""Profesyonel kripto analistisin. Türkçe. Kısa ve net.
KURAL: Sadece verilen sayısal verilere dayan. "VERİ YOK" ise o konuya hiç girme.

{s['symbol']} | {ps} | 1s:%{s['ch1h']:.1f} | 4s:%{s['ch4h']:.1f} | 24s:%{s['ch24h']:.1f}
Hacim:%{s['vol_chg']:.0f} | RSI:{rsi_str} | OBV:{obv_str}
Güven:{s['conviction']} | Katman:{s.get('layer','?')} | Borsa:{s.get('exchange','?')}
Korku/Açgözlülük:{fg_str}
Başlangıç stop:%{STOP_PCT*100:.0f} = {stop:.8f} (kâr arttıkça stop yukarı çekilir, sabit hedef yok)
{layer_ctx}
{ob_str}
Sinyaller: {' | '.join(s['reasons']) if s['reasons'] else 'Yok'}

===ACEMİ===
[Maks 2 cümle. Neden hareket ediyor, ne yapmalı.]
===USTA===
[Maks 3 cümle. RSI/OBV/order book yorumu, kritik seviye.]
===PRO===
[Maks 4 cümle. Whale/OB güveni (1-10), P&D riski (1-10), risk/ödül, giriş/stop $.]"""

        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"},
            json={
                "model": "llama-3.1-8b-instant",
                "messages": [
                    {
                        "role": "system",
                        "content": "Kripto analistisin. SADECE verilen sayısal verilere dayan. Formatı koru. Türkçe.",
                    },
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": 600,
                "temperature": 0.2,
            },
            timeout=15,
        )
        resp = r.json()
        return resp["choices"][0]["message"]["content"] if "choices" in resp else ""
    except Exception:
        return ""


def parse_ai(text):
    a, u, p = "", "", ""
    try:
        if "===ACEMİ===" in text:
            a = text.split("===ACEMİ===")[1].split("===USTA===")[0].strip()
        if "===USTA===" in text:
            u = text.split("===USTA===")[1].split("===PRO===")[0].strip()
        if "===PRO===" in text:
            p = text.split("===PRO===")[1].strip()
    except Exception:
        pass
    return a, u, p

# ============================================================
# SİNYAL KAYDET
# ============================================================

def get_last_signal_time(symbol):
    try:
        r = supabase.table("crypto_signals").select("created_at") \
            .eq("symbol", symbol).order("created_at", ascending=False) \
            .limit(1).execute()
        if r.data:
            dt = datetime.fromisoformat(r.data[0]["created_at"].replace("Z", "+00:00"))
            return dt.timestamp()
        return 0
    except Exception:
        return 0


def save_signal(s, fg, allow_buy=False):
    try:
        price = s["price"]
        if price < 0.0001:
            ps = f"${price:.8f}"
        elif price < 0.01:
            ps = f"${price:.6f}"
        elif price < 1:
            ps = f"${price:.4f}"
        else:
            ps = f"${price:.2f}"

        ai = get_ai_explanation(s, fg)
        acemi, usta, pro = parse_ai(ai)

        emoji = "🔥" if s["conviction"] == "CRITICAL" else "⚡" if s["conviction"] == "HIGH" else "🚀"
        le = "🐋" if s["layer"] == "BIRIKIM" else "📈"

        desc = (
            f"{emoji} {s['symbol']}/USDT | {ps} | "
            f"1s:%{s['ch1h']:+.1f} 4s:%{s['ch4h']:+.1f} | "
            f"Vol:%{s['vol_chg']:+.0f} | RSI:{s['rsi']} | "
            f"{le}{s['layer']} | {s['conviction']} | [{s.get('exchange','?')}]"
        )

        res = supabase.table("crypto_signals").insert({
            "symbol": s["symbol"],
            "coin": s["symbol"],
            "signal_type": s["layer"].lower(),
            "conviction": s["conviction"],
            "value": round(s["ch1h"], 2),
            "price": price,
            "volume_ratio": round(s["vol_chg"] / 100, 2),
            "price_change_1h": round(s["ch1h"], 2),
            "price_change_4h": round(s["ch4h"], 2),
            "price_change_24h": round(s["ch24h"], 2),
            "description": desc,
            "acemi_explanation": acemi,
            "usta_explanation": usta,
            "pro_explanation": pro,
            "market": "CRYPTO",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }).execute()

        signal_cache[s["symbol"]] = time.time()
        sid = res.data[0].get("id") if res.data else None

        log_activity("SINYAL", symbol=s["symbol"], price=price,
                      detail=f"Score:{s['score']} | RSI:{s['rsi']} | OBV:{s['obv_trend']}",
                      conviction=s["conviction"], layer=s["layer"])

        if allow_buy:
            bot_buy(s, sid)

        print(f"✅ SİNYAL: {desc}")
        return True

    except Exception as e:
        print(f"❌ {s.get('symbol','?')}: {e}")
        return False

# ============================================================
# BOT — ALIM
# ============================================================

def bot_buy(s, signal_id):
    symbol = s["symbol"]
    price = s["price"]
    conviction = s["conviction"]
    layer = s["layer"]

    try:
        portfolios = supabase.table("demo_portfolios") \
            .select("user_id, crypto_balance").execute()
        if not portfolios.data:
            return

        for p in portfolios.data:
            user_id = p["user_id"]
            _execute_buy(
                user_id=user_id,
                symbol=symbol,
                price=price,
                signal_id=signal_id,
                conviction=conviction,
                layer=layer,
                rsi=s.get("rsi"),
                obv=s.get("obv_trend"),
                ob_ratio=s["orderbook"]["bid_ask_ratio"] if s.get("orderbook") else None,
                vol_recent=s.get("vol_recent", 0),
                reasons=s.get("reasons"),
                score=s.get("score", 0),
            )
            time.sleep(0.2)

    except Exception as e:
        print(f"❌ bot_buy: {e}")


def _execute_buy(user_id, symbol, price, signal_id, conviction, layer,
                 rsi=None, obv=None, ob_ratio=None, vol_recent=0,
                 reasons=None, score=0):
    try:
        profile = supabase.table("profiles").select("is_pro") \
            .eq("id", user_id).limit(1).execute()
        is_pro = profile.data[0].get("is_pro", False) if profile.data else False

        open_trades = supabase.table("demo_trades").select("*") \
            .eq("user_id", user_id).eq("market", "CRYPTO") \
            .eq("status", "open").execute()
        open_list = open_trades.data or []
        open_count = len(open_list)

        # Kapasite kontrolü
        max_normal = MAX_OPEN_PRO if is_pro else MAX_OPEN_FREE
        exceptional_count = sum(1 for t in open_list if t.get("is_exceptional"))
        is_exceptional = False

        if open_count >= max_normal:
            if is_pro and score >= 24 and exceptional_count < 2 and open_count < MAX_OPEN_PRO_EXCEPTIONAL:
                is_exceptional = True
                print(f"  🌟 İSTİSNAİ: {symbol} (score={score}) — slot {open_count+1}")
            else:
                # Rotasyon YOK — doluysa geç
                print(f"  ⏭️ {user_id} dolu ({open_count}/{max_normal}) — {symbol} atlandı")
                return

        # Free kullanıcı aylık limit
        if not is_pro:
            month_start = datetime.now(timezone.utc).replace(
                day=1, hour=0, minute=0, second=0
            ).isoformat()
            mt = supabase.table("demo_trades").select("id") \
                .eq("user_id", user_id).eq("market", "CRYPTO") \
                .gte("created_at", month_start).execute()
            if len(mt.data or []) >= MAX_OPEN_FREE:
                return

        # Yatırım miktarı
        pct = INVEST_PCT_CRITICAL if conviction == "CRITICAL" else INVEST_PCT_HIGH

        stop_price = round(price * (1 - STOP_PCT), 10)
        entry_reason = " | ".join(reasons) if reasons else None

        with balance_lock:
            fresh = supabase.table("demo_portfolios").select("crypto_balance") \
                .eq("user_id", user_id).limit(1).execute()
            balance = fresh.data[0]["crypto_balance"] if fresh.data else 0
            invest = min(balance * pct, MAX_INVEST_USD)
            if invest < MIN_INVEST_USD:
                return

            supabase.table("demo_trades").insert({
                "user_id": user_id,
                "symbol": symbol,
                "market": "CRYPTO",
                "signal_id": signal_id,
                "buy_price": price,
                "buy_date": datetime.now(timezone.utc).isoformat(),
                "quantity": round(invest / price, 6),
                "status": "open",
                "signal_layer": layer,
                "entry_rsi": rsi,
                "entry_obv": obv,
                "entry_conviction": conviction,
                "entry_ob_ratio": ob_ratio,
                "entry_vol_recent": vol_recent,
                "stop_price": stop_price,
                "current_price": price,
                "peak_price": price,
                "entry_reason": entry_reason,
                "is_exceptional": is_exceptional,
                "sell_warning_sent": False,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }).execute()

            supabase.table("demo_portfolios").update({
                "crypto_balance": round(balance - invest, 2)
            }).eq("user_id", user_id).execute()

        tag = "🌟" if is_exceptional else "✅"
        print(f"  {tag} ALIM: {user_id[:8]} → {symbol} ${invest:.0f} "
              f"| Başlangıç stop:{stop_price:.8f} (%{STOP_PCT*100:.0f})")

        # Öğrenme sistemi — 24s/72s/7g sonuç takibi için kayıt
        record_signal_outcome(signal_id, symbol, layer, conviction, rsi, obv, price)

        log_activity("ALIM", symbol=symbol, price=price,
                      detail=f"${invest:.0f} yatırım | Stop:{stop_price:.8f}{' | İSTİSNAİ' if is_exceptional else ''}",
                      conviction=conviction, layer=layer)

        # PUSH — sadece alım anında
        if price < 0.0001:
            ps = f"${price:.8f}"
        elif price < 0.01:
            ps = f"${price:.6f}"
        elif price < 1:
            ps = f"${price:.4f}"
        else:
            ps = f"${price:.2f}"

        send_push(
            title=f"🟢 ALINDI — {symbol}",
            body=f"{ps} | Stop %{STOP_PCT*100:.0f} | {layer} | Trailing aktif",
            signal_id=signal_id,
        )

    except Exception as e:
        print(f"❌ _execute_buy: {e}")

# ============================================================
# BOT — POZİSYON İZLEME VE KAPATMA
# ============================================================

def check_positions():
    try:
        trades = supabase.table("demo_trades").select("*") \
            .eq("status", "open").eq("market", "CRYPTO").execute()
        if not trades.data:
            return

        print(f"🔍 {len(trades.data)} açık pozisyon kontrol ediliyor...")

        for trade in trades.data:
            try:
                tech = get_technical_data(trade["symbol"])
                if not tech:
                    continue

                current = tech["price"]
                buy_price = float(trade["buy_price"])
                peak = max(float(trade.get("peak_price") or buy_price), current)
                change_pct = (current - buy_price) / buy_price * 100

                # Order book çek
                ob = get_orderbook(trade["symbol"])

                # Doğrulama katmanı + trailing stop
                score, reasons, veto, trailing_stop = close_confirmation_score(trade, tech, ob)

                # Canlı veri güncelle (peak + güncel trailing stop)
                supabase.table("demo_trades").update({
                    "current_price": current,
                    "peak_price": peak,
                    "stop_price": trailing_stop,
                }).eq("id", trade["id"]).execute()

                if veto:
                    print(f"  ⏳ {trade['symbol']} %{change_pct:+.1f} — {reasons[0] if reasons else 'veto'}")
                    time.sleep(0.3)
                    continue

                should_close = score >= CLOSE_SCORE_MIN or score >= 99

                if not should_close:
                    print(f"  👁️ {trade['symbol']} %{change_pct:+.1f} — "
                          f"doğrulama puanı {score}/{CLOSE_SCORE_MIN} (yetersiz)")
                    time.sleep(0.3)
                    continue

                # Kapatma kararı verildi
                exit_reason = " | ".join(reasons) if reasons else "Doğrulama eşiği aşıldı"
                pl = (current - buy_price) * float(trade["quantity"])

                supabase.table("demo_trades").update({
                    "sell_price": current,
                    "sell_date": datetime.now(timezone.utc).isoformat(),
                    "status": "closed",
                    "profit_loss": round(pl, 2),
                    "exit_reason": exit_reason,
                }).eq("id", trade["id"]).execute()

                with balance_lock:
                    port = supabase.table("demo_portfolios").select("crypto_balance") \
                        .eq("user_id", trade["user_id"]).limit(1).execute()
                    if port.data:
                        new_bal = port.data[0]["crypto_balance"] + (float(trade["quantity"]) * current)
                        supabase.table("demo_portfolios").update({
                            "crypto_balance": round(new_bal, 2)
                        }).eq("user_id", trade["user_id"]).execute()

                action = "💰 KAR" if pl > 0 else "🛑 ZARAR" if pl < 0 else "➖ BREAKEVEN"
                print(f"  {action}: {trade['symbol']} %{change_pct:+.1f} | ${pl:.2f} | puan:{score} | {exit_reason[:60]}")

                log_activity("SATIM", symbol=trade["symbol"], price=current,
                              pnl=round(pl, 2), pnl_pct=round(change_pct, 2),
                              detail=exit_reason[:120],
                              conviction=trade.get("entry_conviction"),
                              layer=trade.get("signal_layer"))

                # PUSH — sadece kapanış anında
                if current < 0.0001:
                    ps = f"${current:.8f}"
                elif current < 0.01:
                    ps = f"${current:.6f}"
                elif current < 1:
                    ps = f"${current:.4f}"
                else:
                    ps = f"${current:.2f}"

                emoji = "💰" if pl > 0 else "🛑" if pl < 0 else "➖"
                send_push(
                    title=f"{emoji} KAPANDI — {trade['symbol']}",
                    body=f"%{change_pct:+.1f} | ${pl:.2f} | {exit_reason[:40]}",
                    signal_id=trade.get("signal_id"),
                )

            except Exception as e:
                print(f"⚠️ {trade.get('symbol','?')} pozisyon hata: {e}")
                continue

            time.sleep(0.5)

    except Exception as e:
        print(f"❌ check_positions: {e}")


def position_monitor_loop():
    while True:
        try:
            check_positions()
        except Exception as e:
            print(f"❌ Monitor loop: {e}")
        time.sleep(60)  # 60 saniyede bir kontrol (önceki 10s → çok sık)

# ============================================================
# ANA TARAMA
# ============================================================

def scan_once(scan_count=0):
    print(f"\n🦅 KARTAL GÖZÜ V12 — {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}")
    print("=" * 55)

    # ── ÖNCE izleme listesini kontrol et ───────────────────
    check_watchlist_movement()

    # ── Öğrenme sistemi — 3 taramada bir (ağır işlem) ──────
    if scan_count % 3 == 0:
        check_signal_outcomes()
        update_learning_weights()

    fg = get_fear_greed()
    if fg:
        e = "😱" if fg["value"] <= 25 else "😐" if fg["value"] <= 50 else "😊" if fg["value"] <= 75 else "🤑"
        print(f"{e} Korku/Açgözlülük: {fg['value']} ({fg['label']})")

    print("📡 Veri toplanıyor...")
    mexc = get_mexc_tickers()
    time.sleep(1)
    gateio = get_gateio_tickers()
    time.sleep(1)
    cmc = get_cmc_coins()

    coins = merge_exchange_data(mexc, gateio, cmc)
    if not coins:
        print("⚠️ Veri alınamadı")
        return 0

    now = time.time()
    scored = []
    print(f"🔍 {len(coins)} coin analiz ediliyor...")

    for coin in coins:
        symbol = coin["symbol"]
        try:
            # Cache — aynı coin için 4 saat arayla sinyal
            if symbol in signal_cache and now - signal_cache[symbol] < SIGNAL_COOLDOWN_H * 3600:
                continue
            last_signal = get_last_signal_time(symbol)
            if now - last_signal < SIGNAL_COOLDOWN_H * 3600:
                signal_cache[symbol] = now
                continue

            ch1h = coin.get("ch1h", 0)
            ch4h = coin.get("ch4h", 0)
            ch24h = coin.get("ch24h", 0)
            ch7d = coin.get("ch7d", 0)
            vol_chg = coin.get("vol_chg", 0)
            price = coin["price"]

            # Hızlı ön eleme
            if price <= 0 or price > 2.0:
                continue
            if ch1h >= 6 or ch1h < -3:
                continue
            if ch4h < -8:
                continue
            if ch24h >= 100:
                continue
            if ch7d >= 300:
                continue
            if vol_chg < 20 and ch1h < 1.5:
                continue

            tech = get_technical_data(symbol)
            time.sleep(0.3)

            is_scam, reason = scam_check(symbol, price, ch1h, ch24h, vol_chg, tech)
            if is_scam:
                continue
            if not tech:
                continue

            orderbook = get_orderbook(symbol)
            time.sleep(0.2)

            result = score_coin(
                symbol, coin["name"], price,
                ch1h, ch4h, ch24h, ch7d,
                vol_chg, coin.get("mcap", 0), coin.get("cmc_rank", 9999),
                tech, fg, orderbook,
            )

            if result:
                # Yaş kontrolü
                age = get_coin_age_days(symbol, coin.get("date_added"))
                if age is not None and age < 30:
                    print(f"  ⏭️ {symbol} elendi — {age} gün önce listelendi")
                    continue

                if result["conviction"] in ["CRITICAL", "HIGH"] and result["score"] >= 14:
                    scored.append(result)
                    ob_log = f" | OB:{orderbook['bid_ask_ratio']}x" if orderbook else ""
                    print(f"  🎯 {symbol} | {result['conviction']} | Score:{result['score']} "
                          f"| {result['layer']} | RSI:{result['rsi']} | OBV:{result['obv_trend']}{ob_log}")
                elif result["score"] >= WATCHLIST_MIN_SCORE:
                    # Sinyal eşiğini geçemedi ama izlemeye değer (MEDIUM)
                    watchlist_update(
                        symbol, price, result.get("rsi"), result.get("obv_trend"),
                        result.get("vol_chg", 0), result["score"],
                        source=coin.get("sources", ["CMC"])[0] if coin.get("sources") else "CMC",
                    )

        except Exception:
            continue

    # ── SEÇİM: SADECE CRITICAL + BİRİKİM adayları ──────────
    # Diğerleri de kaydedilir ama bot alım yapmaz
    buy_candidates = [
        s for s in scored
        if s["conviction"] == "CRITICAL" and s["layer"] == "BIRIKIM"
    ]

    best_buy = None
    if buy_candidates:
        best_buy = max(buy_candidates, key=lambda x: x["score"])
        if len(buy_candidates) > 1:
            print(f"  ⚖️ {len(buy_candidates)} CRITICAL+BİRİKİM aday — "
                  f"sadece {best_buy['symbol']} (score:{best_buy['score']}) alım yapıyor")

    # Sinyal kayıt — scored zaten CRITICAL/HIGH + score>=14 olarak filtrelendi
    top = sorted(scored, key=lambda x: -x["score"])[:5]

    print(f"\n📋 {len(scored)} aday → {len(top)} sinyal kaydedilecek")

    signals_found = 0
    for s in top:
        allow = (best_buy is not None and s["symbol"] == best_buy["symbol"])
        if save_signal(s, fg, allow_buy=allow):
            signals_found += 1
        time.sleep(0.5)

    return signals_found

# ============================================================
# ANA DÖNGÜ
# ============================================================

def main():
    print("🚀 Atlas Kripto Kartal Gözü — V12 Savaş Mimarisi")
    print("📌 Trailing Stop: %8→breakeven→%4→%6 | Min tutma:4s | Max pozisyon:3+2 | Rotasyon:YOK")
    print(f"⏰ {datetime.now(timezone.utc).strftime('%H:%M UTC')}")

    if not CMC_API_KEY:
        print("❌ CMC_API_KEY bulunamadı!")
        return

    # Tablo kontrol
    for tbl in ["crypto_signals", "demo_trades", "demo_portfolios", "profiles"]:
        try:
            supabase.table(tbl).select("id").limit(1).execute()
            print(f"✅ {tbl} hazır")
        except Exception as e:
            print(f"⚠️ {tbl}: {e}")

    # Pozisyon izleme thread
    monitor = threading.Thread(target=position_monitor_loop, daemon=True)
    monitor.start()
    print("👁️ Pozisyon izleme başlatıldı (60s aralık)")

    scan_count = 0
    while True:
        try:
            found = scan_once(scan_count)
            scan_count += 1
            print(f"\n✅ Tarama #{scan_count} bitti. {found} sinyal. 15 dk bekleniyor...")
            time.sleep(900)
        except Exception as e:
            print(f"❌ Ana döngü: {e}")
            time.sleep(120)


if __name__ == "__main__":
    main()
