# -*- coding: utf-8 -*-
"""
ATLAS KRİPTO KARTAL GÖZÜ v4
================================
HAVUZ 1 — ANLIK SİNYAL: Günde 1-5 coin, hepsi kazanacak
HAVUZ 2 — İZLEME: Yeni/genç coinler, aylar boyu takip, olgunlaşınca sinyal

VERİ KAYNAKLARI (hepsi ücretsiz, API key yok):
- MEXC  : api.mexc.com/api/v3/ticker/24hr + klines + depth
- Gate.io: api.gateio.ws/api/v4/spot/tickers + candlesticks
- Binance: api1-4.binance.com/api/v3/klines
- CMC    : listings/latest (Basic plan)
- Fear & Greed: alternative.me

SCAM FİLTRESİ:
- Sahte hacim tespiti (wash trading)
- Bid-ask spread kontrolü
- OBV vs hacim çelişkisi
- Likidite kontrolü
- Geç giriş koruması

V5: Sinyal sonuç takibi (24s/72s/7g) + geçmiş başarı oranı
V6: Order book duvar analizi
V8: Periyodik istatistik raporu (en iyi/zayıf paternler)
"""

import os
import json
import time
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

# Hafızada cache
signal_cache = {}       # Son 2 saatte sinyal verilen coinler
watchlist_cache = {}    # İzleme havuzu (sembol → ilk görülme zamanı + veriler)
balance_lock = threading.Lock()   # ← YENİ: bakiye okuma/yazma her zaman atomik

try:
    if FIREBASE_SERVICE_ACCOUNT and not firebase_admin._apps:
        cred = credentials.Certificate(json.loads(FIREBASE_SERVICE_ACCOUNT))
        firebase_admin.initialize_app(cred)
        print("✅ Firebase Admin başlatıldı")
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
# PUSH BİLDİRİM
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
                        notification=messaging.AndroidNotification(channel_id="atlas_signals"),
                    ),
                    apns=messaging.APNSConfig(
                        payload=messaging.APNSPayload(aps=messaging.Aps(sound="default", badge=1))
                    ),
                    token=token,
                )
                messaging.send(msg)
            except:
                pass
        print(f"📱 Push gönderildi: {len(tokens)} kullanıcı")
    except Exception as e:
        print(f"❌ Push: {e}")


# ============================================================
# VERİ KAYNAKLARI — 3 BORSA
# ============================================================

def get_mexc_tickers():
    """MEXC tüm spot pariteler — API key yok"""
    try:
        r = requests.get(
            "https://api.mexc.com/api/v3/ticker/24hr",
            timeout=15
        )
        if r.status_code == 200:
            data = r.json()
            print(f"  → MEXC: {len(data)} parite")
            return data
    except Exception as e:
        print(f"⚠️ MEXC ticker: {e}")
    return []


def get_gateio_tickers():
    """Gate.io tüm spot pariteler — API key yok"""
    try:
        r = requests.get(
            "https://api.gateio.ws/api/v4/spot/tickers",
            timeout=15
        )
        if r.status_code == 200:
            data = r.json()
            print(f"  → Gate.io: {len(data)} parite")
            return data
    except Exception as e:
        print(f"⚠️ Gate.io ticker: {e}")
    return []


def get_cmc_coins():
    """CMC Basic plan — aux yok, price_min/max yok"""
    try:
        all_coins = {}

        r1 = requests.get(
            "https://pro-api.coinmarketcap.com/v1/cryptocurrency/listings/latest",
            headers=CMC_HEADERS,
            params={"limit": 500, "convert": "USD", "sort": "volume_24h", "sort_dir": "desc"},
            timeout=30
        )
        if r1.status_code == 200:
            for c in r1.json().get("data", []):
                all_coins[c["id"]] = c
            print(f"  → CMC hacim: {len(all_coins)} coin")
        else:
            print(f"⚠️ CMC-1: {r1.status_code}")

        time.sleep(2)

        r2 = requests.get(
            "https://pro-api.coinmarketcap.com/v1/cryptocurrency/listings/latest",
            headers=CMC_HEADERS,
            params={"limit": 500, "convert": "USD", "sort": "percent_change_1h", "sort_dir": "desc"},
            timeout=30
        )
        if r2.status_code == 200:
            for c in r2.json().get("data", []):
                if c["id"] not in all_coins:
                    all_coins[c["id"]] = c
            print(f"  → CMC momentum: toplam {len(all_coins)} coin")
        else:
            print(f"⚠️ CMC-2: {r2.status_code}")

        return list(all_coins.values())
    except Exception as e:
        print(f"❌ CMC: {e}")
        return []


def get_fear_greed():
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=5)
        d = r.json()["data"][0]
        return {"value": int(d["value"]), "label": d["value_classification"]}
    except:
        return None


# ============================================================
# TEKNİK ANALİZ — MEXC → Gate.io → Binance
# ============================================================

def get_klines(symbol, interval="4h", limit=100):
    """3 borsadan sırayla dene — en geniş kapsam"""
    sym_usdt = symbol.upper() + "USDT" if not symbol.upper().endswith("USDT") else symbol.upper()
    sym_gate = symbol.upper() + "_USDT"

    # 1. MEXC
    try:
        r = requests.get(
            "https://api.mexc.com/api/v3/klines",
            params={"symbol": sym_usdt, "interval": interval, "limit": limit},
            timeout=5
        )
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list) and len(data) > 10:
                return data, "MEXC"
    except:
        pass

    # 2. Gate.io
    try:
        interval_map = {"1h": "3600", "4h": "14400", "15m": "900", "1d": "86400"}
        gate_interval = interval_map.get(interval, "14400")
        r = requests.get(
            "https://api.gateio.ws/api/v4/spot/candlesticks",
            params={"currency_pair": sym_gate, "interval": gate_interval, "limit": limit},
            timeout=5
        )
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list) and len(data) > 10:
                converted = [
                    [int(c[0])*1000, c[5], c[3], c[4], c[2], c[1]]
                    for c in data
                ]
                return converted, "Gate.io"
    except:
        pass

    # 3. Binance
    for base_url in BINANCE_URLS:
        try:
            r = requests.get(
                f"{base_url}/api/v3/klines",
                params={"symbol": sym_usdt, "interval": interval, "limit": limit},
                timeout=5
            )
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list) and len(data) > 10:
                    return data, "Binance"
        except:
            continue

    return None, None


def get_orderbook_signal(symbol):
    sym_usdt = symbol.upper() + "USDT"
    try:
        r = requests.get(
            "https://api.mexc.com/api/v3/depth",
            params={"symbol": sym_usdt, "limit": 100},
            timeout=5
        )
        if r.status_code != 200:
            return None
        data = r.json()
        bids = data.get("bids", [])
        asks = data.get("asks", [])
        if not bids or not asks:
            return None

        mid_price = (float(bids[0][0]) + float(asks[0][0])) / 2

        bid_wall = sum(float(p) * float(q) for p, q in bids if float(p) >= mid_price * 0.98)
        ask_wall = sum(float(p) * float(q) for p, q in asks if float(p) <= mid_price * 1.02)

        ratio = bid_wall / ask_wall if ask_wall > 0 else 0

        return {
            "bid_wall_usd": round(bid_wall, 2),
            "ask_wall_usd": round(ask_wall, 2),
            "bid_ask_ratio": round(ratio, 2),
        }
    except:
        return None


def calculate_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
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
        if closes[i] > closes[i-1]:
            obv += volumes[i]
        elif closes[i] < closes[i-1]:
            obv -= volumes[i]
        obv_list.append(obv)
    trend = "up" if obv_list[-1] > obv_list[-10] else "down"
    # OBV vs fiyat diverjansı
    price_trend = "up" if closes[-1] > closes[-10] else "down"
    divergence = (trend == "up" and price_trend == "down")  # Bullish diverjans = BİRİKİM
    return trend, divergence


def get_technical_data(symbol):
    """RSI, OBV, ATR, 4s/1s trend. Binance yoksa MEXC veya Gate.io'dan."""
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

        # 4s değişim
        ch4h = ((closes[-1] - closes[-2]) / closes[-2]) * 100 if closes[-2] > 0 else 0

        # Hacim trend — son 5 mum vs önceki 5 mum
        vol_recent = sum(volumes[-5:]) / 5
        vol_prev = sum(volumes[-10:-5]) / 5
        vol_surge = vol_recent / vol_prev if vol_prev > 0 else 1

        # ATR(14)
        atrs = []
        for i in range(1, min(15, len(closes))):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i-1]),
                abs(lows[i] - closes[i-1])
            )
            atrs.append(tr)
        atr = sum(atrs) / len(atrs) if atrs else 0

        # SAHTE HACİM TESPİTİ
        # Hacim yüksek ama fiyat hiç oynamamış = wash trading şüphesi
        avg_candle_body = sum(abs(float(k[4]) - float(k[1])) for k in k4h[-20:]) / 20
        last_body = abs(closes[-1] - float(k4h[-1][1]))
        suspicious_volume = (vol_surge > 5 and last_body < avg_candle_body * 0.3)

        return {
            "rsi": rsi,
            "obv_trend": obv_trend,
            "obv_divergence": obv_divergence,  # True = bullish diverjans = birikim
            "ch4h": round(ch4h, 2),
            "vol_surge_4h": round(vol_surge, 2),
            "atr": atr,
            "exchange": exchange,
            "suspicious_volume": suspicious_volume,
            "price": closes[-1],
        }
    except:
        return None


# ============================================================
# SCAM / SAHTE HACİM FİLTRESİ
# ============================================================

def scam_check(symbol, price, ch1h, ch24h, vol_chg, tech):
    """
    Scam ve sahte hacim tespiti.
    True döndürürse = ŞÜPHELİ, sinyal verme.
    """
    # 1. Fiyat çok düşük = çöp coin riski
    if price < 0.000001:
        return True, "Fiyat çok düşük"

    # 2. 24 saatte %300+ = pump & dump döngüsünde
    if ch24h >= 300:
        return True, "24s %300+ pump & dump"

    # 3. 1 saatte %50+ = zaten geç
    if ch1h >= 50:
        return True, "1s %50+ geç giriş"

    # 4. Teknik veri yoksa = borsada yok = likit değil
    if not tech:
        return True, "Borsa bulunamadı / likit değil"

    # 5. Wash trading şüphesi
    if tech.get("suspicious_volume"):
        return True, "Sahte hacim şüphesi (wash trading)"

    # 6. OBV düşüyor ama fiyat ve hacim yükseliyor = dağıtım
    if tech.get("obv_trend") == "down" and ch1h > 10 and vol_chg > 200:
        return True, "OBV düşüyor — dağıtım (insiderlar satıyor)"

    # 7. 4 saatte sert düşüş
    if tech.get("ch4h", 0) < -10:
        return True, "4s %10+ düşüş — trend kötü"
    if tech:
        rsi = tech.get("rsi")
        obv = tech.get("obv_trend")
        if rsi and rsi > 80 and obv == "down":
            return True, "RSI 80+ OBV düşüyor — dağıtım"

    return False, ""


# ============================================================
# V5: SİNYAL SONUÇ TAKİBİ
# ============================================================

def check_signal_outcomes():
    """24s/72s/7g sonra sinyal sonuçlarını ölç"""
    try:
        now = datetime.now(timezone.utc)
        for hours, field in [(24, "24h"), (72, "72h"), (24*7, "7d")]:
            cutoff = (now - timedelta(hours=hours)).isoformat()
            rows = supabase.table("signal_outcomes") \
                .select("*") \
                .eq(f"checked_{field}", False) \
                .lte("created_at", cutoff) \
                .limit(20).execute()
            if not rows.data:
                continue
            print(f"📈 {len(rows.data)} sinyal için {field} sonuç ölçülüyor...")
            for row in rows.data:
                try:
                    k, _ = get_klines(row["symbol"], "1h", 2)
                    if not k:
                        supabase.table("signal_outcomes").update({
                            f"checked_{field}": True
                        }).eq("id", row["id"]).execute()
                        continue
                    current = float(k[-1][4])
                    entry = row["entry_price"]
                    pct = ((current - entry) / entry) * 100 if entry > 0 else 0
                    supabase.table("signal_outcomes").update({
                        f"price_{field}": current,
                        f"pct_{field}": round(pct, 2),
                        f"checked_{field}": True
                    }).eq("id", row["id"]).execute()
                except:
                    continue
                time.sleep(0.3)
    except Exception as e:
        print(f"❌ Outcome check: {e}")


def get_historical_success_rate(rsi, obv_trend, layer):
    """
    Geçmiş sinyallerden, benzer RSI/OBV/layer kombinasyonunun
    24s içinde kâr getirme oranını döndürür.
    Yetersiz veri (min 5 örnek) varsa None döner.
    """
    try:
        if rsi is None:
            rsi_min, rsi_max = 0, 100
        elif rsi < 30:
            rsi_min, rsi_max = 0, 30
        elif rsi < 50:
            rsi_min, rsi_max = 30, 50
        elif rsi < 70:
            rsi_min, rsi_max = 50, 70
        else:
            rsi_min, rsi_max = 70, 100

        rows = supabase.table("signal_outcomes") \
            .select("pct_24h") \
            .eq("layer", layer) \
            .eq("entry_obv", obv_trend) \
            .gte("entry_rsi", rsi_min) \
            .lt("entry_rsi", rsi_max) \
            .eq("checked_24h", True) \
            .not_.is_("pct_24h", "null") \
            .execute()

        if not rows.data or len(rows.data) < 5:
            return None

        wins = sum(1 for r in rows.data if r["pct_24h"] and r["pct_24h"] > 2)
        total = len(rows.data)
        return {"rate": round(wins / total * 100, 1), "sample": total}
    except Exception as e:
        print(f"⚠️ Success rate: {e}")
        return None

# ============================================================
# V9: PROJE YAŞ KONTROLÜ (CMC → CoinGecko fallback)
# ============================================================

_coingecko_list_cache = {"data": None, "ts": 0}


def get_coingecko_id(symbol):
    """CoinGecko coin listesini 24s cache'le, sembolden id bul."""
    global _coingecko_list_cache
    now = time.time()
    if _coingecko_list_cache["data"] is None or now - _coingecko_list_cache["ts"] > 86400:
        try:
            r = requests.get("https://api.coingecko.com/api/v3/coins/list", timeout=15)
            if r.status_code == 200:
                _coingecko_list_cache["data"] = r.json()
                _coingecko_list_cache["ts"] = now
        except:
            return None
    if not _coingecko_list_cache["data"]:
        return None
    sym_lower = symbol.lower()
    matches = [c for c in _coingecko_list_cache["data"] if c["symbol"] == sym_lower]
    return matches[0]["id"] if matches else None


def get_coin_age_days(symbol, date_added=None):
    """
    1. CMC date_added varsa kullan.
    2. Yoksa CoinGecko genesis_date'i dene.
    3. İkisi de yoksa None — yaş bilinmiyor, kontrol atlanır.
    """
    if date_added:
        try:
            added = datetime.fromisoformat(date_added.replace("Z", "+00:00"))
            return (datetime.now(timezone.utc) - added).days
        except:
            pass

    try:
        cg_id = get_coingecko_id(symbol)
        if not cg_id:
            return None
        r = requests.get(
            f"https://api.coingecko.com/api/v3/coins/{cg_id}",
            params={"localization": "false", "tickers": "false",
                    "market_data": "false", "community_data": "false",
                    "developer_data": "false"},
            timeout=8
        )
        if r.status_code != 200:
            return None
        genesis = r.json().get("genesis_date")
        if genesis:
            added = datetime.fromisoformat(genesis).replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - added).days
    except:
        pass
    return None
# ============================================================
# V8: İSTATİSTİK RAPORU
# ============================================================

def generate_pattern_report():
    """
    signal_outcomes verisinden RSI bucket + OBV + layer kombinasyonlarının
    24s başarı oranlarını çıkarır. SADECE konsola loglar (uydurma yok,
    tamamen gerçek veri). Min 20 toplam örnek + min 5 örnek/grup gerekir.
    """
    try:
        rows = supabase.table("signal_outcomes") \
            .select("layer, entry_rsi, entry_obv, pct_24h") \
            .eq("checked_24h", True) \
            .not_.is_("pct_24h", "null") \
            .execute()
        if not rows.data or len(rows.data) < 20:
            print(f"📊 V8: Yeterli veri yok ({len(rows.data) if rows.data else 0}/20)")
            return

        groups = defaultdict(list)
        for r in rows.data:
            rsi = r.get("entry_rsi")
            if rsi is None:
                bucket = "RSI:?"
            elif rsi < 30:
                bucket = "RSI<30"
            elif rsi < 50:
                bucket = "RSI30-50"
            elif rsi < 70:
                bucket = "RSI50-70"
            else:
                bucket = "RSI70+"
            key = f"{r['layer']} | {bucket} | OBV:{r['entry_obv']}"
            groups[key].append(r["pct_24h"])

        results = []
        for key, vals in groups.items():
            if len(vals) < 5:
                continue
            win_rate = sum(1 for v in vals if v > 2) / len(vals) * 100
            avg = sum(vals) / len(vals)
            results.append({"key": key, "win_rate": round(win_rate, 1), "avg": round(avg, 2), "n": len(vals)})

        if not results:
            print("📊 V8: Hiçbir grup min 5 örneğe ulaşmadı")
            return

        results.sort(key=lambda x: x["win_rate"], reverse=True)
        best = results[0]
        worst = results[-1]

        print(f"📊 V8 EN İYİ PATERN: {best['key']} → %{best['win_rate']} başarı (ort %{best['avg']}, n={best['n']})")
        if worst["key"] != best["key"]:
            print(f"📊 V8 EN ZAYIF PATERN: {worst['key']} → %{worst['win_rate']} başarı (ort %{worst['avg']}, n={worst['n']})")
    except Exception as e:
        print(f"❌ V8 pattern: {e}")


# ============================================================
# PUANLAMA SİSTEMİ
# ============================================================

def score_coin(symbol, name, price, ch1h, ch4h, ch24h, ch7d,
               vol_chg, mcap, cmc_rank, tech, fg, orderbook=None):
    """
    Tüm sinyalleri birleştir. Minimum 5 bağımsız sinyal gerekli.
    """
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

    # OBV aşağı ise BİRİKİM olamaz
    if obv_trend == "down":
        layer = "MOMENTUM"

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
        reasons.append("⚠️ OBV düşüyor — zayıf rally")

    # ── HACİM DEĞİŞİMİ ───────────────────────────────────────
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

    # 4s hacim artışı (Binance/MEXC/Gate.io'dan)
    if vol_surge >= 3 and ch1h < 5:
        score += 3
        if layer != "BIRIKIM" and obv_trend != "down":
            layer = "BIRIKIM"
        reasons.append(f"🐋 4s hacim {vol_surge:.1f}x — sessiz birikim")
    elif vol_surge >= 2:
        score += 1

    # Kataliz zorunlu
    if vol_chg < 30 and (rsi is None or rsi > 55) and obv_trend != "up":
        return None

    # ── FİYAT MOMENTUM (ERKEN YAKALAMA) ──────────────────────
    # Fiyat henüz az hareket etmişken (≤%3) bonus — "trene binmek" istemiyoruz.
    # %6+ hareket etmiş coinler için ceza — zaten geç.
    if 0 <= ch1h <= 3:
        score += 2
        reasons.append(f"%{ch1h:.1f} — erken aşama, henüz oynamamış")
    elif ch1h <= 6:
        score += 1
        reasons.append(f"%{ch1h:.1f} — hafif hareket başladı")
    elif ch1h > 6:
        score -= 2
        reasons.append(f"⚠️ %{ch1h:.1f} — zaten hareket etmiş, geç giriş riski")

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

    # ── V6: ORDER BOOK ───────────────────────────────────────
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
            reasons.append(f"🧱 Satış duvarı tespit edildi — bid/ask {ratio}x")

    # ── V5: GEÇMİŞ BAŞARI ORANI ──────────────────────────────
    hist = get_historical_success_rate(rsi, obv_trend, layer)
    if hist:
        if hist["rate"] >= 70:
            score += 5
            reasons.append(f"📊 Geçmiş başarı: %{hist['rate']} ({hist['sample']} sinyal)")
        elif hist["rate"] <= 30:
            return None  # Kanıtlanmış kötü patern — sinyal verme
        elif hist["rate"] <= 50:
            score -= 3
            reasons.append(f"⚠️ Geçmiş başarı zayıf: %{hist['rate']} ({hist['sample']} sinyal)")

    if score < 6:
        return None

    # Conviction eşikleri — sert filtre
    if score >= 18:
        conviction = "CRITICAL"
    elif score >= 13:
        conviction = "HIGH"
    elif score >= 8:
        conviction = "MEDIUM"
    else:
        return None

    return {
        "symbol": symbol, "name": name, "price": price,
        "ch1h": ch1h, "ch4h": ch4h, "ch24h": ch24h, "ch7d": ch7d,
        "vol_chg": vol_chg, "mcap": mcap, "cmc_rank": cmc_rank,
        "rsi": rsi, "obv_trend": obv_trend, "obv_div": obv_div,
        "atr": tech.get("atr", 0) if tech else 0,
        "exchange": tech.get("exchange", "?") if tech else "?",
        "orderbook": orderbook,
        "conviction": conviction, "reasons": reasons,
        "score": score, "layer": layer,
    }

# ============================================================
# HAVUZ 2 — İZLEME SİSTEMİ
# ============================================================

def watchlist_update(symbol, price, ch1h, ch24h, vol_chg, tech, source):
    """
    Yeni veya genç coinleri izleme havuzuna ekle.
    Scam değilse ve potansiyel varsa izle.
    """
    try:
        # Scam kontrolü
        is_scam, reason = scam_check(symbol, price, ch1h, ch24h, vol_chg, tech)
        if is_scam:
            return

        # Minimum koşullar — izlemeye değer mi?
        if price <= 0 or price > 10:
            return
        if ch24h < -20:  # %20'den fazla düşmüş = zayıf
            return

        # Supabase'de var mı?
        existing = supabase.table("crypto_watchlist") \
            .select("id, observation_count, first_seen, last_score") \
            .eq("symbol", symbol).limit(1).execute()

        rsi = tech.get("rsi") if tech else None
        obv = tech.get("obv_trend") if tech else None

        # İlk kez görülüyorsa ekle
        if not existing.data:
            supabase.table("crypto_watchlist").insert({
                "symbol": symbol,
                "source": source,
                "first_seen": datetime.now(timezone.utc).isoformat(),
                "last_seen": datetime.now(timezone.utc).isoformat(),
                "first_price": price,
                "last_price": price,
                "observation_count": 1,
                "last_rsi": rsi,
                "last_obv": obv,
                "last_vol_chg": vol_chg,
                "last_score": 0,
                "status": "watching",
            }).execute()
            print(f"  👁️ İZLEMEYE ALINDI: {symbol} @ ${price:.6f} [{source}]")
        else:
            # Güncelle
            row = existing.data[0]
            supabase.table("crypto_watchlist").update({
                "last_seen": datetime.now(timezone.utc).isoformat(),
                "last_price": price,
                "observation_count": row["observation_count"] + 1,
                "last_rsi": rsi,
                "last_obv": obv,
                "last_vol_chg": vol_chg,
            }).eq("id", row["id"]).execute()

    except Exception as e:
        print(f"⚠️ Watchlist: {e}")


def watchlist_check_signals(fg):
    """
    İzleme havuzundaki coinleri kontrol et.
    Olgunlaşmış olanları sinyal havuzuna taşı.
    """
    try:
        # En az 3 gözlem yapılmış coinler
        candidates = supabase.table("crypto_watchlist") \
            .select("*") \
            .eq("status", "watching") \
            .gte("observation_count", 3) \
            .execute()

        if not candidates.data:
            return []

        print(f"👁️ İzleme havuzu: {len(candidates.data)} coin kontrol ediliyor...")
        signals = []

        for row in candidates.data:
            symbol = row["symbol"]
            try:
                # Güncel teknik veri
                tech = get_technical_data(symbol)
                if not tech:
                    continue

                price = tech["price"]
                ch1h = 0
                ch24h = row.get("last_vol_chg", 0)
                vol_chg = row.get("last_vol_chg", 0)
                rsi = tech.get("rsi")
                obv_trend = tech.get("obv_trend")
                obv_div = tech.get("obv_divergence", False)

                # İzleme sinyali koşulları — daha sıkı
                ready = False
                trigger = ""

                if rsi and rsi < 30 and obv_trend == "up":
                    ready = True
                    trigger = f"RSI {rsi} aşırı satım + OBV yükseliyor"
                elif obv_div and vol_chg > 100:
                    ready = True
                    trigger = "OBV bullish diverjans + hacim artışı"
                elif rsi and rsi < 35 and obv_div:
                    ready = True
                    trigger = "RSI düşük + OBV bullish diverjans"

                if ready:
                    # Gözlem süresi
                    first_seen = datetime.fromisoformat(
                        row["first_seen"].replace("Z", "+00:00")
                    )
                    days_watched = (datetime.now(timezone.utc) - first_seen).days

                    orderbook = get_orderbook_signal(symbol)

                    signals.append({
                        "symbol": symbol,
                        "name": symbol,
                        "price": price,
                        "ch1h": ch1h,
                        "ch4h": tech.get("ch4h", 0),
                        "ch24h": ch24h,
                        "ch7d": 0,
                        "vol_chg": vol_chg,
                        "mcap": 0,
                        "cmc_rank": 9999,
                        "rsi": rsi,
                        "obv_trend": obv_trend,
                        "obv_div": obv_div,
                        "atr": tech.get("atr", 0),
                        "exchange": tech.get("exchange", "?"),
                        "orderbook": orderbook,
                        "conviction": "HIGH",
                        "reasons": [
                            f"👁️ {days_watched} gün izlendi",
                            f"🎯 Tetikleyici: {trigger}",
                        ],
                        "score": 15,
                        "layer": "WATCHLIST",
                        "from_watchlist": True,
                    })

                    # Durumu güncelle
                    supabase.table("crypto_watchlist").update({
                        "status": "signaled",
                        "signal_date": datetime.now(timezone.utc).isoformat(),
                    }).eq("id", row["id"]).execute()

                time.sleep(0.3)

            except:
                continue

        return signals

    except Exception as e:
        print(f"❌ Watchlist check: {e}")
        return []


# ============================================================
# VERİ BİRLEŞTİRME — MEXC + Gate.io → Ortak Format
# ============================================================

def merge_exchange_data(mexc_tickers, gateio_tickers, cmc_coins):
    merged = {}

    # CMC verisi — en güvenilir fiyat/hacim
    for c in cmc_coins:
        q = c.get("quote", {}).get("USD", {})
        symbol = c.get("symbol", "")
        price = float(q.get("price", 0) or 0)
        if price <= 0 or price > 2.0:
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
            "date_added": c.get("date_added"),   # ← YENİ SATIR
            "sources": ["CMC"],
        }

    # MEXC verisi — CMC'de olmayan coinler
    for t in mexc_tickers:
        sym = t.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        base = sym[:-4]  # BTCUSDT → BTC
        price = float(t.get("lastPrice", 0) or 0)
        if price <= 0 or price > 2.0:
            continue
        vol_usdt = float(t.get("quoteVolume", 0) or 0)
        if vol_usdt < 100_000:
            continue
        ch24h = float(t.get("priceChangePercent", 0) or 0)

        if base in merged:
            merged[base]["sources"].append("MEXC")
        else:
            merged[base] = {
                "symbol": base,
                "name": base,
                "price": price,
                "ch1h": 0,
                "ch4h": 0,
                "ch24h": ch24h,
                "ch7d": 0,
                "vol_chg": 0,
                "mcap": 0,
                "cmc_rank": 9999,
                "sources": ["MEXC"],
            }

    # Gate.io verisi
    for t in gateio_tickers:
        pair = t.get("currency_pair", "")
        if not pair.endswith("_USDT"):
            continue
        base = pair[:-5]  # BTC_USDT → BTC
        price = float(t.get("last", 0) or 0)
        if price <= 0 or price > 2.0:
            continue
        vol_usdt = float(t.get("quote_volume", 0) or 0)
        if vol_usdt < 100_000:
            continue
        ch24h = float(t.get("change_percentage", 0) or 0)

        if base in merged:
            merged[base]["sources"].append("Gate.io")
        else:
            merged[base] = {
                "symbol": base,
                "name": base,
                "price": price,
                "ch1h": 0,
                "ch4h": 0,
                "ch24h": ch24h,
                "ch7d": 0,
                "vol_chg": 0,
                "mcap": 0,
                "cmc_rank": 9999,
                "sources": ["Gate.io"],
            }

    print(f"  → Birleşik evren: {len(merged)} coin")
    return list(merged.values())


# ============================================================
# AI ANALİZ
# ============================================================

def get_ai_explanation(s, fg):
    try:
        price = s["price"]
        atr = s.get("atr", 0)

        if price < 0.0001:
            ps = f"${price:.8f}"
        elif price < 0.01:
            ps = f"${price:.6f}"
        elif price < 1:
            ps = f"${price:.4f}"
        else:
            ps = f"${price:.2f}"

        stop = price * 0.92 if atr == 0 else price - atr * 1.5
        target = price * 1.20 if atr == 0 else price + atr * 3
        fg_str = f"{fg['value']} ({fg['label']})" if fg else "Veri yok"

        layer_ctx = {
            "BIRIKIM": "BİRİKİM: Whale sessizce giriyor, fiyat henüz oynamamış.",
            "MOMENTUM": "MOMENTUM: Fiyat ve hacim birlikte yükseliyor.",
            "WATCHLIST": "İZLEME: Uzun süredir takip ediliyordu, şimdi sinyal verdi.",
        }.get(s.get("layer", "MOMENTUM"), "")

        # V5: Geçmiş başarı oranı — veri yoksa AI'ya "yetersiz veri" olarak bildirilir
        hist = get_historical_success_rate(s.get("rsi"), s.get("obv_trend"), s.get("layer"))
        hist_str = f"Geçmiş başarı oranı: %{hist['rate']} ({hist['sample']} sinyal)" if hist else "Geçmiş veri henüz yetersiz (bu konudan bahsetme)"

        # V6: Order book — veri yoksa belirtilir
        ob = s.get("orderbook")
        if ob:
            ob_str = f"Order book: bid/ask oranı {ob['bid_ask_ratio']}x, alım duvarı ${ob['bid_wall_usd']/1000:.0f}K, satış duvarı ${ob['ask_wall_usd']/1000:.0f}K"
        else:
            ob_str = "Order book verisi yok (bu konudan bahsetme)"

        # RSI/OBV None ise AI'ya net şekilde bildir
        rsi_str = f"{s['rsi']}" if s['rsi'] is not None else "VERİ YOK"
        obv_str = s['obv_trend'] if s['obv_trend'] is not None else "VERİ YOK"

        prompt = f"""Profesyonel kripto analistisin. Türkçe. Kısa ve net.

KESİN KURAL: SADECE aşağıda verilen sayısal/metinsel verilere dayanarak yorum yap.
Bir veri "VERİ YOK" veya "bahsetme" notuyla işaretliyse, o konuda HİÇBİR yorum yapma,
varsayımda bulunma, tahmin etme veya genel/klişe cümle kurma. Sadece elindeki
gerçek sayılarla konuş.

{s['symbol']} | {ps} | 1s:%{s['ch1h']:.1f} | 4s:%{s['ch4h']:.1f} | 24s:%{s['ch24h']:.1f}
Hacim:%{s['vol_chg']:.0f} | RSI:{rsi_str} | OBV:{obv_str}
Güven:{s['conviction']} | Katman:{s.get('layer','?')} | Borsa:{s.get('exchange','?')}
Korku/Açgözlülük:{fg_str}
Stop:{stop:.8f} | Hedef:{target:.8f}
{layer_ctx}
{hist_str}
{ob_str}
Tespit edilen sinyaller: {' | '.join(s['reasons']) if s['reasons'] else 'Yok'}

===ACEMİ===
[Maks 2 cümle. Sadece yukarıdaki gerçek verilere göre neden hareket ediyor, ne yapmalı. P&D riskini belirt.]
===USTA===
[Maks 3 cümle. Sadece mevcut RSI/OBV/order book verilerine göre yorum, kritik seviye, hedef.]
===PRO===
[Maks 4 cümle. Sadece mevcut verilere göre whale/order book güveni(1-10), P&D riski(1-10), risk/ödül, giriş/stop/hedef $.]"""

        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"},
            json={
                "model": "llama-3.1-8b-instant",
                "messages": [
                    {"role": "system", "content": "Kripto analistisin. SADECE sana verilen sayısal/metinsel verilere dayan. 'VERİ YOK' veya 'bahsetme' işaretli hiçbir konuda yorum yapma, varsayım yapma, genel/uydurma cümle kurma. Formatı değiştirme. P&D riskini belirt. Türkçe."},
                    {"role": "user", "content": prompt}
                ],
                "max_tokens": 600, "temperature": 0.2
            },
            timeout=15
        )
        resp = r.json()
        return resp["choices"][0]["message"]["content"] if "choices" in resp else ""
    except:
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
    except:
        pass
    return a, u, p


# ============================================================
# SİNYAL KAYDET & GÖNDER
# ============================================================

def save_and_push_signal(s, fg):
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
        le = "🐋" if s["layer"] == "BIRIKIM" else "👁️" if s["layer"] == "WATCHLIST" else "📈"

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
            "created_at": datetime.now(timezone.utc).isoformat()
        }).execute()

        signal_cache[s["symbol"]] = time.time()
        sid = res.data[0].get("id") if res.data else None

        # V5: Sinyal sonuç takibi
        try:
            supabase.table("signal_outcomes").insert({
                "signal_id": sid,
                "symbol": s["symbol"],
                "layer": s["layer"],
                "conviction": s["conviction"],
                "entry_rsi": s.get("rsi"),
                "entry_obv": s.get("obv_trend"),
                "entry_price": price,
            }).execute()
        except Exception as e:
            print(f"⚠️ Outcome kayıt: {e}")

        # Bot işle
        crypto_bot_process(
            s["symbol"], price, s["conviction"],
            s["ch1h"], s["vol_chg"], s["layer"], sid,
            s.get("rsi"), s.get("obv_trend"),
            s.get("atr", 0), s.get("reasons"), s.get("orderbook")
        )

        send_push(
            title=f"{emoji} {s['symbol']} — {s['conviction']}",
            body=f"{ps} | RSI:{s['rsi']} | {le}{s['layer']}",
            signal_id=sid
        )

        print(f"✅ SİNYAL: {desc}")
        return True

    except Exception as e:
        print(f"❌ {s.get('symbol','?')}: {e}")
        return False


# ============================================================
# KRİPTO BOT
# ============================================================

def crypto_bot_should_buy(conviction, ch1h, vol_chg, layer):
    if ch1h < 0:
        return False
    if conviction == "CRITICAL":
        return True
    if conviction == "HIGH" and layer in ["BIRIKIM", "WATCHLIST"]:
        return True
    if conviction == "HIGH" and ch1h >= 5 and vol_chg >= 200:
        return True
    if conviction == "MEDIUM" and layer == "BIRIKIM" and vol_chg >= 500:
        return True
    return False


def crypto_bot_process(symbol, price, conviction, ch1h, vol_chg, layer, signal_id, rsi=None, obv=None, atr=0, reasons=None, orderbook=None):
    try:
        if not crypto_bot_should_buy(conviction, ch1h, vol_chg, layer):
            return
        print(f"🤖 Kripto Bot {symbol}: AL")
        portfolios = supabase.table("demo_portfolios") \
            .select("user_id, crypto_balance").execute()
        if not portfolios.data:
            return
        for p in portfolios.data:
            user_id = p["user_id"]
            balance = p.get("crypto_balance", 0) or 0
            if balance < 5:
                continue
            profile = supabase.table("profiles").select("is_pro") \
                .eq("id", user_id).limit(1).execute()
            is_pro = profile.data[0].get("is_pro", False) if profile.data else False
            _crypto_bot_buy(user_id, symbol, price, signal_id, is_pro, balance, conviction, layer, rsi, obv, atr, reasons, orderbook)
            time.sleep(0.2)
    except Exception as e:
        print(f"❌ Kripto bot: {e}")


def _crypto_bot_buy(user_id, symbol, price, signal_id, is_pro, balance, conviction, layer="MOMENTUM", rsi=None, obv=None, atr=0, reasons=None, orderbook=None):
    try:
        if not is_pro:
            month_start = datetime.now(timezone.utc).replace(
                day=1, hour=0, minute=0, second=0).isoformat()
            mt = supabase.table("demo_trades").select("id") \
                .eq("user_id", user_id).eq("market", "CRYPTO") \
                .gte("created_at", month_start).execute()
            if len(mt.data) >= 3:
                return
            op = supabase.table("demo_trades").select("id") \
                .eq("user_id", user_id).eq("market", "CRYPTO") \
                .eq("status", "open").execute()
            if len(op.data) >= 3:
                return

        pct = 0.20 if conviction == "CRITICAL" else 0.15 if conviction == "HIGH" else 0.10

        if atr and atr > 0:
            stop_price = round(price - atr * 1.5, 10)
            target_price = round(price + atr * 3, 10)
        else:
            stop_price = round(price * 0.92, 10)
            target_price = round(price * 1.20, 10)

        entry_reason = " | ".join(reasons) if reasons else None
        entry_ob_ratio = orderbook.get("bid_ask_ratio") if orderbook else None

        with balance_lock:
            # Bakiyeyi tekrar oku — check_positions arada kapatmış olabilir
            fresh = supabase.table("demo_portfolios").select("crypto_balance") \
                .eq("user_id", user_id).limit(1).execute()
            current_balance = fresh.data[0]["crypto_balance"] if fresh.data else balance
            invest = min(current_balance * pct, 50)
            if invest < 5:
                return

            supabase.table("demo_trades").insert({
                "user_id": user_id, "symbol": symbol, "market": "CRYPTO",
                "signal_id": signal_id, "buy_price": price,
                "buy_date": datetime.now(timezone.utc).isoformat(),
                "quantity": round(invest / price, 6), "status": "open",
                "signal_layer": layer,
                "entry_rsi": rsi,
                "entry_obv": obv,
                "entry_conviction": conviction,
                "entry_atr": atr,
                "entry_ob_ratio": entry_ob_ratio,
                "stop_price": stop_price,
                "target_price": target_price,
                "current_price": price,
                "peak_price": price,
                "entry_reason": entry_reason,
                "created_at": datetime.now(timezone.utc).isoformat()
            }).execute()

            supabase.table("demo_portfolios").update({
                "crypto_balance": round(current_balance - invest, 2)
            }).eq("user_id", user_id).execute()

        print(f"  ✅ Bot alım: {user_id} → {symbol} ${invest:.0f} | {layer} | Stop:{stop_price} Hedef:{target_price}")
    except Exception as e:
        print(f"❌ Bot buy: {e}")


def crypto_bot_check_positions():
    try:
        trades = supabase.table("demo_trades").select("*") \
            .eq("status", "open").eq("market", "CRYPTO").execute()
        if not trades.data:
            return
        print(f"🔍 {len(trades.data)} açık pozisyon kontrol...")
        for trade in trades.data:
            try:
                tech = get_technical_data(trade["symbol"])
                if not tech:
                    continue
                current = tech["price"]
                buy_price = trade["buy_price"]
                stop_price = trade.get("stop_price") or buy_price * 0.92
                target_price = trade.get("target_price") or buy_price * 1.20
                peak_price = max(trade.get("peak_price") or buy_price, current)
                change = ((current - buy_price) / buy_price) * 100

                # ── V11: DÖNÜŞ (REVERSAL) TESPİTİ ────────────────────
                reversal_reasons = []

                # 1. OBV dönüşü
                if trade.get("entry_obv") == "up" and tech.get("obv_trend") == "down":
                    reversal_reasons.append("OBV yön değiştirdi (birikim bitti)")

                # 2. RSI aşırı alımdan dönüş
                entry_rsi = trade.get("entry_rsi")
                cur_rsi = tech.get("rsi")
                if entry_rsi and cur_rsi and entry_rsi >= 70 and cur_rsi < 65:
                    reversal_reasons.append(f"RSI {entry_rsi}→{cur_rsi} (momentum tepe yaptı)")

                # 3. Order book tersine döndü
                ob = get_orderbook_signal(trade["symbol"])
                entry_ob = trade.get("entry_ob_ratio")
                if ob and entry_ob and entry_ob >= 1.5 and ob.get("bid_ask_ratio", 0) <= 0.7:
                    reversal_reasons.append(f"Order book tersine döndü ({entry_ob}x→{ob['bid_ask_ratio']}x)")

                # 4. Zirveden %5+ geri çekilme (kâr varken)
                if peak_price > buy_price:
                    drawdown = ((peak_price - current) / peak_price) * 100
                    if drawdown >= 5 and current > buy_price:
                        reversal_reasons.append(f"Zirveden %{drawdown:.1f} geri çekildi")

                # Trailing stop: hedefin yarısına ulaştıysa stop'u girişe çek
                halfway = buy_price + (target_price - buy_price) * 0.5
                new_stop = stop_price
                if current >= halfway and stop_price < buy_price:
                    new_stop = buy_price

                # Canlı veriyi güncelle
                supabase.table("demo_trades").update({
                    "current_price": current,
                    "peak_price": peak_price,
                    "stop_price": new_stop,
                }).eq("id", trade["id"]).execute()

                # ── SAT PUSH — reversal varsa, henüz kapatmadan uyar ──
                if reversal_reasons and not trade.get("sell_warning_sent"):
                    reason_str = " | ".join(reversal_reasons)
                    send_push(
                        title=f"⚠️ SAT — {trade['symbol']}",
                        body=f"%{change:+.1f} | {reason_str}",
                        signal_id=trade.get("signal_id")
                    )
                    supabase.table("demo_trades").update({
                        "sell_warning_sent": True,
                        "exit_reason": f"Erken uyarı: {reason_str}"
                    }).eq("id", trade["id"]).execute()
                    print(f"  ⚠️ SAT UYARISI: {trade['symbol']} — {reason_str}")

                # ── Hedef/Stop/Reversal+kâr → kapat ───────────────────
                should_close = current >= target_price or current <= new_stop or \
                    (reversal_reasons and current > buy_price)

                if should_close:
                    pl = (current - buy_price) * trade["quantity"]
                    if current >= target_price:
                        exit_reason = "Hedef Vuruldu (Take Profit)"
                    elif reversal_reasons and current > buy_price:
                        exit_reason = f"Dönüş tespit edildi, kârla kapatıldı: {' | '.join(reversal_reasons)}"
                    elif new_stop > buy_price:
                        exit_reason = "Trailing Stop — Kâr Korundu"
                    else:
                        exit_reason = "Stop Loss"

                    supabase.table("demo_trades").update({
                        "sell_price": current,
                        "sell_date": datetime.now(timezone.utc).isoformat(),
                        "status": "closed",
                        "profit_loss": round(pl, 2),
                        "exit_reason": exit_reason
                    }).eq("id", trade["id"]).execute()

                    with balance_lock:
                        port = supabase.table("demo_portfolios").select("crypto_balance") \
                            .eq("user_id", trade["user_id"]).limit(1).execute()
                        if port.data:
                            new_bal = port.data[0]["crypto_balance"] + (trade["quantity"] * current)
                            supabase.table("demo_portfolios").update({
                                "crypto_balance": round(new_bal, 2)
                            }).eq("user_id", trade["user_id"]).execute()

                    action = "💰 KAR" if pl > 0 else "🛑 STOP"
                    print(f"  {action}: {trade['symbol']} %{change:.1f} | ${pl:.2f} | {exit_reason}")

                    send_push(
                        title=f"{'💰' if pl>0 else '🛑'} KAPANDI — {trade['symbol']}",
                        body=f"%{change:+.1f} | ${pl:.2f} | {exit_reason}",
                        signal_id=trade.get("signal_id")
                    )
            except Exception as e:
                print(f"⚠️ {trade['symbol']} pozisyon kontrol hatası: {e}")
                continue
            time.sleep(0.3)
    except Exception as e:
        print(f"❌ Pozisyon kontrol: {e}")


def get_last_signal_time(symbol):
    try:
        r = supabase.table("crypto_signals").select("created_at") \
            .eq("symbol", symbol).order("created_at", ascending=False) \
            .limit(1).execute()
        if r.data:
            dt = datetime.fromisoformat(r.data[0]["created_at"].replace("Z", "+00:00"))
            return dt.timestamp()
        return 0
    except:
        return 0

# ============================================================
# V11: POZİSYON İZLEME — AYRI THREAD (10s)
# ============================================================

def position_monitor_loop():
    while True:
        try:
            crypto_bot_check_positions()
        except Exception as e:
            print(f"❌ Pozisyon izleme thread: {e}")
        time.sleep(10)

# ============================================================
# ANA TARAMA
# ============================================================

def scan_once(scan_count=0):
    print(f"\n🦅 KARTAL GÖZÜ v4 — {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}")
    print("=" * 55)

    # Pozisyon izleme ayrı thread'de (10s). Burada sadece sonuç takibi/rapor.
    if scan_count % 3 == 0:
        check_signal_outcomes()
        generate_pattern_report()


    fg = get_fear_greed()
    if fg:
        e = "😱" if fg["value"] <= 25 else "😐" if fg["value"] <= 50 else "😊" if fg["value"] <= 75 else "🤑"
        print(f"{e} Korku/Açgözlülük: {fg['value']} ({fg['label']})")

    # Veri topla
    print("📡 Veri toplanıyor...")
    mexc = get_mexc_tickers()
    time.sleep(1)
    gateio = get_gateio_tickers()
    time.sleep(1)
    cmc = get_cmc_coins()

    # Birleştir
    coins = merge_exchange_data(mexc, gateio, cmc)
    if not coins:
        print("⚠️ Veri alınamadı")
        return 0

    now = time.time()
    scored = []
    watchlist_candidates = []

    print(f"🔍 {len(coins)} coin analiz ediliyor...")

    for coin in coins:
        symbol = coin["symbol"]
        try:
            # Cache
            if symbol in signal_cache and now - signal_cache[symbol] < 7200:
                continue
            if now - get_last_signal_time(symbol) < 7200:
                signal_cache[symbol] = now
                continue

            ch1h = coin["ch1h"]
            ch4h = coin["ch4h"]
            ch24h = coin["ch24h"]
            ch7d = coin["ch7d"]
            vol_chg = coin["vol_chg"]
            price = coin["price"]

            # Hızlı ön eleme
            # Hızlı ön eleme
            if price <= 0 or price > 2.0:
                continue
            if ch1h >= 6 or ch1h < -3:  # %6+ zaten geç, atla — erken yakalama hedefi
                continue
            if ch4h < -8:
                continue
            if ch24h >= 100:
                continue
            if ch7d >= 300:
                continue
            if vol_chg < 20 and ch1h < 1.5:
                continue

            # Teknik analiz
            tech = get_technical_data(symbol)
            time.sleep(0.3)

            # Scam kontrolü
            is_scam, reason = scam_check(symbol, price, ch1h, ch24h, vol_chg, tech)
            if is_scam:
                continue

            # Teknik veri yoksa watchlist'e ekle, sinyal verme
            if not tech:
                watchlist_candidates.append((symbol, price, ch1h, ch24h, vol_chg, None, "No-tech"))
                continue

            # V6: Order book (sadece skoru sınırın yakınında olan adaylar için
            # ek API yükü olmasın diye, önce skoru hesapla, sonra gerekirse ekle)
            orderbook = get_orderbook_signal(symbol)
            time.sleep(0.2)

            # Puanla
            result = score_coin(
                symbol, coin["name"], price,
                ch1h, ch4h, ch24h, ch7d,
                vol_chg, coin["mcap"], coin["cmc_rank"],
                tech, fg, orderbook
            )

            if result:
                 # V9: Proje yaş kontrolü — sadece skorlamayı geçenler için
                age_days = get_coin_age_days(symbol, coin.get("date_added"))
                if age_days is not None and age_days < 30:
                    print(f"  ⏭️ {symbol} elendi — {age_days} gün önce listelendi (rug riski)")
                    continue
                scored.append(result)
                ob_log = f" | OB:{orderbook['bid_ask_ratio']}x" if orderbook else ""
                print(f"  🎯 {symbol} | {result['conviction']} | Score:{result['score']} | {result['layer']} | RSI:{result['rsi']} | OBV:{result['obv_trend']}{ob_log} | [{result['exchange']}]")
            else:
                # Sinyal eşiğini geçemedi ama izlemeye değer olabilir
                if vol_chg > 50 or (tech.get("rsi") and tech["rsi"] < 40):
                    watchlist_candidates.append((symbol, price, ch1h, ch24h, vol_chg, tech, coin.get("sources", ["?"])[0]))

        except:
            continue

    # İzleme havuzunu güncelle
    for args in watchlist_candidates:
        watchlist_update(*args)

    # İzleme havuzundan olgunlaşmış sinyaller
    watchlist_signals = watchlist_check_signals(fg)
    scored.extend(watchlist_signals)

    # Sırala: WATCHLIST > BİRİKİM > MOMENTUM, sonra score
    layer_order = {"WATCHLIST": 0, "BIRIKIM": 1, "MOMENTUM": 2}
    scored.sort(key=lambda x: (layer_order.get(x["layer"], 3), -x["score"]))

    # SERT ELEME — sayı sabit DEĞİL, kalite sabit.
    # MEDIUM tek başına asla yeterli değil. Doldurma yapılmaz, 0 sinyal de olabilir.
    top = [s for s in scored if s["conviction"] in ["CRITICAL", "HIGH"] and s["score"] >= 14]
    top = top[:5]  # üst sınır 5, alt sınır yok

    print(f"\n📋 {len(scored)} aday → {len(top)} sinyal seçildi")

    signals_found = 0
    for s in top:
        if save_and_push_signal(s, fg):
            signals_found += 1
        time.sleep(0.5)

    return signals_found


# ============================================================
# ANA DÖNGÜ
# ============================================================

def main():
    print("🚀 Atlas Kripto Kartal Gözü — v4 (V5+V6+V8+V11)")
    print("🎯 MEXC + Gate.io + Binance + CMC | RSI | OBV | Order Book | Watchlist | Scam Filter | Outcome Tracking")
    print(f"⏰ {datetime.now(timezone.utc).strftime('%H:%M UTC')}")

    if not CMC_API_KEY:
        print("❌ CMC_API_KEY bulunamadı!")
        return

    # Tabloları kontrol et
    try:
        supabase.table("crypto_watchlist").select("id").limit(1).execute()
        print("✅ crypto_watchlist tablosu hazır")
    except:
        print("⚠️ crypto_watchlist tablosu yok — Supabase'de oluştur!")

    try:
        supabase.table("crypto_signals").select("id").limit(1).execute()
        print("✅ crypto_signals tablosu hazır")
    except Exception as e:
        print(f"⚠️ Tablo: {e}")

    try:
        supabase.table("signal_outcomes").select("id").limit(1).execute()
        print("✅ signal_outcomes tablosu hazır")
    except Exception as e:
        print(f"⚠️ signal_outcomes tablosu yok: {e}")

    monitor_thread = threading.Thread(target=position_monitor_loop, daemon=True)
    monitor_thread.start()
    print("👁️ Pozisyon izleme thread'i başlatıldı (10s aralık)")

    scan_count = 0
    while True:
        try:
            found = scan_once(scan_count)
            scan_count += 1
            print(f"\n✅ Tarama #{scan_count} bitti. {found} sinyal. 15 dk bekleniyor...")
            time.sleep(900)
        except Exception as e:
            print(f"❌ Döngü: {e}")
            time.sleep(120)


if __name__ == "__main__":
    main()
