# -*- coding: utf-8 -*-
"""
ATLAS KRİPTO KARTAL GÖZÜ — V13 MOMENTUM MİMARİSİ
==================================================
Son güncelleme: 19 Haziran 2026

HEDEF: SYN gibi güçlü momentum coinleri erken yakala, yapışkan takip et.

2 KATMAN (BIRIKIM KALDIRILDI):
1. MOMENTUM — hacim spike + fiyat hareketi + çoklu borsa
2. SUREGEN  — 24s %8+ yürümüş, momentum devam ediyor

CONVICTION EŞİKLERİ:
- CRITICAL : score >= 16 → bot alır, push gönderilir
- HIGH     : score >= 11 → push gönderilir, signal_outcomes'a kaydedilir
- MEDIUM   : score >= 7  → sadece kaydedilir

BOT KURALLARI:
- Max 5 normal + 3 istisna = max 8 açık pozisyon
- Aynı sembolde açık pozisyon varsa tekrar alma
- VIX 25+ → alım durur (kripto)
- VIX 30+ → alım durur (tüm platformlar)

TRAILING STOP:
- kâr <%2      → stop = giriş - %8 (sabit)
- kâr %2-%5    → stop = giriş (breakeven)
- kâr %5-%8    → stop = zirveden -%8
- kâr >=%8     → stop = zirveden -%12 (18 Haz: %6→%12, SYN erken çıkış sonrası)
- Min tutma: 4 saat

VERİ KAYNAKLARI:
- MEXC, Gate.io, KuCoin, Huobi, Coinbase (teknik + çoklu borsa konfirmasyon)
- CMC (hacim/mcap/momentum)
- Fear & Greed (alternative.me)
- Order Book (MEXC depth)

MİMARİ NOTLAR:
- BIRIKIM (RSI<30 dip yakala) → %0 win rate → 18 Haziran 2026'da kaldırıldı
- MOMENTUM Hunter (momentum_hunter.py) ayrı servis olarak çalışıyor
- signal_outcomes tablosu: 24s/72s/7g sonuç takibi
- learning_weights: min 10 örnek + z>=1.65 ile aktifleşir

DEĞİŞİKLİK GÜNLÜĞÜ (19 Haziran 2026):
- DİP SIÇRAMASI CRITICAL İSTİSNASI EKLENDİ: READY vakası — RSI 16.6,
  ch1h +%3.3, hacim -%3 ile HIGH kaldı (score 11-15 aralığı), bot
  almadı. Coin sonraki 1 saatte +%24 yaptı. Artık RSI<20 VE ch1h>=3
  ikisi birden varsa HIGH→CRITICAL yükseltiliyor (hacim negatif olsa
  bile) — "tükenme" değil "dipten sıçrama başlangıcı" pattern'i.
- KRİTİK EKSİK GİDERİLDİ — PUSH YOKTU: save_signal() hiçbir zaman push
  göndermiyordu, push sadece bot_buy() içinde (CRITICAL + slot uygunsa)
  gönderiliyordu. READY HIGH kaldığı için ne bot aldı ne de kullanıcıya
  haber gitti — sistem doğru yakalamıştı ama kullanıcı görmedi. Artık
  HIGH ve CRITICAL sinyaller bot alsın almasın HER ZAMAN push ediliyor;
  asıl amaç botun her şeyi alması değil, kullanıcının güçlü sinyalleri
  görüp kendi kararını verebilmesi.
- ACİLİYET İŞARETLEMESİ EKLENDİ: CRITICAL sinyaller veya "dip sıçraması"
  pattern'i (READY tipi, hızlı 1-saatlik hareketler) artık hem push
  başlığında hem description'da "🔴 ACİL" öneki ile işaretleniyor, push
  metni kullanıcıyı doğrudan "CMC'de kontrol et" diye yönlendiriyor.
  Bot alamasa/almasa bile bu sinyaller kullanıcının gözünden kaçmasın
  diye description alanına da aynı işaret kaydediliyor (Flutter tarafı
  bu öneki kırmızı vurgu için kullanabilir, şema değişikliği gerekmedi).
- KRİTİK SLOT HESAPLAMA HATASI DÜZELTİLDİ: _execute_buy() istisna kontrolünde
  open_count (TOPLAM: normal+istisna) doğrudan max_normal (5) ile
  karşılaştırılıyordu. 4 normal+3 istisna=7 pozisyonu olan kullanıcıda
  7>=5 olduğu için yeni her coin istisna sayılıyor, istisna slotu da
  dolu (3/3) olunca HİÇ alım yapılamıyordu (VELVET/NOICE/MAGMA/HIGH/BLUAI
  kaçırıldı). normal_count = open_count - exceptional_count hesaplanıp
  ona göre kontrol ediliyor artık.
- trusted_coins/trusted_symbols filtresi tamamen kaldırıldı (sadece mcap+rank kontrolü kaldı)
- Fiyat üst limiti ($2.0) kaldırıldı — VELVET ($0.55), IMU gibi coinler artık taranıyor
- mcap alt limiti $5M → $1M düşürüldü
- ch1h ön eleme aralığı genişletildi: -3/+6 → -5/+15
- ch24h ön eleme: 200% → 500%
- SUREGEN eşiği: ch24h>=8 → ch24h>=5 (daha erken yakalama)
- Top-5 sinyal limiti kaldırıldı — tüm CRITICAL/HIGH sinyaller kaydediliyor
- Bot artık TÜM CRITICAL sinyalleri alıyor (önceden sadece en yüksek skorlu 1 tanesi)
- İstisnai pozisyon eşiği: score>=24 → score>=16, max 2→3 istisna
- Trailing stop genişletildi: %4/%6 → %8/%12 (SYN erken çıkış vakası sonrası)
- "ascending=" parametresi → "desc=" (Supabase kütüphane güncellemesi uyumu)
- V12 → V13 tüm referanslar güncellendi
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
ZOMBIE_HOLD_HOURS = 24        # Bu süre geçti + kâr yok + doğrulama yetersizse
                              # slot temizliği yapılır (kârda olan dokunulmaz)
CLOSE_SCORE_MIN = 5           # Doğrulama katmanı minimum puan
MIN_PROFIT_PCT = 0.03         # Doğrulama çıkışı için min %3 kâr
MAX_OPEN_FREE = 3             # Free kullanıcı max açık pozisyon
MAX_OPEN_PRO = 5              # Pro max normal pozisyon
MAX_OPEN_PRO_EXCEPTIONAL = 8  # Pro istisna ile max
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
LEARNING_MIN_SAMPLES = 10        # 10 örnek yeterli — hızlı öğren, yavaş unutma
LEARNING_MIN_ABS_Z = 1.65        # p<0.10 — daha duyarlı, erken uyarı
LEARNING_MAX_BONUS = 6           # ±6 puan — CRITICAL eşiğini (13) etkileyecek kadar güçlü
LEARNING_BASELINE_WINRATE = 0.50 # Beklenti: %50 win rate — altında ceza, üstünde ödül
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

def get_trusted_exchange_symbols():
    """
    Binance + Coinbase + KuCoin + Huobi'deki USDT çiftlerini çeker.
    Bu borsalarda listelenmiş = minimum kalite standardı geçilmiş demektir.
    Bir coin bu listede yoksa çöp coin riski yüksek.
    """
    trusted = set()

    # Binance
    for base_url in ["https://api1.binance.com", "https://api2.binance.com", "https://api.binance.com"]:
        try:
            r = requests.get(f"{base_url}/api/v3/exchangeInfo", timeout=10)
            if r.status_code == 200:
                symbols = r.json().get("symbols", [])
                for s in symbols:
                    if s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING":
                        trusted.add(s["baseAsset"])
                print(f"  → Binance (trusted): {len(trusted)} coin")
                break
        except Exception:
            continue

    # KuCoin
    try:
        r = requests.get("https://api.kucoin.com/api/v1/symbols", timeout=10)
        if r.status_code == 200:
            symbols = r.json().get("data", [])
            count = 0
            for s in symbols:
                if s.get("quoteCurrency") == "USDT" and s.get("enableTrading"):
                    trusted.add(s["baseCurrency"])
                    count += 1
            print(f"  → KuCoin: {count} USDT çifti")
    except Exception as e:
        print(f"⚠️ KuCoin: {e}")

    # Huobi (HTX)
    try:
        r = requests.get("https://api.huobi.pro/v1/common/symbols", timeout=10)
        if r.status_code == 200:
            symbols = r.json().get("data", [])
            count = 0
            for s in symbols:
                if s.get("quote-currency") == "usdt" and s.get("state") == "online":
                    trusted.add(s["base-currency"].upper())
                    count += 1
            print(f"  → Huobi: {count} USDT çifti")
    except Exception as e:
        print(f"⚠️ Huobi: {e}")

    # Coinbase
    try:
        r = requests.get("https://api.exchange.coinbase.com/products", timeout=10)
        if r.status_code == 200:
            products = r.json()
            count = 0
            for p in products:
                if p.get("quote_currency") == "USDT" and p.get("status") == "online":
                    trusted.add(p["base_currency"])
                    count += 1
            print(f"  → Coinbase: {count} USDT çifti")
    except Exception as e:
        print(f"⚠️ Coinbase: {e}")

    print(f"  → Güvenilir evren: {len(trusted)} benzersiz coin")
    return trusted


def get_coinbase_tickers():
    try:
        r = requests.get("https://api.exchange.coinbase.com/products", timeout=15)
        if r.status_code == 200:
            products = r.json()
            usdt = [p for p in products if p.get("quote_currency") in ("USDT", "USD") and p.get("status") == "online"]
            print(f"  → Coinbase: {len(usdt)} parite")
            return usdt
    except Exception as e:
        print(f"⚠️ Coinbase: {e}")
    return []


def get_binance_tickers():
    """Binance ticker - Railway'de engellenmiş olabilir, hızlı dene geç."""
    urls = [
        "https://api1.binance.com/api/v3/ticker/24hr",
        "https://api2.binance.com/api/v3/ticker/24hr",
        "https://data-api.binance.vision/api/v3/ticker/24hr",
    ]
    for url in urls:
        try:
            r = requests.get(url, headers=HEADERS, timeout=5)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list) and len(data) > 100:
                    domain = url.split("/")[2]
                    print(f"  → Binance: {len(data)} parite ({domain})")
                    return data
        except Exception:
            continue
    # Sessizce geç — KuCoin zaten Binance'i kapsıyor
    return []


def get_kucoin_tickers():
    try:
        r = requests.get("https://api.kucoin.com/api/v1/market/allTickers", timeout=15)
        if r.status_code == 200:
            data = r.json().get("data", {}).get("ticker", [])
            print(f"  → KuCoin: {len(data)} parite")
            return data
    except Exception as e:
        print(f"⚠️ KuCoin ticker: {e}")
    return []


def get_huobi_tickers():
    try:
        r = requests.get("https://api.huobi.pro/market/tickers", timeout=15)
        if r.status_code == 200:
            data = r.json().get("data", [])
            print(f"  → Huobi: {len(data)} parite")
            return data
    except Exception as e:
        print(f"⚠️ Huobi ticker: {e}")
    return []


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
    - Kâr %8 - %20    → stop = zirveden %8 geri çekilme
    - Kâr >= %20      → stop = zirveden %12 geri çekilme (SYN gibi güçlü
                         trendlerde erken çıkışı önler — büyük harekete
                         nefes alanı tanır, 18 Haz 2026 SYN örneğinden
                         sonra %6'dan %12'ye genişletildi)

    Döner: (stop_price, etiket)
    """
    profit_pct = (peak - buy_price) / buy_price

    if profit_pct < TRAIL_ACTIVATE_PCT:
        return buy_price * (1 - STOP_PCT), "sabit_stop"
    elif profit_pct < TRAIL_4_PCT:
        return buy_price, "breakeven"
    elif profit_pct < TRAIL_6_PCT:
        return peak * (1 - 0.08), "trail_8pct"
    else:
        return peak * (1 - 0.12), "trail_12pct"


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
            "trail_8pct": "Trailing stop (zirveden %8) vuruldu",
            "trail_12pct": "Trailing stop (zirveden %12) vuruldu",
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

    profit_pct = (current - buy_price) / buy_price
    obv_reversed = (trade.get("entry_obv") == "up" and tech.get("obv_trend") == "down")

    # VETO: 4 saat dolmadan doğrulama katmanı çıkışı yok — TEK İSTİSNA:
    # zarar >= %5 VE giriş anındaki "birikim" trendi tersine döndüyse
    # (OBV up→down), bu "gürültü" değil "bozulma" sinyali — erken
    # doğrulamaya izin ver. İki şart birden gerektiği için rastlantısal
    # tetiklenme riski düşük.
    if hold_hours < MIN_HOLD_HOURS:
        early_exit_ok = profit_pct <= -0.05 and obv_reversed
        if not early_exit_ok:
            return 0, [f"VETO: {hold_hours:.1f}s/{MIN_HOLD_HOURS}s tutma — sadece stop aktif"], True, trailing_stop
        reasons.append(f"⏱️ Erken doğrulama: zarar %{profit_pct*100:.1f} + OBV dönüşü ({hold_hours:.1f}s/{MIN_HOLD_HOURS}s)")

    # ── KAYNAK 1: OBV dönüşü ────────────────────────────────
    if obv_reversed:
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

    # ── ZOMBİ TEMİZLİĞİ ──────────────────────────────────────
    # 24+ saat açık, hâlâ KÂRDA DEĞİL (current <= buy_price) ve
    # doğrulama eşiğine ulaşmamış pozisyonlar slot işgal ediyor.
    # Kârda olan pozisyonlara DOKUNULMAZ — trailing stop onları
    # zaten koruyor, "patlama" potansiyeli kesilmez.
    if hold_hours >= ZOMBIE_HOLD_HOURS and profit_pct <= 0 and score < CLOSE_SCORE_MIN:
        return 50, [f"⏰ {hold_hours:.1f}s açık, kâr yok (%{profit_pct*100:.1f}) — "
                     f"slot temizliği"], False, trailing_stop

    return score, reasons, False, trailing_stop

# ============================================================
# SCAM FİLTRESİ
# ============================================================

def scam_check(symbol, price, ch1h, ch24h, vol_chg, tech):
    if price < 0.000001:
        return True, "Fiyat çok düşük"
    if ch24h >= 500:
        return True, "24s %500+ pump & dump"
    if ch1h >= 15:
        return True, "1s %15+ zaten geç"
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
               vol_chg, mcap, cmc_rank, tech, fg, orderbook=None,
               suregen_candidate=False, binance_vol=0, on_coinbase=False,
               in_trusted_db=False):
    # ============================================================
    # 📌 TODO (SUREGEN İZLEME — 15 Haz 2026'da eklendi):
    # "SUREGEN" layer'ı: 24s'te %8+ yürümüş ama anlık duraklamış
    # coinleri artık eleme yerine score_coin'e gönderiyor (RSI/OBV
    # kararı veriyor). Amaç: erken-orta vadeli istikrarlı momentum
    # coinlerini de yakalamak.
    #
    # ~2-3 HAFTA SONRA (veya learning_weights'te
    # "SUREGEN|...|..." satırları n>=30 olduğunda) KONTROL ET:
    #   SELECT * FROM learning_weights WHERE layer = 'SUREGEN';
    # - win_rate nasıl? Diğer layer'lardan (BIRIKIM/MOMENTUM) iyi/kötü mü?
    # - MEDIUM eşiği (score>=9) SUREGEN için çok mu sıkı? (PIEVERSE
    #   gibi score=8 ile elenenler çoğunluksa, eşiği 8'e düşürmeyi
    #   düşün — ama önce gerçek win_rate verisine bak.)
    # - Debug logları (🔬 SUREGEN ADAY/SONUÇ) artık gerekirse kaldırılabilir.
    # ============================================================
    score = 0
    reasons = []
    layer = "MOMENTUM"

    rsi = tech.get("rsi") if tech else None
    obv_trend = tech.get("obv_trend") if tech else None
    obv_div = tech.get("obv_divergence") if tech else False
    vol_surge = tech.get("vol_surge_4h", 1) if tech else 1

    # ── DOĞRULAMA MODU: çapraz-kaynak fiyat tutarlılığı ──────────
    # `price` (CMC/ticker kaynaklı) ile `tech["price"]` (klines'tan
    # gelen gerçek son kapanış — alımın gerçekleşeceği kaynak) arasında
    # >%50 fark varsa, bu coin için elimizdeki veri güvenilmez —
    # muhtemelen sembol çakışması (aynı ticker, farklı proje) veya
    # stale veri. Sinyal/alım üretme, sessizce ele.
    tech_price = tech.get("price") if tech else None
    if tech_price and price > 0:
        price_ratio = max(price, tech_price) / min(price, tech_price)
        if price_ratio > 1.5:
            print(f"  🚫 FİYAT TUTARSIZLIĞI: {symbol} elendi "
                  f"(ticker:${price} vs klines:${tech_price}, "
                  f"oran:{price_ratio:.1f}x)")
            return None

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
            # BIRIKIM için aşırı alım cezası — MOMENTUM için değil
            # SYN gibi coinler RSI 75+ iken %100+ yapabiliyor
            if not suregen_candidate:
                score -= 1  # Eski -3 yerine -1, çok sert cezalandırma
            reasons.append(f"⚠️ RSI {rsi} — Yüksek, dikkat")
        elif rsi > 60:
            score += 1

    # ── OBV ──────────────────────────────────────────────────
    if obv_trend == "up" and ch1h > 0:
        score += 3
        reasons.append("📈 OBV yükseliyor — alım baskısı")
    elif obv_trend == "down" and ch1h > 3:
        score -= 2

    layer = "MOMENTUM"  # Artık tek layer MOMENTUM

    # ── HACİM ────────────────────────────────────────────────
    if vol_chg >= 1000:
        score += 7
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

    if vol_surge >= 3:
        score += 3
        reasons.append(f"🐋 4s hacim {vol_surge:.1f}x — güçlü momentum")
    elif vol_surge >= 2:
        score += 1

    # Kataliz zorunlu — hiçbir şey yoksa eleme
    # İSTİSNA: "Süregen momentum" — coin son 24s'te zaten %8+ yürümüş.
    # Bu durumda elemiyoruz, RSI/OBV'nin "devam eder mi tükendi mi"
    # kararına bırakıyoruz. Layer'ı SUREGEN olarak işaretleyip
    # öğrenme sisteminin bu pattern'i ayrı takip etmesini sağlıyoruz.
    no_catalyst = vol_chg < 30 and (rsi is None or rsi > 55) and obv_trend != "up"
    if no_catalyst:
        if ch24h >= 30 and ch1h > 0:
            # BSB yapısı: 24s güçlü yürüyüş + 1s hâlâ pozitif = momentum devam ediyor
            layer = "SUREGEN"
            score += 3  # güçlü momentum bonusu
            reasons.append(f"🚀 Süregen güç — 24s %{ch24h:.1f}, 1s %{ch1h:.1f} devam ediyor")
            print(f"  🔬 SUREGEN score_coin'e girdi: {symbol} | rsi:{rsi} "
                  f"obv:{obv_trend} ch24h:{ch24h:.2f} (skor hesabı sürüyor...)")
        elif ch24h >= 8:
            layer = "SUREGEN"
            reasons.append(f"⏩ Süregen momentum — 24s %{ch24h:.1f}, anlık duraklama")
            print(f"  🔬 SUREGEN score_coin'e girdi: {symbol} | rsi:{rsi} "
                  f"obv:{obv_trend} ch24h:{ch24h:.2f} (skor hesabı sürüyor...)")
        else:
            return None

    # ── FİYAT HAREKET (İĞNE DELİĞİ — yukarı kırılma teyidi) ──
    # Amaç: "henüz oynamamış" (ch1h≈0) veya "düşüyor" (ch1h<0) coinleri
    # değil, "şimdi yukarı kırılmaya BAŞLAMIŞ" (ch1h pozitif ve makul)
    # coinleri öne çıkarmak. Aşırı pump (>6%) trene atlama riski.
    if ch1h < 0:
        score -= 3
        reasons.append(f"%{ch1h:.1f} — düşüş sürüyor, teyit yok")
    elif ch1h == 0:
        reasons.append(f"%{ch1h:.1f} — durağan, henüz kırılım yok")
    elif ch1h <= 1.5:
        score += 1
        reasons.append(f"%{ch1h:.1f} — hafif yukarı kırılım başladı")
    elif ch1h <= 4:
        score += 3
        reasons.append(f"%{ch1h:.1f} — yukarı kırılım teyitli")
    elif ch1h <= 8:
        score += 4
        reasons.append(f"%{ch1h:.1f} — güçlü momentum")
    elif ch1h <= 15:
        score += 2
        reasons.append(f"%{ch1h:.1f} — hızlı hareket, dikkat")

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
    # LVVA, MAGMA, PLAY, ROUTE, VELVET gibi düşük mcap coinlerde
    # RSI<30 "alıcı yok, coin ölüyor" anlamına geliyor — sahte dip.
    # BAY, READY, SWEAT, GAIN gibi $1M+ coinlerde gerçek birikim.
    # BIRIKIM + RSI<30 kombinasyonu için minimum $500K market cap şartı.
    if layer == "BIRIKIM" and rsi is not None and rsi < 30:
        if mcap < 5_000_000:
            reasons.append(f"🚫 BIRIKIM RSI<30 + mcap ${mcap/1e3:.0f}K — sahte dip (çöp coin), elendi")
            return None
        if cmc_rank > 2000:
            reasons.append(f"🚫 BIRIKIM RSI<30 + CMC rank #{cmc_rank} — bilinmez coin, elendi")
            return None
        # Hacim yetersizse BIRIKIM sinyali güvenilmez — OXT gibi sahte dip
        if vol_chg < 100:
            reasons.append(f"🚫 BIRIKIM RSI<30 + hacim yetersiz (%{vol_chg:.0f}) — sahte dip, elendi")
            return None

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

    # ── KURUMSAL BORSA BONUSU ────────────────────────────────
    # Veri kanıtlı: Binance hacim patlaması = büyük para girişi
    # Coinbase listeli = kurumsal onay (BlackRock, Fidelity vs)
    if on_coinbase:
        score += 2
        reasons.append("🏦 Coinbase listeli — kurumsal onaylı")

    if in_trusted_db:
        score += 1
        reasons.append("⭐ Güvenilir coin listesinde")

    if binance_vol and binance_vol > 5_000_000:  # $5M+ Binance hacmi
        score += 3
        reasons.append(f"🏛️ Binance hacmi ${binance_vol/1e6:.1f}M — kurumsal akış")
    elif binance_vol and binance_vol > 1_000_000:
        score += 1
        reasons.append(f"🏛️ Binance hacmi ${binance_vol/1e6:.1f}M")


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
        if layer == "SUREGEN":
            print(f"  🔬 SUREGEN SONUÇ: {symbol} → ELENDİ (score={score} < 6)")
        return None
    learning_bonus = get_learning_bonus(layer, rsi, obv_trend, score=score)
    if learning_bonus != 0:
        score += learning_bonus
        reasons.append(f"🧠 Öğrenme katsayısı: {learning_bonus:+d}")

    # ── VERİ KANITLI CRITICAL GEÇİŞ KURALI ──────────────────
    # 409 gerçek trade analizi sonucu:
    # BIRIKIM + RSI<30  → %27 win rate, ort +3.3% (KULLAN)
    # BIRIKIM + RSI30-50 → %13 win rate, ort +0.1% (HAYIR)
    # Artık sadece MOMENTUM ve SUREGEN — BIRIKIM yok
    # MOMENTUM/SUREGEN score 16+ = CRITICAL
    if score >= 16:
        conviction = "CRITICAL"
        if layer == "SUREGEN":
            reasons.append("🚀 SUREGEN CRITICAL — momentum gücü doğrulandı")
        else:
            reasons.append("⚡ MOMENTUM CRITICAL — güçlü trend yakalandı")
    elif score >= 11:
        conviction = "HIGH"
        # 19 Haz 2026 — DİP SIÇRAMASI İSTİSNASI: RSI çok düşükken (<20)
        # + fiyat zaten pozitif yöne dönmüşse (ch1h>=3), bu "tükenme"
        # değil "dipten sıçrama başlangıcı" olabilir. Hacim henüz negatif
        # olsa bile (henüz yeni başlamış, hacim verisi gecikmeli gelir).
        # READY örneği: RSI 16.6, ch1h +3.3, vol -3% iken HIGH kaldı,
        # bot almadı, coin sonraki 1 saatte +%24 yaptı. Bu pattern'i
        # CRITICAL'a yükselt — sıkı şart: RSI<20 VE ch1h>=3 ikisi birden.
        if rsi is not None and rsi < 20 and ch1h >= 3:
            conviction = "CRITICAL"
            reasons.append(f"🎯 DİP SIÇRAMASI: RSI {rsi} + ch1h %{ch1h:.1f} — CRITICAL'a yükseltildi")
    elif score >= 7:
        conviction = "MEDIUM"
    else:
        if layer == "SUREGEN":
            print(f"  🔬 SUREGEN SONUÇ: {symbol} → ELENDİ (score={score} < 7)")
        return None

    if layer == "SUREGEN":
        print(f"  🔬 SUREGEN SONUÇ: {symbol} → SİNYAL! score={score} conviction={conviction}")

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


def record_signal_outcome(signal_id, symbol, layer, conviction, rsi, obv_trend, entry_price, score=None):
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
            "entry_score": score,
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

                    # Borsa çapraz-kontaminasyon koruması: 24s içinde
                    # |%95|'i aşan bir değişim, gerçek bir hareket değil,
                    # farklı bir borsadan/pariteden yanlış fiyat okunduğu
                    # anlamına gelir (örn. "U" sembolü stablecoin ile
                    # karışmış, fiyat $0.0003 → $1.00 gibi). Bu durumda
                    # kaydı "checked" yapma, bir sonraki taramada tekrar
                    # denensin — istatistiği bozmasın.
                    if abs(pct) > 95:
                        print(f"⚠️ {row['symbol']} {field} sonucu şüpheli "
                              f"(%{pct:.1f}, {entry}→{current}) — atlanıyor")
                        continue

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
    KALE MİMARİSİ — 4 boyutlu pattern öğrenmesi:
    layer + RSI bucket + OBV + skor bandı
    
    Kurallar:
    - Min 10 örnek (hızlı öğrenme)
    - z >= 1.65 (p<0.10, erken uyarı)
    - Kaybeden pattern için ceza 2x hızlı birikir
    - Kazanan pattern için ödül 1x (asimetrik — kayıptan kaç)
    - CRITICAL eşiği artık 13 (eski 10) — learning bunu zorlamaz
      ama bonus sistemi aracılığıyla etkiler
    """
    try:
        rows = supabase.table("signal_outcomes") \
            .select("layer, entry_rsi, entry_obv, entry_score, pct_24h") \
            .eq("checked_24h", True) \
            .not_.is_("pct_24h", "null") \
            .execute()

        if not rows.data or len(rows.data) < LEARNING_MIN_SAMPLES:
            return

        def score_band(s):
            """Skor aralığı — ne kadar güçlü sinyal olduğunu ayırt eder."""
            try:
                s = int(float(s)) if s is not None else 0
            except Exception:
                s = 0
            if s >= 16: return "ULTRA"
            if s >= 13: return "HIGH"
            if s >= 10: return "MID"
            return "LOW"

        groups = defaultdict(list)
        for r in rows.data:
            bucket = rsi_bucket(r.get("entry_rsi"))
            band = score_band(r.get("entry_score"))
            key = (
                r.get("layer") or "?",
                bucket,
                r.get("entry_obv") or "?",
                band,
            )
            groups[key].append(r["pct_24h"])

        updated = 0
        for (layer, bucket, obv, band), pcts in groups.items():
            n = len(pcts)
            if n < LEARNING_MIN_SAMPLES:
                continue

            wins = sum(1 for p in pcts if p > 2)
            win_rate = wins / n

            p0 = LEARNING_BASELINE_WINRATE
            se = math.sqrt(p0 * (1 - p0) / n)
            z = (win_rate - p0) / se if se > 0 else 0

            if abs(z) < LEARNING_MIN_ABS_Z:
                continue

            # Asimetrik ceza: kaybeden pattern için 2x hızlı öğren
            # Kazananda 1x, kaybedende 2x — "önce zarar durdur"
            loss_multiplier = 2.0 if z < 0 else 1.0
            magnitude = min(abs(z) / 3.0, 1.0) * LEARNING_MAX_BONUS * loss_multiplier
            magnitude = min(magnitude, LEARNING_MAX_BONUS)  # cap
            bonus = round(magnitude) if z > 0 else -round(magnitude)
            bonus = max(-LEARNING_MAX_BONUS, min(LEARNING_MAX_BONUS, bonus))

            if bonus == 0:
                continue

            key_str = f"{layer}|{bucket}|{obv}|{band}"
            supabase.table("learning_weights").upsert({
                "pattern_key": key_str,
                "layer": layer,
                "rsi_bucket": bucket,
                "obv_trend": obv,
                "sample_size": n,
                "win_rate": round(win_rate * 100, 1),
                "z_score": round(z, 2),
                "bonus": bonus,
                "market": "CRYPTO",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }, on_conflict="pattern_key").execute()

            updated += 1
            print(f"  🧠 ÖĞRENME: {key_str} → bonus:{bonus:+d} "
                  f"(n={n}, win_rate=%{win_rate*100:.1f}, z={z:.2f})")

        if updated:
            print(f"  🧠 {updated} patern güncellendi")

    except Exception as e:
        print(f"❌ update_learning_weights: {e}")


_learning_cache = {"data": {}, "ts": 0}


def get_learning_bonus(layer, rsi, obv_trend, score=None):
    """
    4 boyutlu pattern key ile learning_weights'ten bonus okur.
    Önce tam eşleşme (layer|bucket|obv|band) dener,
    yoksa 3 boyutlu (layer|bucket|obv) fallback yapar.
    10 dakika cache'lenir.
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

    # Skor bandı
    def _band(s):
        try:
            s = int(float(s)) if s is not None else 0
        except Exception:
            s = 0
        if s >= 16: return "ULTRA"
        if s >= 13: return "HIGH"
        if s >= 10: return "MID"
        return "LOW"

    band = _band(score)

    # Önce 4 boyutlu tam eşleşme
    key4 = f"{layer}|{bucket}|{obv_trend}|{band}"
    if key4 in _learning_cache["data"]:
        return _learning_cache["data"][key4]

    # Fallback: 3 boyutlu (eski format geriye dönük uyumluluk)
    key3 = f"{layer}|{bucket}|{obv_trend}"
    return _learning_cache["data"].get(key3, 0)


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

        # Symbol unique constraint nedeniyle, herhangi bir status'ta
        # önceden kayıtlı mı kontrol et (örn. eski 'signaled' kaydı)
        any_existing = supabase.table("crypto_watchlist") \
            .select("id").eq("symbol", symbol).limit(1).execute()

        if any_existing.data:
            # Var olan kaydı 'watching' olarak yeniden aktifleştir
            supabase.table("crypto_watchlist").update({
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
                "signal_date": None,
            }).eq("id", any_existing.data[0]["id"]).execute()
            print(f"  👁️ İZLEMEYE ALINDI: {symbol} (skor:{score}) @ ${price} (yeniden aktif)")
            return

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



def merge_exchange_data(mexc_tickers, gateio_tickers, cmc_coins, binance_tickers=None, kucoin_tickers=None, huobi_tickers=None, coinbase_tickers=None):
    binance_tickers = binance_tickers or []
    kucoin_tickers = kucoin_tickers or []
    huobi_tickers = huobi_tickers or []
    coinbase_tickers = coinbase_tickers or []
    merged = {}

    for c in cmc_coins:
        q = c.get("quote", {}).get("USD", {})
        symbol = c.get("symbol", "")
        price = float(q.get("price", 0) or 0)
        if price <= 0:
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
        if price <= 0:
            continue
        if is_stablecoin(base, price, 0, float(t.get("priceChangePercent", 0) or 0)):
            continue
        if float(t.get("quoteVolume", 0) or 0) < 100_000:
            continue
        if base in merged:
            # SEMBOL ÇAKIŞMASI KORUMASI: CMC'de bu sembolle eşleşen coin
            # zaten varsa, MEXC'deki "aynı sembollü" parite GERÇEKTE
            # tamamen farklı bir proje olabilir (kısa ticker'lar birden
            # fazla coin tarafından paylaşılır — örn. CMC'deki BTX/BeatSwap
            # $0.016 iken MEXC'deki BTXUSDT $0.19 olabilir, alakasız
            # coinler). Fiyatlar arasında ciddi (>%50) sapma varsa bu
            # coin GÜVENİLMEZ — sources MISMATCH ile işaretlenir ve
            # score_coin'de sert filtre uygular.
            cmc_price = merged[base]["price"]
            if cmc_price > 0:
                ratio = max(price, cmc_price) / min(price, cmc_price)
                if ratio > 1.5:
                    merged[base]["sources"].append("MEXC_MISMATCH")
                    merged[base]["mismatch_ratio"] = round(ratio, 1)
                    continue
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
        if price <= 0:
            continue
        if is_stablecoin(base, price, 0, float(t.get("change_percentage", 0) or 0)):
            continue
        if float(t.get("quote_volume", 0) or 0) < 100_000:
            continue
        if base in merged:
            cmc_price = merged[base]["price"]
            if cmc_price > 0:
                ratio = max(price, cmc_price) / min(price, cmc_price)
                if ratio > 1.5:
                    merged[base]["sources"].append("GATEIO_MISMATCH")
                    merged[base]["mismatch_ratio"] = round(ratio, 1)
                    continue
            merged[base]["sources"].append("Gate.io")
        else:
            merged[base] = {
                "symbol": base, "name": base, "price": price,
                "ch1h": 0, "ch4h": 0,
                "ch24h": float(t.get("change_percentage", 0) or 0),
                "ch7d": 0, "vol_chg": 0, "mcap": 0, "cmc_rank": 9999,
                "sources": ["Gate.io"],
            }

    # ── BİNANCE ──────────────────────────────────────────────
    for t in binance_tickers:
        sym = t.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        base = sym[:-4]
        price = float(t.get("lastPrice", 0) or 0)
        if price <= 0:
            continue
        if is_stablecoin(base, price, 0, float(t.get("priceChangePercent", 0) or 0)):
            continue
        if float(t.get("quoteVolume", 0) or 0) < 100_000:
            continue
        if base in merged:
            cmc_price = merged[base]["price"]
            if cmc_price > 0:
                ratio = max(price, cmc_price) / min(price, cmc_price)
                if ratio > 1.5:
                    merged[base]["sources"].append("BINANCE_MISMATCH")
                    continue
            merged[base]["sources"].append("Binance")
            # Binance'den gelen hacim verisi daha güvenilir — güncelle
            merged[base]["binance_vol"] = float(t.get("quoteVolume", 0) or 0)
        else:
            merged[base] = {
                "symbol": base, "name": base, "price": price,
                "ch1h": 0, "ch4h": 0,
                "ch24h": float(t.get("priceChangePercent", 0) or 0),
                "ch7d": 0, "vol_chg": 0, "mcap": 0, "cmc_rank": 9999,
                "sources": ["Binance"],
            }

    # ── KUCOIN ───────────────────────────────────────────────
    for t in kucoin_tickers:
        sym = t.get("symbol", "")
        if not sym.endswith("-USDT"):
            continue
        base = sym[:-5]
        price = float(t.get("last", 0) or 0)
        if price <= 0:
            continue
        if is_stablecoin(base, price, 0, float(t.get("changeRate", 0) or 0)):
            continue
        if float(t.get("volValue", 0) or 0) < 100_000:
            continue
        if base in merged:
            cmc_price = merged[base]["price"]
            if cmc_price > 0:
                ratio = max(price, cmc_price) / min(price, cmc_price)
                if ratio > 1.5:
                    merged[base]["sources"].append("KUCOIN_MISMATCH")
                    continue
            merged[base]["sources"].append("KuCoin")
            merged[base]["kucoin_vol"] = float(t.get("volValue", 0) or 0)
        else:
            merged[base] = {
                "symbol": base, "name": base, "price": price,
                "ch1h": 0, "ch4h": 0,
                "ch24h": float(t.get("changeRate", 0) or 0) * 100,
                "ch7d": 0, "vol_chg": 0, "mcap": 0, "cmc_rank": 9999,
                "sources": ["KuCoin"],
            }

    # ── HUOBİ ────────────────────────────────────────────────
    for t in huobi_tickers:
        sym = t.get("symbol", "")
        if not sym.endswith("usdt"):
            continue
        base = sym[:-4].upper()
        price = float(t.get("close", 0) or 0)
        if price <= 0:
            continue
        if is_stablecoin(base, price, 0, 0):
            continue
        vol = float(t.get("vol", 0) or 0) * price
        if vol < 100_000:
            continue
        if base in merged:
            cmc_price = merged[base]["price"]
            if cmc_price > 0:
                ratio = max(price, cmc_price) / min(price, cmc_price)
                if ratio > 1.5:
                    merged[base]["sources"].append("HUOBI_MISMATCH")
                    continue
            merged[base]["sources"].append("Huobi")
            merged[base]["huobi_vol"] = vol
        else:
            merged[base] = {
                "symbol": base, "name": base, "price": price,
                "ch1h": 0, "ch4h": 0,
                "ch24h": float(t.get("open", 0) or 0),
                "ch7d": 0, "vol_chg": 0, "mcap": 0, "cmc_rank": 9999,
                "sources": ["Huobi"],
            }

    # ── COİNBASE ─────────────────────────────────────────────
    coinbase_symbols = set()
    for p in coinbase_tickers:
        base = p.get("base_currency", "")
        if base:
            coinbase_symbols.add(base)
            if base in merged:
                merged[base]["sources"].append("Coinbase")
            # Coinbase'de listelenmiş ama diğerlerinde yok — kalite işareti olarak sakla

    # ── TOPLAM HACİM + BORSA SKORU hesapla ───────────────────
    for sym, coin in merged.items():
        sources = coin.get("sources", [])
        # Mismatch olanları çıkar
        clean_sources = [s for s in sources if "MISMATCH" not in s]
        coin["exchange_count"] = len(clean_sources)
        coin["exchanges"] = "+".join(clean_sources)
        coin["on_coinbase"] = sym in coinbase_symbols

        # Toplam hacim — tüm borsalardan topla
        total_vol = 0
        total_vol += coin.get("binance_vol", 0)
        total_vol += coin.get("kucoin_vol", 0)
        total_vol += coin.get("huobi_vol", 0)
        if total_vol > 0:
            coin["total_vol_usd"] = total_vol

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
            .eq("symbol", symbol).order("created_at", desc=True) \
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

        is_urgent = s["conviction"] == "CRITICAL" or "DİP SIÇRAMASI" in " ".join(s.get("reasons", []))
        urgent_prefix = "🔴 ACİL — " if is_urgent else ""

        desc = (
            f"{urgent_prefix}{emoji} {s['symbol']}/USDT | {ps} | "
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
            "exchanges": s.get("exchanges", ""),
            "exchange_count": s.get("exchange_count", 0),
            "on_coinbase": s.get("on_coinbase", False),
            "binance_vol": s.get("binance_vol", 0),
            "market": "CRYPTO",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }).execute()

        signal_cache[s["symbol"]] = time.time()
        sid = res.data[0].get("id") if res.data else None

        log_activity("SINYAL", symbol=s["symbol"], price=price,
                      detail=f"Score:{s['score']} | RSI:{s['rsi']} | OBV:{s['obv_trend']}",
                      conviction=s["conviction"], layer=s["layer"])

        # 19 Haz 2026 — KRİTİK EKSİK GİDERİLDİ: save_signal() hiçbir zaman
        # push göndermiyordu, sadece DB'ye yazıyordu. Push sadece bot_buy()
        # içinde (yani sadece CRITICAL + bot gerçekten alabildiğinde)
        # gönderiliyordu. Sonuç: READY gibi HIGH sinyaller (veya CRITICAL
        # olup slot doluluğundan alınamayanlar) kullanıcının hiç haberi
        # olmadan sessizce DB'de kalıyordu — sistem doğru yakalıyordu ama
        # kullanıcı göremiyordu. Artık HIGH ve CRITICAL sinyaller bot alıp
        # almadığına bakılmaksızın HER ZAMAN push ediliyor; kullanıcı
        # isterse manuel karar verebilsin.
        #
        # ACİLİYET VURGUSU: "dip sıçraması" pattern'i (RSI<20 + ch1h>=3,
        # READY örneğindeki gibi 1 saatte %24 gidebilen tip) veya CRITICAL
        # sinyaller 🔴 ile işaretlenip kullanıcıya CMC'de doğrulaması
        # için açık çağrı yapılıyor — bot almasa/alamasa bile bu sinyaller
        # gözden kaçmasın.
        if s["conviction"] in ("CRITICAL", "HIGH"):
            if is_urgent:
                urgency_tag = "🔴 ACİL"
                cta = f"CMC'de {s['symbol']} kontrol et — hızlı hareket ediyor!"
            else:
                urgency_tag = emoji
                cta = f"{s['layer']} sinyali"
            send_push(
                title=f"{urgency_tag} {s['conviction']}: {s['symbol']}",
                body=f"{ps} | 1s:%{s['ch1h']:+.1f} | RSI:{s['rsi']} | {cta}",
                signal_id=sid,
            )

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

    # ── MAKRO KONTROL ────────────────────────────────────────
    try:
        ms = supabase.table("market_status").select("crypto_status, vix") \
            .eq("id", 1).maybeSingle().execute()
        if ms.data:
            if ms.data.get("crypto_status") == "RED":
                print(f"  🔴 MAKRO RED — BTC düşüşte, {symbol} alımı durduruldu")
                return
            vix = float(ms.data.get("vix") or 0)
            if vix >= 30:
                print(f"  🔴 VIX {vix:.1f} — küresel kriz, {symbol} alımı durduruldu")
                return
            if vix >= 25:
                print(f"  ⚠️ VIX {vix:.1f} — yüksek korku, {symbol} alımı durduruldu")
                return
    except Exception:
        pass  # Tablo yoksa devam et

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

        # Aynı coin'de zaten açık pozisyon varsa tekrar alım yapma —
        # aynı sinyalin art arda/çift tetiklenmesiyle aynı coin için
        # ikinci bir pozisyon açılmasını engeller.
        if any(t.get("symbol") == symbol for t in open_list):
            print(f"  ⏭️ {symbol} zaten açık pozisyonda — tekrar alım atlandı")
            return

        # Kapasite kontrolü
        max_normal = MAX_OPEN_PRO if is_pro else MAX_OPEN_FREE
        exceptional_count = sum(1 for t in open_list if t.get("is_exceptional"))
        normal_count = open_count - exceptional_count
        is_exceptional = False

        if normal_count >= max_normal:
            if is_pro and score >= 16 and exceptional_count < 3 and open_count < MAX_OPEN_PRO_EXCEPTIONAL:
                is_exceptional = True
                print(f"  🌟 İSTİSNAİ: {symbol} (score={score}) — slot {open_count+1}")
            else:
                # Rotasyon YOK — doluysa geç
                print(f"  ⏭️ {user_id} dolu (normal:{normal_count} istisna:{exceptional_count}) — {symbol} atlandı")
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
        record_signal_outcome(signal_id, symbol, layer, conviction, rsi, obv, price, score=score)

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
    print(f"\n🦅 KARTAL GÖZÜ V13 — {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}")
    print("=" * 55)

    # ── ÖNCE izleme listesini kontrol et ───────────────────
    check_watchlist_movement()

    # ── Öğrenme sistemi — 3 taramada bir ──────────────────
    if scan_count % 3 == 0:
        check_signal_outcomes()
        update_learning_weights()

    # ── Güvenilir coin listesi — Supabase'den yükle ────────
    # Her 20 taramada bir güncelle (~saatte 1x)
    if scan_count % 20 == 0 or not hasattr(scan_once, '_trusted_coins'):
        try:
            r = supabase.table("trusted_coins").select("symbol").execute()
            scan_once._trusted_coins = {row["symbol"] for row in (r.data or [])}
            print(f"  ✅ Güvenilir coin listesi: {len(scan_once._trusted_coins)} coin")
        except Exception as e:
            print(f"⚠️ trusted_coins yüklenemedi: {e}")
            scan_once._trusted_coins = set()
    trusted_coins_db = scan_once._trusted_coins

    fg = get_fear_greed()
    if fg:
        e = "😱" if fg["value"] <= 25 else "😐" if fg["value"] <= 50 else "😊" if fg["value"] <= 75 else "🤑"
        print(f"{e} Korku/Açgözlülük: {fg['value']} ({fg['label']})")

    print("📡 Veri toplanıyor...")
    mexc = get_mexc_tickers()
    time.sleep(1)
    gateio = get_gateio_tickers()
    time.sleep(1)
    binance = get_binance_tickers()  # Railway'de engellenmiş olabilir, sessiz geç
    kucoin = get_kucoin_tickers()
    time.sleep(1)
    huobi = get_huobi_tickers()
    time.sleep(1)
    coinbase = get_coinbase_tickers()
    time.sleep(1)
    cmc = get_cmc_coins()

    # Güvenilir borsa listesi — Binance gelirse ekle, yoksa KuCoin+Huobi+Coinbase yeterli
    trusted_symbols = set()
    for t in binance:
        s = t.get("symbol", "")
        if s.endswith("USDT"):
            trusted_symbols.add(s[:-4])
    for t in kucoin:
        s = t.get("symbol", "")
        if s.endswith("-USDT"):
            trusted_symbols.add(s[:-5])
    for t in huobi:
        s = t.get("symbol", "")
        if s.endswith("usdt"):
            trusted_symbols.add(s[:-4].upper())
    for p in coinbase:
        trusted_symbols.add(p.get("base_currency", ""))
    sources = "Binance+" if binance else ""
    print(f"  → Güvenilir evren: {len(trusted_symbols)} coin ({sources}KuCoin+Huobi+Coinbase)")

    coins = merge_exchange_data(mexc, gateio, cmc, binance, kucoin, huobi, coinbase)
    if not coins:
        print("⚠️ Veri alınamadı")
        return 0

    now = time.time()
    scored = []
    print(f"🔍 {len(coins)} coin analiz ediliyor...")

    for coin in coins:
        symbol = coin["symbol"]
        try:
            # Leveraged token filtresi — 3S, 5S, 3L, 5L, 2L, 2S suffix'li coinler
            LEVERAGED_SUFFIXES = ("3S", "5S", "3L", "5L", "2S", "2L", "10S", "10L")
            if any(symbol.endswith(s) for s in LEVERAGED_SUFFIXES):
                continue
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
            if price <= 0:
                continue
            if ch1h >= 15 or ch1h < -5:  # Daha geniş aralık
                continue
            if ch4h < -10:
                continue
            if ch24h >= 500:  # Sadece aşırı pompaları ele
                continue
            if ch7d >= 1000:
                continue

            # SEMBOL ÇAKIŞMASI SIKI MODU: CMC ile MEXC/Gate.io fiyatları
            # arasında >%50 sapma tespit edilmişse (merge_exchange_data),
            # bu sembol farklı projelere ait birden fazla coin'i
            # temsil ediyor olabilir — güvenilmez, tamamen ele.
            sources = coin.get("sources", [])
            if any(s.endswith("_MISMATCH") for s in sources):
                print(f"  🚫 SEMBOL ÇAKIŞMASI: {symbol} elendi "
                      f"(fiyat oranı {coin.get('mismatch_ratio', '?')}x — "
                      f"CMC ile borsa fiyatı uyuşmuyor, farklı coin olabilir)")
                continue

            # SÜREGEN MOMENTUM İSTİSNASI:
            # Coin şu an duraklamış (ch1h<1.5) ve hacim patlaması yok (vol_chg<20)
            # ama son 24s'te zaten %8+ yürümüş — bu "tükenmiş" olabilir AMA
            # "yavaş istikrarlı yükselişin devamı" da olabilir. Eleme — geçsin,
            # score_coin RSI/OBV ile karar versin, "SUREGEN" olarak etiketlensin.
            suregen_candidate = False
            if vol_chg < 20 and ch1h < 1.5:
                if ch24h >= 5:
                    if ch1h == 0.0 and vol_chg == 0.0:
                        continue
                    suregen_candidate = True
                    print(f"  🔬 SUREGEN ADAY: {symbol} | ch1h:{ch1h:.2f} "
                          f"ch24h:{ch24h:.2f} vol_chg:{vol_chg:.1f} price:{price}")
                else:
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

            # ── DİNAMİK KALİTE FİLTRESİ ─────────────────────────
            mcap = coin.get("mcap", 0)
            cmc_rank = coin.get("cmc_rank", 9999)

            # Temel kalite eşiği — sadece mcap ve rank filtresi
            if mcap < 1_000_000:
                continue  # $1M altı mcap — likidite yok

            if mcap < 5_000_000 and cmc_rank > 2000:
                continue  # Çok küçük ve bilinmez — manipülasyon riski

            # trusted_coins DB'de varsa ekstra bonus olarak kullan (engel değil)
            in_trusted_db = symbol in trusted_coins_db if trusted_coins_db else False

            result = score_coin(
                symbol, coin["name"], price,
                ch1h, ch4h, ch24h, ch7d,
                vol_chg, mcap, cmc_rank,
                tech, fg, orderbook,
                suregen_candidate=suregen_candidate,
                binance_vol=coin.get("binance_vol", 0),
                on_coinbase=coin.get("on_coinbase", False),
                in_trusted_db=in_trusted_db,
            )

            if result:
                # Yaş kontrolü
                age = get_coin_age_days(symbol, coin.get("date_added"))
                if age is not None and age < 30:
                    print(f"  ⏭️ {symbol} elendi — {age} gün önce listelendi")
                    continue

                if result["conviction"] in ["CRITICAL", "HIGH"] and result["score"] >= 14:
                    # Borsa bilgilerini result'a ekle
                    result["exchanges"] = coin.get("exchanges", "")
                    result["exchange_count"] = coin.get("exchange_count", 0)
                    result["on_coinbase"] = coin.get("on_coinbase", False)
                    result["binance_vol"] = coin.get("binance_vol", 0)
                    scored.append(result)
                    exchanges = coin.get("exchanges", "?")
                    ex_count = coin.get("exchange_count", 0)
                    coinbase_tag = " 🏦CB" if coin.get("on_coinbase") else ""
                    ob_log = f" | OB:{orderbook['bid_ask_ratio']}x" if orderbook else ""
                    # Çoklu borsa bonusu — 3+ borsada aynı anda = kurumsal
                    multi_ex_tag = f" 🔥{ex_count}BORSA" if ex_count >= 3 else ""
                    print(f"  🎯 {symbol} | {result['conviction']} | Score:{result['score']} "
                          f"| {result['layer']} | RSI:{result['rsi']} | OBV:{result['obv_trend']}"
                          f"{ob_log} | [{exchanges}]{coinbase_tag}{multi_ex_tag}")
                elif result["score"] >= WATCHLIST_MIN_SCORE:
                    # Sinyal eşiğini geçemedi ama izlemeye değer (MEDIUM)
                    watchlist_update(
                        symbol, price, result.get("rsi"), result.get("obv_trend"),
                        result.get("vol_chg", 0), result["score"],
                        source=coin.get("sources", ["CMC"])[0] if coin.get("sources") else "CMC",
                    )

        except Exception:
            continue

    # ── SEÇİM: CRITICAL sinyaller — tüm layerlar ──────────
    buy_candidates = [
        s for s in scored
        if s["conviction"] == "CRITICAL"
    ]

    # Tüm CRITICAL ve HIGH sinyalleri kaydet — top 5 limiti kaldırıldı
    top = sorted(scored, key=lambda x: -x["score"])

    print(f"\n📋 {len(scored)} aday → {len(top)} sinyal kaydedilecek ({len(buy_candidates)} CRITICAL)")

    signals_found = 0
    for s in top:
        allow = s["conviction"] == "CRITICAL"  # Tüm CRITICAL'lar alınır
        if save_signal(s, fg, allow_buy=allow):
            signals_found += 1
        time.sleep(0.5)

    return signals_found

# ============================================================
# ANA DÖNGÜ
# ============================================================

def main():
    print("🚀 Atlas Kripto Kartal Gözü — V13 Momentum Mimarisi")
    print("📌 Trailing Stop: %8→breakeven→%8→%12 | Min tutma:4s | Max pozisyon:5+3 | BIRIKIM:YOK")
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
