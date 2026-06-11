# -*- coding: utf-8 -*-
import os
import json
import time
import requests
from datetime import datetime, timezone, timedelta
from supabase import create_client
import firebase_admin
from firebase_admin import credentials, messaging

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
GROQ_KEY = os.environ.get("GROQ_API_KEY")
FIREBASE_SERVICE_ACCOUNT = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
CMC_API_KEY = os.environ.get("CMC_API_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
signal_cache = {}

try:
    if FIREBASE_SERVICE_ACCOUNT and not firebase_admin._apps:
        cred = credentials.Certificate(json.loads(FIREBASE_SERVICE_ACCOUNT))
        firebase_admin.initialize_app(cred)
        print("✅ Firebase Admin başlatıldı")
except Exception as e:
    print(f"⚠️ Firebase hatası: {e}")

CMC_HEADERS = {
    "Accepts": "application/json",
    "X-CMC_PRO_API_KEY": CMC_API_KEY,
}

# Binance mirror URL'leri — ABD kısıtı aşmak için sırayla dene
BINANCE_URLS = [
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
    "https://api4.binance.com",
]


# ============================================================
# PUSH
# ============================================================

def send_push_notification(title, body, signal_id=None):
    try:
        profiles = supabase.table("profiles").select("fcm_token").not_.is_("fcm_token", "null").execute()
        if not profiles.data:
            return
        tokens = [p["fcm_token"] for p in profiles.data if p.get("fcm_token")]
        for token in tokens:
            try:
                message = messaging.Message(
                    notification=messaging.Notification(title=title, body=body),
                    data={
                        "market": "CRYPTO",
                        "signal_id": str(signal_id) if signal_id else "",
                        "route": "signals",
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
                messaging.send(message)
            except Exception as e:
                print(f"⚠️ Push hatası: {e}")
        print(f"📱 Push: {len(tokens)} kullanıcı")
    except Exception as e:
        print(f"❌ Push hatası: {e}")


# ============================================================
# VERİ KAYNAKLARI
# ============================================================

def get_cmc_coins():
    """CMC'den 2 strateji: hacim değişimi + 1s kazananlar"""
    try:
        all_coins = {}

        # Strateji 1: Hacim değişimine göre — birikim tespiti
        r1 = requests.get(
            "https://pro-api.coinmarketcap.com/v1/cryptocurrency/listings/latest",
            headers=CMC_HEADERS,
            params={
                "limit": 500,
                "convert": "USD",
                "sort": "volume_24h_percent_change",
                "sort_dir": "desc",
                "price_min": 0.000001,
                "price_max": 2.0,
                "volume_24h_min": 100000,
                "aux": "percent_change_1h,percent_change_4h,percent_change_24h,percent_change_7d,volume_change_24h,market_cap",
            },
            timeout=30
        )
        if r1.status_code == 200:
            for coin in r1.json().get("data", []):
                all_coins[coin.get("id")] = coin
            print(f"  → Hacim listesi: {len(all_coins)} coin")
        else:
            print(f"⚠️ CMC-1: {r1.status_code}")

        time.sleep(2)

        # Strateji 2: 1s kazananlar — erken momentum
        r2 = requests.get(
            "https://pro-api.coinmarketcap.com/v1/cryptocurrency/listings/latest",
            headers=CMC_HEADERS,
            params={
                "limit": 500,
                "convert": "USD",
                "sort": "percent_change_1h",
                "sort_dir": "desc",
                "price_min": 0.000001,
                "price_max": 2.0,
                "volume_24h_min": 100000,
                "aux": "percent_change_1h,percent_change_4h,percent_change_24h,percent_change_7d,volume_change_24h,market_cap",
            },
            timeout=30
        )
        if r2.status_code == 200:
            for coin in r2.json().get("data", []):
                cid = coin.get("id")
                if cid not in all_coins:
                    all_coins[cid] = coin
            print(f"  → Momentum listesi: toplam {len(all_coins)} coin")
        else:
            print(f"⚠️ CMC-2: {r2.status_code}")

        return list(all_coins.values())

    except Exception as e:
        print(f"❌ CMC hatası: {e}")
        return []


def get_binance_klines(symbol_usdt, interval="4h", limit=100):
    """
    Binance klines — API key gerektirmez, ücretsiz.
    RSI, OBV, 4s/1s trend hesaplamak için OHLCV verisi.
    """
    for base_url in BINANCE_URLS:
        try:
            r = requests.get(
                f"{base_url}/api/v3/klines",
                params={
                    "symbol": symbol_usdt,
                    "interval": interval,
                    "limit": limit,
                },
                headers={"Accept": "application/json"},
                timeout=8
            )
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list) and len(data) > 0:
                    return data
            elif r.status_code == 451:
                continue  # Sonraki mirror'ı dene
        except:
            continue
    return None


def calculate_rsi(closes, period=14):
    """RSI hesapla — dış kütüphane yok"""
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
        return 100
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def calculate_obv(closes, volumes):
    """OBV hesapla — fiyat düz ama OBV yükseliyorsa birikim var"""
    if len(closes) < 2:
        return None, None
    obv = 0
    obv_values = [0]
    for i in range(1, len(closes)):
        if closes[i] > closes[i-1]:
            obv += volumes[i]
        elif closes[i] < closes[i-1]:
            obv -= volumes[i]
        obv_values.append(obv)
    # OBV trend: son 10 mum
    recent_obv = obv_values[-10:]
    obv_trend = "up" if recent_obv[-1] > recent_obv[0] else "down"
    return obv_values[-1], obv_trend


def get_technical_data(symbol):
    """
    Binance'den 4s ve 1s klines çek.
    RSI, OBV, trend hesapla.
    """
    try:
        symbol_usdt = symbol + "USDT" if not symbol.endswith("USDT") else symbol

        # 4 saatlik — RSI ve OBV için
        klines_4h = get_binance_klines(symbol_usdt, "4h", 100)
        if not klines_4h or len(klines_4h) < 20:
            return None

        closes_4h = [float(k[4]) for k in klines_4h]
        volumes_4h = [float(k[5]) for k in klines_4h]
        highs_4h = [float(k[2]) for k in klines_4h]
        lows_4h = [float(k[3]) for k in klines_4h]

        # RSI(14) — 30 altı: aşırı satım = fırsat, 70 üstü: aşırı alım = dikkat
        rsi = calculate_rsi(closes_4h)

        # OBV — fiyat düz ama OBV yükseliyorsa whale sessizce giriyor
        obv_val, obv_trend = calculate_obv(closes_4h, volumes_4h)

        # 4 saatlik fiyat değişimi (son 1 mum)
        price_change_4h = ((closes_4h[-1] - closes_4h[-2]) / closes_4h[-2]) * 100 if closes_4h[-2] > 0 else 0

        # Hacim trend 4s — son 5 mumun hacmi önceki 5 mumdanfazla mı?
        vol_recent = sum(volumes_4h[-5:]) / 5
        vol_prev = sum(volumes_4h[-10:-5]) / 5
        vol_ratio_4h = vol_recent / vol_prev if vol_prev > 0 else 1

        # 1 saatlik — kısa vadeli momentum
        klines_1h = get_binance_klines(symbol_usdt, "1h", 24)
        price_change_1h_binance = 0
        vol_ratio_1h = 1
        if klines_1h and len(klines_1h) >= 4:
            closes_1h = [float(k[4]) for k in klines_1h]
            volumes_1h = [float(k[5]) for k in klines_1h]
            price_change_1h_binance = ((closes_1h[-1] - closes_1h[-2]) / closes_1h[-2]) * 100
            avg_vol_1h = sum(volumes_1h[:-1]) / len(volumes_1h[:-1]) if len(volumes_1h) > 1 else 1
            vol_ratio_1h = volumes_1h[-1] / avg_vol_1h if avg_vol_1h > 0 else 1

        # ATR — volatilite ölçüsü (stop loss için)
        atrs = []
        for i in range(1, min(14, len(closes_4h))):
            tr = max(
                highs_4h[i] - lows_4h[i],
                abs(highs_4h[i] - closes_4h[i-1]),
                abs(lows_4h[i] - closes_4h[i-1])
            )
            atrs.append(tr)
        atr = sum(atrs) / len(atrs) if atrs else 0

        return {
            "rsi": rsi,
            "obv_trend": obv_trend,
            "price_change_4h": round(price_change_4h, 2),
            "vol_ratio_4h": round(vol_ratio_4h, 2),
            "vol_ratio_1h": round(vol_ratio_1h, 2),
            "price_change_1h_binance": round(price_change_1h_binance, 2),
            "atr": round(atr, 8),
            "current_price": closes_4h[-1],
        }

    except Exception as e:
        return None


def get_fear_greed():
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=5)
        data = r.json()
        value = int(data["data"][0]["value"])
        label = data["data"][0]["value_classification"]
        return {"value": value, "label": label}
    except:
        return None


def get_last_signal_time(symbol):
    try:
        r = supabase.table("crypto_signals") \
            .select("created_at") \
            .eq("symbol", symbol) \
            .order("created_at", ascending=False) \
            .limit(1) \
            .execute()
        if r.data:
            dt = datetime.fromisoformat(r.data[0]["created_at"].replace("Z", "+00:00"))
            return dt.timestamp()
        return 0
    except:
        return 0


# ============================================================
# KARTAL GÖZÜ — PUANLAMA
# ============================================================

def analyze_coin(cmc_data, tech_data, fear_greed):
    """
    CMC verisi + Binance teknik analiz birleşik puanlama.
    RSI + OBV + 4s trend + hacim değişimi + fiyat momentum
    """
    try:
        quote = cmc_data.get("quote", {}).get("USD", {})
        symbol = cmc_data.get("symbol", "")
        name = cmc_data.get("name", "")
        cmc_rank = cmc_data.get("cmc_rank", 9999)

        price = float(quote.get("price", 0) or 0)
        price_change_1h = float(quote.get("percent_change_1h", 0) or 0)
        price_change_4h = float(quote.get("percent_change_4h", 0) or 0)
        price_change_24h = float(quote.get("percent_change_24h", 0) or 0)
        price_change_7d = float(quote.get("percent_change_7d", 0) or 0)
        volume_24h = float(quote.get("volume_24h", 0) or 0)
        volume_change_24h = float(quote.get("volume_change_24h", 0) or 0)
        market_cap = float(quote.get("market_cap", 0) or 0)

        if price <= 0 or not (0.000001 <= price <= 2.0):
            return None
        if volume_24h < 100_000:
            return None

        # DUMP koruması — kesinlikle sinyal verme
        if price_change_1h >= 30:
            return None  # Pump başlamış, geç kalındı
        if price_change_24h >= 100:
            return None  # Pump & dump riski
        if price_change_1h < -5 and volume_change_24h > 50:
            return None  # Hacim artarken fiyat düşüyor = dump
        if price_change_1h < -3:
            return None
        if price_change_4h < -8:
            return None  # 4 saatte sert düşüş = trend kötü
        if price_change_7d >= 200:
            return None  # Haftalık zaten %200 çıkmış = geç

        score = 0
        reasons = []
        signal_layer = "MOMENTUM"

        # ============================================================
        # 1. RSI ANALİZİ — en güvenilir teknik gösterge
        # ============================================================
        rsi = tech_data.get("rsi") if tech_data else None
        if rsi is not None:
            if rsi < 30:
                score += 5
                reasons.append(f"📉 RSI {rsi:.0f} — Aşırı satım, dip fırsatı")
            elif rsi < 40:
                score += 3
                reasons.append(f"📊 RSI {rsi:.0f} — Satım bölgesinden çıkış")
            elif rsi < 50:
                score += 2
                reasons.append(f"📊 RSI {rsi:.0f} — Nötr, toparlanıyor")
            elif rsi > 70:
                score -= 2  # Aşırı alım — dikkatli ol
                reasons.append(f"⚠️ RSI {rsi:.0f} — Aşırı alım, dikkat")
            elif rsi > 60:
                score += 1
                reasons.append(f"📈 RSI {rsi:.0f} — Momentum güçlü")

        # ============================================================
        # 2. OBV ANALİZİ — whale birikim tespiti
        # ============================================================
        obv_trend = tech_data.get("obv_trend") if tech_data else None
        if obv_trend == "up" and price_change_1h < 5:
            # OBV yükseliyor ama fiyat henüz oynamamış = BIRIKIM
            score += 6
            signal_layer = "BIRIKIM"
            reasons.append("🐋 OBV yükseliyor, fiyat sessiz — whale sessizce giriyor")
        elif obv_trend == "up":
            score += 3
            reasons.append("📈 OBV yükseliyor — alım baskısı güçlü")
        elif obv_trend == "down" and price_change_1h > 3:
            # Fiyat yükseliyor ama OBV düşüyor = zayıf rally, dikkat
            score -= 2
            reasons.append("⚠️ OBV düşüyor — rally zayıf, dump gelebilir")

        # ============================================================
        # 3. HACİM ANALİZİ — CMC volume_change_24h
        # ============================================================
        if volume_change_24h >= 1000:
            score += 7
            if signal_layer != "BIRIKIM":
                signal_layer = "BIRIKIM"
            reasons.append(f"🔥 Hacim %{volume_change_24h:.0f} — olağandışı whale hareketi")
        elif volume_change_24h >= 500:
            score += 5
            reasons.append(f"⚡ Hacim %{volume_change_24h:.0f} — güçlü whale ilgisi")
        elif volume_change_24h >= 200:
            score += 3
            reasons.append(f"Hacim %{volume_change_24h:.0f} — kurumsal ilgi")
        elif volume_change_24h >= 100:
            score += 2
            reasons.append(f"Hacim %{volume_change_24h:.0f} — artış")
        elif volume_change_24h >= 50:
            score += 1
            reasons.append(f"Hacim %{volume_change_24h:.0f} — hafif artış")

        # Kataliz zorunlu
        if volume_change_24h < 50 and (rsi is None or rsi > 50) and obv_trend != "up":
            return None

        # ============================================================
        # 4. FİYAT MOMENTUM — 1s, 4s, 24s
        # ============================================================
        if price_change_1h >= 15:
            score += 5
            reasons.append(f"%{price_change_1h:.1f} güçlü yükseliş (1s)")
        elif price_change_1h >= 8:
            score += 4
            reasons.append(f"%{price_change_1h:.1f} yükseliş (1s)")
        elif price_change_1h >= 5:
            score += 3
            reasons.append(f"%{price_change_1h:.1f} yükseliş (1s)")
        elif price_change_1h >= 2:
            score += 2
            reasons.append(f"%{price_change_1h:.1f} yükseliş (1s)")
        elif price_change_1h >= 1:
            score += 1
            reasons.append(f"%{price_change_1h:.1f} hafif yükseliş (1s)")

        # 4 saatlik trend
        if price_change_4h >= 10:
            score += 4
            reasons.append(f"%{price_change_4h:.1f} güçlü 4s trend")
        elif price_change_4h >= 5:
            score += 3
            reasons.append(f"%{price_change_4h:.1f} pozitif 4s trend")
        elif price_change_4h >= 2:
            score += 2
            reasons.append(f"%{price_change_4h:.1f} 4s yükseliş")
        elif price_change_4h >= 0:
            score += 1

        # 24 saatlik bağlam
        if price_change_24h >= 30:
            score += 3
            reasons.append(f"%{price_change_24h:.1f} güçlü 24s trend")
        elif price_change_24h >= 15:
            score += 2
            reasons.append(f"%{price_change_24h:.1f} pozitif 24s trend")
        elif price_change_24h >= 5:
            score += 1

        # ============================================================
        # 5. MARKET CAP — küçük = yüksek potansiyel
        # ============================================================
        if market_cap > 0:
            if market_cap < 5_000_000:
                score += 3
                reasons.append(f"💎 Micro cap (${market_cap/1_000_000:.1f}M)")
            elif market_cap < 20_000_000:
                score += 2
                reasons.append(f"💎 Küçük cap (${market_cap/1_000_000:.1f}M)")
            elif market_cap < 100_000_000:
                score += 1

        # ============================================================
        # 6. KORKU & AÇGÖZLÜLÜK
        # ============================================================
        if fear_greed:
            if fear_greed["value"] <= 20:
                score += 2
                reasons.append(f"😱 Aşırı Korku ({fear_greed['value']}) — dip fırsatı")
            elif fear_greed["value"] >= 80:
                score += 1

        # SONUÇ
        if score < 5:
            return None

        if score >= 16:
            conviction = "CRITICAL"
        elif score >= 11:
            conviction = "HIGH"
        elif score >= 6:
            conviction = "MEDIUM"
        else:
            return None

        return {
            "symbol": symbol,
            "name": name,
            "price": price,
            "price_change_1h": price_change_1h,
            "price_change_4h": price_change_4h,
            "price_change_24h": price_change_24h,
            "price_change_7d": price_change_7d,
            "volume_change_24h": volume_change_24h,
            "market_cap": market_cap,
            "cmc_rank": cmc_rank,
            "rsi": rsi,
            "obv_trend": obv_trend,
            "atr": tech_data.get("atr", 0) if tech_data else 0,
            "conviction": conviction,
            "reasons": reasons,
            "score": score,
            "signal_layer": signal_layer,
        }

    except Exception as e:
        return None


# ============================================================
# AI ANALİZ
# ============================================================

def get_ai_explanation(coin_data, fear_greed):
    try:
        symbol = coin_data["symbol"]
        name = coin_data["name"]
        price = coin_data["price"]
        signal_layer = coin_data["signal_layer"]
        rsi = coin_data.get("rsi")
        obv_trend = coin_data.get("obv_trend")
        atr = coin_data.get("atr", 0)

        if price < 0.0001:
            price_str = f"${price:.8f}"
        elif price < 0.01:
            price_str = f"${price:.6f}"
        elif price < 1:
            price_str = f"${price:.4f}"
        else:
            price_str = f"${price:.2f}"

        stop_price = price - (atr * 1.5) if atr > 0 else price * 0.92
        target_price = price + (atr * 3) if atr > 0 else price * 1.15

        fg_str = f"{fear_greed['value']} ({fear_greed['label']})" if fear_greed else "N/A"
        mc_str = f"${coin_data['market_cap']/1_000_000:.1f}M" if coin_data["market_cap"] > 0 else "?"

        layer_ctx = (
            "BİRİKİM SİNYALİ: OBV yükseliyor veya hacim patlamış ama fiyat henüz oynamamış. Whale sessizce giriyor. En değerli sinyal türü."
            if signal_layer == "BIRIKIM"
            else "MOMENTUM SİNYALİ: Fiyat ve hacim birlikte yükseliyor."
        )

        prompt = f"""Profesyonel kripto para analistisin. Türkçe yaz. Kısa ve net ol.

=== VERİ ===
Coin: {symbol} ({name}) | Fiyat: {price_str} | Cap: {mc_str}
1s: %{coin_data['price_change_1h']:.1f} | 4s: %{coin_data['price_change_4h']:.1f} | 24s: %{coin_data['price_change_24h']:.1f} | 7g: %{coin_data['price_change_7d']:.1f}
Hacim değişimi: %{coin_data['volume_change_24h']:.0f}
RSI(14): {rsi if rsi else 'Hesaplanamadı'} | OBV: {obv_trend if obv_trend else '?'}
ATR: {atr:.8f} | Tahmini stop: {stop_price:.8f} | Tahmini hedef: {target_price:.8f}
Güven: {coin_data['conviction']} | Katman: {signal_layer}
Korku/Açgözlülük: {fg_str}
Sinyaller: {' | '.join(coin_data['reasons'])}
{layer_ctx}

===ACEMİ===
[Maks 2 cümle. Coin neden hareket ediyor, ne yapmalı. Pump & dump riskini belirt.]
===USTA===
[Maks 3 cümle. RSI/OBV yorumu, kritik seviye, kısa vadeli hedef.]
===PRO===
[Maks 4 cümle. Whale senaryosu güven (1-10), pump & dump riski (1-10), risk/ödül, kesin giriş/stop/hedef $.]"""

        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"},
            json={
                "model": "llama-3.1-8b-instant",
                "messages": [
                    {"role": "system", "content": "Profesyonel kripto analistisin. Formatı değiştirme. Pump & dump riskini her zaman belirt. Türkçe yaz."},
                    {"role": "user", "content": prompt}
                ],
                "max_tokens": 600,
                "temperature": 0.2
            },
            timeout=15
        )
        resp = r.json()
        if "choices" not in resp:
            return ""
        return resp["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"⚠️ AI hatası: {e}")
        return ""


def parse_ai_levels(ai_text):
    acemi, usta, pro = "", "", ""
    try:
        if "===ACEMİ===" in ai_text:
            acemi = ai_text.split("===ACEMİ===")[1].split("===USTA===")[0].strip()
        if "===USTA===" in ai_text:
            usta = ai_text.split("===USTA===")[1].split("===PRO===")[0].strip()
        if "===PRO===" in ai_text:
            pro = ai_text.split("===PRO===")[1].strip()
    except:
        pass
    return acemi, usta, pro


# ============================================================
# ANA TARAMA
# ============================================================

def scan_once():
    print(f"\n🦅 KRİPTO KARTAL GÖZÜ — {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}")
    print("=" * 55)

    fear_greed = get_fear_greed()
    if fear_greed:
        v = fear_greed["value"]
        emoji = "😱" if v <= 25 else "😐" if v <= 50 else "😊" if v <= 75 else "🤑"
        print(f"{emoji} Korku/Açgözlülük: {v} ({fear_greed['label']})")

    print("📡 CMC verisi alınıyor...")
    cmc_coins = get_cmc_coins()
    if not cmc_coins:
        print("⚠️ CMC verisi alınamadı")
        return 0

    now = time.time()
    scored = []

    print(f"🔍 {len(cmc_coins)} coin ön filtre + teknik analiz...")

    for coin in cmc_coins:
        symbol = coin.get("symbol", "")
        try:
            # Cache kontrolü
            if symbol in signal_cache and now - signal_cache[symbol] < 7200:
                continue
            last_time = get_last_signal_time(symbol)
            if now - last_time < 7200:
                signal_cache[symbol] = last_time
                continue

            # Ön filtre — teknik analiz pahalı, önce CMC filtrele
            quote = coin.get("quote", {}).get("USD", {})
            price = float(quote.get("price", 0) or 0)
            price_change_1h = float(quote.get("percent_change_1h", 0) or 0)
            volume_change_24h = float(quote.get("volume_change_24h", 0) or 0)
            price_change_4h = float(quote.get("percent_change_4h", 0) or 0)

            if not (0.000001 <= price <= 2.0):
                continue
            if price_change_1h >= 30 or price_change_1h < -3:
                continue
            if price_change_4h < -8:
                continue
            if volume_change_24h < 30 and price_change_1h < 2:
                continue

            # Teknik analiz — Binance klines
            tech = get_technical_data(symbol)
            time.sleep(0.3)  # Rate limit

            result = analyze_coin(coin, tech, fear_greed)
            if not result:
                continue

            scored.append(result)
            print(f"  🎯 {symbol} | {result['conviction']} | Score:{result['score']} | {result['signal_layer']} | RSI:{result.get('rsi','?')} | OBV:{result.get('obv_trend','?')}")

        except Exception as e:
            continue

    # Sırala — BİRİKİM önce, sonra score
    scored.sort(key=lambda x: (0 if x["signal_layer"] == "BIRIKIM" else 1, -x["score"]))
    top5 = scored[:5]

    print(f"\n📋 {len(scored)} aday → en iyi {len(top5)} sinyal")

    signals_found = 0
    for s in top5:
        try:
            symbol = s["symbol"]
            conviction = s["conviction"]
            price = s["price"]
            signal_layer = s["signal_layer"]

            if price < 0.0001:
                price_str = f"${price:.8f}"
            elif price < 0.01:
                price_str = f"${price:.6f}"
            elif price < 1:
                price_str = f"${price:.4f}"
            else:
                price_str = f"${price:.2f}"

            ai_text = get_ai_explanation(s, fear_greed)
            acemi, usta, pro = parse_ai_levels(ai_text)

            emoji = "🔥" if conviction == "CRITICAL" else "⚡" if conviction == "HIGH" else "🚀"
            layer_emoji = "🐋" if signal_layer == "BIRIKIM" else "📈"

            description = (
                f"{emoji} {symbol}/USDT | {price_str} | "
                f"1s:%{s['price_change_1h']:+.1f} 4s:%{s['price_change_4h']:+.1f} | "
                f"Vol:%{s['volume_change_24h']:+.0f} | "
                f"RSI:{s.get('rsi','?')} | {layer_emoji}{signal_layer} | {conviction}"
            )

            result = supabase.table("crypto_signals").insert({
                "symbol": symbol,
                "coin": symbol,
                "signal_type": signal_layer.lower(),
                "conviction": conviction,
                "value": round(s["price_change_1h"], 2),
                "price": price,
                "volume_ratio": round(s["volume_change_24h"] / 100, 2),
                "price_change_1h": round(s["price_change_1h"], 2),
                "price_change_4h": round(s["price_change_4h"], 2),
                "price_change_24h": round(s["price_change_24h"], 2),
                "description": description,
                "acemi_explanation": acemi,
                "usta_explanation": usta,
                "pro_explanation": pro,
                "market": "CRYPTO",
                "created_at": datetime.now(timezone.utc).isoformat()
            }).execute()

            signal_cache[symbol] = now
            signals_found += 1
            signal_id = result.data[0].get("id") if result.data else None

            push_body = f"{price_str} | RSI:{s.get('rsi','?')} | {layer_emoji}{signal_layer}"
            send_push_notification(
                title=f"{emoji} {symbol} — {conviction}",
                body=push_body,
                signal_id=signal_id
            )

            print(f"✅ {description}")
            time.sleep(0.5)

        except Exception as e:
            print(f"❌ {s.get('symbol','?')}: {e}")
            continue

    return signals_found


# ============================================================
# ANA DÖNGÜ
# ============================================================

def main():
    print("🚀 Atlas Kripto Kartal Gözü — v2 RSI+OBV")
    print("🎯 CMC + Binance klines | RSI | OBV | 4s trend")
    print(f"⏰ {datetime.now(timezone.utc).strftime('%H:%M UTC')}")

    if not CMC_API_KEY:
        print("❌ CMC_API_KEY bulunamadı!")
        return

    try:
        supabase.table("crypto_signals").select("id").limit(1).execute()
        print("✅ crypto_signals tablosu hazır")
    except Exception as e:
        print(f"⚠️ Tablo: {e}")

    scan_count = 0
    while True:
        try:
            found = scan_once()
            scan_count += 1
            print(f"\n✅ Tarama #{scan_count} bitti. {found} sinyal. 15 dk bekleniyor...")
            time.sleep(900)  # 15 dk — Binance klines çok istek atmamak için
        except Exception as e:
            print(f"❌ Ana döngü hatası: {e}")
            time.sleep(120)


if __name__ == "__main__":
    main()
