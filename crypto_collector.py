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
volume_history = {}  # Hacim geçmişi — birikim tespiti için

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
# VERİ — CoinMarketCap
# ============================================================

def get_all_coins():
    """
    CMC'den $2 altı tüm coinleri çek.
    İki strateji:
    1. Hacim değişimine göre sıralı — ani hacim artışları
    2. 1 saatlik kazananlara göre — momentum yakalamak
    """
    try:
        all_coins = {}

        # STRATEJI 1: Hacim değişimine göre — birikim tespiti
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
                "aux": "volume_24h_reported,circulating_supply,total_supply,market_cap_by_total_supply,percent_change_1h,percent_change_24h,percent_change_7d,volume_change_24h",
            },
            timeout=30
        )
        if r1.status_code == 200:
            data1 = r1.json().get("data", [])
            for coin in data1:
                cid = coin.get("id")
                all_coins[cid] = coin
            print(f"  → Hacim değişimi listesi: {len(data1)} coin")
        else:
            print(f"⚠️ CMC strateji 1: {r1.status_code}")

        time.sleep(2)

        # STRATEJI 2: 1 saatlik kazananlar — erken momentum
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
            },
            timeout=30
        )
        if r2.status_code == 200:
            data2 = r2.json().get("data", [])
            for coin in data2:
                cid = coin.get("id")
                if cid not in all_coins:
                    all_coins[cid] = coin
            print(f"  → 1s momentum listesi: {len(data2)} coin")
        else:
            print(f"⚠️ CMC strateji 2: {r2.status_code}")

        result = list(all_coins.values())
        print(f"✅ Toplam {len(result)} benzersiz coin")
        return result

    except Exception as e:
        print(f"❌ CMC hatası: {e}")
        return []


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
# KARTAL GÖZÜ — KRİPTO PUANLAMA
# ============================================================

def analyze_coin(coin, fear_greed):
    """
    3 katmanlı analiz:
    KATMAN 1 — BİRİKİM: Hacim patlamış ama fiyat henüz oynamamış = EN DEĞERLİ
    KATMAN 2 — MOMENTUM: Fiyat + hacim birlikte yükseliyor = GİRİLEBİLİR
    KATMAN 3 — GEÇ GİRİŞ: Fiyat çok yükselmiş = UYARI, sinyal verilmez
    """
    try:
        quote = coin.get("quote", {}).get("USD", {})

        price = float(quote.get("price", 0) or 0)
        price_change_1h = float(quote.get("percent_change_1h", 0) or 0)
        price_change_24h = float(quote.get("percent_change_24h", 0) or 0)
        price_change_7d = float(quote.get("percent_change_7d", 0) or 0)
        volume_24h = float(quote.get("volume_24h", 0) or 0)
        volume_change_24h = float(quote.get("volume_change_24h", 0) or 0)
        market_cap = float(quote.get("market_cap", 0) or 0)

        symbol = coin.get("symbol", "")
        name = coin.get("name", "")
        cmc_rank = coin.get("cmc_rank", 9999)

        # Temel filtreler
        if price <= 0 or not (0.000001 <= price <= 2.0):
            return None
        if volume_24h < 100_000:
            return None

        # KATMAN 3 KONTROL — Geç giriş, sinyal verme
        if price_change_1h >= 30:
            print(f"  ⚠️ {symbol} %{price_change_1h:.1f} — pump başlamış, geç giriş, pas geç")
            return None
        if price_change_24h >= 100:
            print(f"  ⚠️ {symbol} 24s %{price_change_24h:.1f} — pump & dump riski, pas geç")
            return None

        # Düşüş kontrolü
        if price_change_1h < -5 and volume_change_24h > 50:
            # Hacim artarken fiyat düşüyor = DUMP
            return None
        if price_change_1h < -3:
            return None

        score = 0
        reasons = []
        signal_layer = ""

        # ============================================================
        # KATMAN 1: BİRİKİM TESPİTİ (en değerli sinyal)
        # Hacim patlamış ama fiyat henüz %5'ten az hareket etmiş
        # ============================================================
        is_accumulation = (
            volume_change_24h >= 200 and  # Hacim 3x+ artmış
            abs(price_change_1h) < 5 and  # Ama fiyat henüz oynamamış
            price_change_24h < 20          # 24 saatte de çok oynamamış
        )

        if is_accumulation:
            signal_layer = "BIRIKIM"
            if volume_change_24h >= 1000:
                score += 8
                reasons.append(f"🐋 BIRIKIM: Hacim %{volume_change_24h:.0f} arttı, fiyat henüz oynamamış — whale sessizce giriyor")
            elif volume_change_24h >= 500:
                score += 6
                reasons.append(f"🐋 BIRIKIM: Hacim %{volume_change_24h:.0f} arttı, fiyat sessiz — erken sinyal")
            elif volume_change_24h >= 200:
                score += 4
                reasons.append(f"📊 BIRIKIM: Hacim %{volume_change_24h:.0f} arttı, fiyat henüz tepki vermedi")

        # ============================================================
        # KATMAN 2: MOMENTUM (fiyat + hacim birlikte)
        # ============================================================

        # Hacim değişimi skoru
        if volume_change_24h >= 500:
            score += 5
            if "BIRIKIM" not in signal_layer:
                reasons.append(f"⚡ Hacim %{volume_change_24h:.0f} artış")
        elif volume_change_24h >= 200:
            score += 3
            if "BIRIKIM" not in signal_layer:
                reasons.append(f"Hacim %{volume_change_24h:.0f} artış")
        elif volume_change_24h >= 100:
            score += 2
            reasons.append(f"Hacim %{volume_change_24h:.0f} artış")
        elif volume_change_24h >= 50:
            score += 1
            reasons.append(f"Hacim %{volume_change_24h:.0f} hafif artış")

        # 1 saatlik fiyat hareketi
        if price_change_1h >= 15:
            score += 5
            reasons.append(f"%{price_change_1h:.1f} güçlü yükseliş (1s)")
            signal_layer = signal_layer or "MOMENTUM"
        elif price_change_1h >= 8:
            score += 4
            reasons.append(f"%{price_change_1h:.1f} yükseliş (1s)")
            signal_layer = signal_layer or "MOMENTUM"
        elif price_change_1h >= 5:
            score += 3
            reasons.append(f"%{price_change_1h:.1f} yükseliş (1s)")
            signal_layer = signal_layer or "MOMENTUM"
        elif price_change_1h >= 2:
            score += 2
            reasons.append(f"%{price_change_1h:.1f} yükseliş (1s)")
        elif price_change_1h >= 1:
            score += 1
            reasons.append(f"%{price_change_1h:.1f} hafif yükseliş (1s)")

        # 24 saatlik trend
        if price_change_24h >= 30:
            score += 3
            reasons.append(f"%{price_change_24h:.1f} güçlü 24s trend")
        elif price_change_24h >= 15:
            score += 2
            reasons.append(f"%{price_change_24h:.1f} pozitif 24s trend")
        elif price_change_24h >= 5:
            score += 1
            reasons.append(f"%{price_change_24h:.1f} 24s yükseliş")

        # Market cap bonusu — küçük cap daha fazla hareket potansiyeli
        if market_cap > 0:
            if market_cap < 5_000_000:
                score += 3
                reasons.append(f"💎 Micro cap (${market_cap/1_000_000:.1f}M) — yüksek potansiyel")
            elif market_cap < 20_000_000:
                score += 2
                reasons.append(f"💎 Küçük cap (${market_cap/1_000_000:.1f}M)")
            elif market_cap < 100_000_000:
                score += 1
                reasons.append(f"Cap: ${market_cap/1_000_000:.0f}M")

        # Fear & Greed bonusu
        if fear_greed:
            if fear_greed["value"] <= 20:
                score += 2
                reasons.append(f"😱 Aşırı Korku ({fear_greed['value']}) — dip fırsatı")
            elif fear_greed["value"] >= 80:
                score += 1
                reasons.append(f"🤑 Açgözlülük ({fear_greed['value']})")

        # KATALİZ ZORUNLU: En az bir güçlü sinyal olmalı
        if score < 4:
            return None
        if volume_change_24h < 50 and price_change_1h < 3:
            return None

        # SONUÇ
        if score >= 14:
            conviction = "CRITICAL"
        elif score >= 9:
            conviction = "HIGH"
        elif score >= 5:
            conviction = "MEDIUM"
        else:
            return None

        return {
            "symbol": symbol,
            "name": name,
            "price": price,
            "price_change_1h": price_change_1h,
            "price_change_24h": price_change_24h,
            "price_change_7d": price_change_7d,
            "volume_24h": volume_24h,
            "volume_change_24h": volume_change_24h,
            "market_cap": market_cap,
            "cmc_rank": cmc_rank,
            "conviction": conviction,
            "reasons": reasons,
            "score": score,
            "signal_layer": signal_layer or "MOMENTUM",
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

        if price < 0.0001:
            price_str = f"${price:.8f}"
        elif price < 0.01:
            price_str = f"${price:.6f}"
        elif price < 1:
            price_str = f"${price:.4f}"
        else:
            price_str = f"${price:.2f}"

        fg_str = f"{fear_greed['value']} ({fear_greed['label']})" if fear_greed else "N/A"
        mc_str = f"${coin_data['market_cap']/1_000_000:.1f}M" if coin_data["market_cap"] > 0 else "Bilinmiyor"

        layer_context = ""
        if signal_layer == "BIRIKIM":
            layer_context = "ÖNEMLI: Bu bir BİRİKİM sinyali. Hacim artmış ama fiyat henüz oynamamış. Whale sessizce giriyor olabilir. Bu en değerli sinyal türü."
        else:
            layer_context = "Bu bir MOMENTUM sinyali. Fiyat ve hacim birlikte artıyor."

        prompt = f"""Sen profesyonel bir kripto para analistisin. Düşük fiyatlı altcoin/meme coin uzmanısın. Türkçe yaz.

=== VERİ ===
Coin: {symbol} ({name})
Fiyat: {price_str}
Market Cap: {mc_str}
CMC Sıra: #{coin_data['cmc_rank']}
1 Saatlik: %{coin_data['price_change_1h']:.2f}
24 Saatlik: %{coin_data['price_change_24h']:.2f}
7 Günlük: %{coin_data['price_change_7d']:.2f}
Hacim Değişimi (24s): %{coin_data['volume_change_24h']:.0f}
Güven: {coin_data['conviction']}
Sinyal Katmanı: {signal_layer}
Korku/Açgözlülük: {fg_str}
Sinyaller: {' | '.join(coin_data['reasons'])}

{layer_context}

=== FORMAT ===
===ACEMİ===
[Maks 2 cümle. Bu coin neden hareketleniyor ve ne yapılmalı. Sade Türkçe. Pump & dump riskini mutlaka belirt.]
===USTA===
[Maks 3 cümle. Hacim analizi, birikim mi boşaltma mı, kritik fiyat seviyesi, kısa vadeli hedef.]
===PRO===
[Maks 4 cümle. Whale senaryosu (1-10 güven), pump & dump riski skoru (1-10), risk/ödül, $ giriş/stop/hedef. Kesin ol.]"""

        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"},
            json={
                "model": "llama-3.1-8b-instant",
                "messages": [
                    {"role": "system", "content": "Profesyonel kripto para analistisin. Düşük fiyatlı altcoin uzmanısın. Sadece verilen formatı kullan. Başlıkları değiştirme. Pump & dump riskini her zaman değerlendir. Türkçe yaz."},
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
        result = resp["choices"][0]["message"]["content"]
        print(f"✅ AI: {result[:60]}...")
        return result
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

    print("📡 CMC'den coin listesi alınıyor...")
    all_coins = get_all_coins()
    if not all_coins:
        print("⚠️ Coin listesi alınamadı")
        return 0

    now = time.time()
    scored = []

    print(f"🔍 {len(all_coins)} coin analiz ediliyor...")

    for coin in all_coins:
        symbol = coin.get("symbol", "")
        try:
            cache_key = symbol
            if cache_key in signal_cache and now - signal_cache[cache_key] < 7200:
                continue
            last_time = get_last_signal_time(symbol)
            if now - last_time < 7200:
                signal_cache[cache_key] = last_time
                continue

            result = analyze_coin(coin, fear_greed)
            if not result:
                continue

            scored.append(result)
            print(f"  🎯 {symbol} | {result['conviction']} | Score: {result['score']} | {result['signal_layer']}")

        except Exception as e:
            print(f"❌ {symbol}: {e}")
            continue

    scored.sort(key=lambda x: (
        0 if x["signal_layer"] == "BIRIKIM" else 1,
        -x["score"]
    ))

    top5 = scored[:5]
    print(f"\n📋 {len(scored)} aday → en iyi {len(top5)} sinyal seçildi")

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

            if conviction == "CRITICAL":
                emoji = "🔥"
            elif conviction == "HIGH":
                emoji = "⚡"
            else:
                emoji = "🚀"

            layer_emoji = "🐋" if signal_layer == "BIRIKIM" else "📈"

            description = (
                f"{emoji} {symbol}/USDT | {price_str} | "
                f"1s: %{s['price_change_1h']:+.1f} | "
                f"Hacim: %{s['volume_change_24h']:+.0f} | "
                f"{layer_emoji} {signal_layer} | {conviction}"
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
                "price_change_4h": 0,
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

            push_body = f"{price_str} | 1s: %{s['price_change_1h']:+.1f} | {layer_emoji} {signal_layer}"
            send_push_notification(
                title=f"{emoji} {symbol} — {conviction}",
                body=push_body,
                signal_id=signal_id
            )

            print(f"✅ KAYDEDİLDİ: {description}")
            time.sleep(0.5)

        except Exception as e:
            print(f"❌ {s.get('symbol', '?')}: {e}")
            continue

    return signals_found

# ============================================================
# ANA DÖNGÜ
# ============================================================

def main():
    print("🚀 Atlas Kripto Kartal Gözü başlatıldı...")
    print("🎯 Hedef: $0.000001-$2 | Birikim tespiti | Whale takibi")
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
            print(f"\n✅ Tarama #{scan_count} bitti. {found} sinyal. 10 dk bekleniyor...")
            time.sleep(600)

        except Exception as e:
            print(f"❌ Ana döngü hatası: {e}")
            time.sleep(120)


if __name__ == "__main__":
    main()
