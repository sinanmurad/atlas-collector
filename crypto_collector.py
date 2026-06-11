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

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
signal_cache = {}

try:
    if FIREBASE_SERVICE_ACCOUNT and not firebase_admin._apps:
        cred = credentials.Certificate(json.loads(FIREBASE_SERVICE_ACCOUNT))
        firebase_admin.initialize_app(cred)
        print("✅ Firebase Admin başlatıldı")
except Exception as e:
    print(f"⚠️ Firebase hatası: {e}")


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


def get_all_usdt_pairs():
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/ticker/24hr",
            headers={"Accept": "application/json"},
            timeout=30
        )
        if r.status_code == 429:
            print("⚠️ Binance rate limit — 60sn bekleniyor...")
            time.sleep(60)
            return []
        if r.status_code != 200:
            print(f"❌ Binance HTTP {r.status_code}: {r.text[:100]}")
            return []
        tickers = r.json()
        if not isinstance(tickers, list):
            print(f"❌ Binance beklenmedik yanıt: {r.text[:200]}")
            return []

        candidates = []
        for t in tickers:
            if not isinstance(t, dict):
                continue
            symbol = t.get("symbol", "")
            if not symbol.endswith("USDT"):
                continue
            try:
                price = float(t.get("lastPrice", 0) or 0)
                volume_usdt = float(t.get("quoteVolume", 0) or 0)
                price_change_pct = float(t.get("priceChangePercent", 0) or 0)
                count = int(t.get("count", 0) or 0)
            except:
                continue
            if not (0.000001 <= price <= 2.0):
                continue
            if volume_usdt < 500_000:
                continue
            candidates.append({
                "symbol": symbol,
                "price": price,
                "volume_usdt_24h": volume_usdt,
                "price_change_24h": price_change_pct,
                "trade_count": count,
            })

        candidates.sort(key=lambda x: x["volume_usdt_24h"], reverse=True)
        print(f"✅ {len(candidates)} USDT paritesi bulundu ($2 altı, hacimli)")
        return candidates

    except Exception as e:
        print(f"❌ Binance ticker hatası: {e}")
        return []

def get_1h_candles(symbol):
    """Son 48 saatin 1 saatlik mumlarını çek — baseline hacim ve trend için"""
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": symbol, "interval": "1h", "limit": 48},
            timeout=8
        )
        candles = r.json()
        if not isinstance(candles, list) or len(candles) < 10:
            return None

        closes = [float(c[4]) for c in candles]
        volumes = [float(c[5]) for c in candles]

        # Son mum hariç ortalama hacim
        avg_volume = sum(volumes[:-1]) / len(volumes[:-1])
        current_volume = volumes[-1]
        current_price = closes[-1]
        prev_price = closes[-2] if len(closes) >= 2 else closes[-1]

        # 1 saatlik değişim
        price_change_1h = ((current_price - prev_price) / prev_price) * 100 if prev_price > 0 else 0

        # 4 saatlik trend
        price_4h_ago = closes[-4] if len(closes) >= 4 else closes[0]
        price_change_4h = ((current_price - price_4h_ago) / price_4h_ago) * 100 if price_4h_ago > 0 else 0

        # 24 saatlik trend
        price_24h_ago = closes[-24] if len(closes) >= 24 else closes[0]
        price_change_24h = ((current_price - price_24h_ago) / price_24h_ago) * 100 if price_24h_ago > 0 else 0

        # Hacim patlaması
        volume_ratio = current_volume / avg_volume if avg_volume > 0 else 0

        return {
            "price": current_price,
            "price_change_1h": round(price_change_1h, 2),
            "price_change_4h": round(price_change_4h, 2),
            "price_change_24h": round(price_change_24h, 2),
            "volume_ratio": round(volume_ratio, 2),
            "avg_volume": avg_volume,
            "current_volume": current_volume,
        }

    except:
        return None


def get_fear_greed():
    """Kripto korku & açgözlülük endeksi"""
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=5)
        data = r.json()
        value = int(data["data"][0]["value"])
        label = data["data"][0]["value_classification"]
        return {"value": value, "label": label}
    except:
        return None


def calculate_signal_score(candle_data, price_change_24h_ticker):
    score = 0
    reasons = []

    price_change_1h = candle_data["price_change_1h"]
    price_change_4h = candle_data["price_change_4h"]
    volume_ratio = candle_data["volume_ratio"]

    # Düşüş — sinyal verme
    if price_change_1h < -3:
        return "NORMAL", [], 0

    # KATALİZ ZORUNLU: Hacim patlaması olmadan sinyal yok
    if volume_ratio < 3:
        return "NORMAL", [], 0

    # 1. HACİM PATLAMASI — en önemli gösterge
    if volume_ratio >= 20:
        score += 6
        reasons.append(f"🔥 Hacim {volume_ratio:.1f}x — OLAĞANDIŞI whale hareketi")
    elif volume_ratio >= 10:
        score += 5
        reasons.append(f"⚡ Hacim {volume_ratio:.1f}x — güçlü whale ilgisi")
    elif volume_ratio >= 5:
        score += 4
        reasons.append(f"Hacim {volume_ratio:.1f}x — kurumsal/whale ilgisi")
    elif volume_ratio >= 3:
        score += 2
        reasons.append(f"Hacim {volume_ratio:.1f}x artışı")

    # 2. KISA VADELİ FİYAT HAREKETİ (1s)
    if price_change_1h >= 10:
        score += 5
        reasons.append(f"%{price_change_1h:.1f} patlama (1 saat)")
    elif price_change_1h >= 5:
        score += 4
        reasons.append(f"%{price_change_1h:.1f} güçlü yükseliş (1 saat)")
    elif price_change_1h >= 3:
        score += 3
        reasons.append(f"%{price_change_1h:.1f} yükseliş (1 saat)")
    elif price_change_1h >= 1:
        score += 1
        reasons.append(f"%{price_change_1h:.1f} hafif yükseliş (1 saat)")

    # 3. ORTA VADELİ TREND (4s)
    if price_change_4h >= 15:
        score += 3
        reasons.append(f"%{price_change_4h:.1f} güçlü trend (4 saat)")
    elif price_change_4h >= 8:
        score += 2
        reasons.append(f"%{price_change_4h:.1f} trend (4 saat)")
    elif price_change_4h >= 3:
        score += 1
        reasons.append(f"%{price_change_4h:.1f} pozitif trend (4 saat)")

    # 4. 24 SAATLİK BAĞLAM
    if price_change_24h_ticker >= 30:
        score += 2
        reasons.append(f"%{price_change_24h_ticker:.1f} 24s patlama")
    elif price_change_24h_ticker >= 15:
        score += 1
        reasons.append(f"%{price_change_24h_ticker:.1f} 24s yükseliş")

    # SONUÇ
    if score >= 12:
        conviction = "CRITICAL"
    elif score >= 8:
        conviction = "HIGH"
    elif score >= 5:
        conviction = "MEDIUM"
    else:
        conviction = "NORMAL"

    return conviction, reasons, score


def get_ai_explanation(symbol, candle_data, conviction, reasons, fear_greed):
    try:
        coin = symbol.replace("USDT", "")
        fg_str = f"{fear_greed['value']} ({fear_greed['label']})" if fear_greed else "N/A"
        price = candle_data["price"]

        # Fiyat formatı — sıfırları bol coinler için
        if price < 0.0001:
            price_str = f"${price:.8f}"
        elif price < 0.01:
            price_str = f"${price:.6f}"
        elif price < 1:
            price_str = f"${price:.4f}"
        else:
            price_str = f"${price:.2f}"

        prompt = f"""Sen profesyonel bir kripto para analistisin. Düşük fiyatlı altcoin analizi yap.

=== VERİ ===
Coin: {coin}/USDT
Fiyat: {price_str}
1 Saatlik: %{candle_data['price_change_1h']:.2f}
4 Saatlik: %{candle_data['price_change_4h']:.2f}
24 Saatlik: %{candle_data['price_change_24h']:.2f}
Hacim: normalin {candle_data['volume_ratio']:.1f}x
Güven: {conviction}
Korku/Açgözlülük: {fg_str}
Sinyaller: {' | '.join(reasons)}

=== FORMAT ===
===ACEMİ===
[Maks 2 cümle. Bu coin neden hareket ediyor ve ne beklenmeli. Sade Türkçe.]
===USTA===
[Maks 3 cümle. Hacim analizi, momentum gücü, kısa vadeli hedef seviye.]
===PRO===
[Maks 4 cümle. Whale olasılığı, pump & dump riski, risk/ödül oranı, $ giriş/stop/hedef seviyeleri.]"""

        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"},
            json={
                "model": "llama-3.1-8b-instant",
                "messages": [
                    {"role": "system", "content": "Profesyonel kripto para analistisin. Düşük fiyatlı altcoin uzmanısın. Sadece verilen formatı kullan. Başlıkları değiştirme."},
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


def scan_once():
    """
    Tüm Binance USDT paritelerini tara
    $2 altı, hacim patlaması, momentum yakalamak
    """
    print(f"\n🔍 Kripto tarama başlıyor... {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}")

    # Fear & Greed — bir kez al
    fear_greed = get_fear_greed()
    if fear_greed:
        print(f"😱 Korku/Açgözlülük: {fear_greed['value']} ({fear_greed['label']})")

    # Tüm ticker'ları al
    all_pairs = get_all_usdt_pairs()
    if not all_pairs:
        return 0

    now = time.time()
    scored = []

    for ticker in all_pairs:
        symbol = ticker["symbol"]

        try:
            # Son 1 saat içinde sinyal verildi mi
            if symbol in signal_cache and now - signal_cache[symbol] < 3600:
                continue

            last_time = get_last_signal_time(symbol)
            if now - last_time < 3600:
                signal_cache[symbol] = last_time
                continue

            # 1 saatlik mum verisi
            candle = get_1h_candles(symbol)
            if not candle:
                time.sleep(0.05)
                continue

            # Puanla
            conviction, reasons, score = calculate_signal_score(
                candle, ticker["price_change_24h"]
            )

            if conviction == "NORMAL":
                time.sleep(0.05)
                continue

            scored.append({
                "symbol": symbol,
                "price": candle["price"],
                "candle": candle,
                "conviction": conviction,
                "reasons": reasons,
                "score": score,
                "volume_usdt_24h": ticker["volume_usdt_24h"],
            })

            print(f"  🎯 {symbol} | {conviction} | Score: {score}")
            for r in reasons:
                print(f"     → {r}")

            time.sleep(0.1)

        except Exception as e:
            print(f"❌ {symbol}: {e}")
            continue

    # En iyi 5 sinyal
    scored.sort(key=lambda x: x["score"], reverse=True)
    top5 = scored[:5]

    print(f"\n  📋 {len(scored)} aday → en iyi {len(top5)} sinyal seçildi")

    signals_found = 0
    for s in top5:
        try:
            symbol = s["symbol"]
            coin = symbol.replace("USDT", "")
            conviction = s["conviction"]
            price = s["price"]
            candle = s["candle"]

            # Fiyat formatı
            if price < 0.0001:
                price_str = f"${price:.8f}"
            elif price < 0.01:
                price_str = f"${price:.6f}"
            elif price < 1:
                price_str = f"${price:.4f}"
            else:
                price_str = f"${price:.2f}"

            ai_text = get_ai_explanation(symbol, candle, conviction, s["reasons"], fear_greed)
            acemi, usta, pro = parse_ai_levels(ai_text)

            if conviction == "CRITICAL":
                emoji = "🔥"
            elif conviction == "HIGH":
                emoji = "⚡"
            else:
                emoji = "🚀"

            description = (
                f"{emoji} {coin}/USDT | {price_str} | "
                f"1s: %{candle['price_change_1h']:+.1f} | "
                f"Hacim: {candle['volume_ratio']:.1f}x | {conviction}"
            )

            result = supabase.table("crypto_signals").insert({
                "symbol": symbol,
                "coin": coin,
                "signal_type": "momentum",
                "conviction": conviction,
                "value": round(candle["price_change_1h"], 2),
                "price": price,
                "volume_ratio": round(candle["volume_ratio"], 2),
                "price_change_1h": round(candle["price_change_1h"], 2),
                "price_change_4h": round(candle["price_change_4h"], 2),
                "price_change_24h": round(candle["price_change_24h"], 2),
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

            send_push_notification(
                title=f"{emoji} {coin} — {conviction}",
                body=f"{price_str} | 1s: %{candle['price_change_1h']:+.1f} | Hacim: {candle['volume_ratio']:.1f}x",
                signal_id=signal_id
            )

            print(f"✅ KAYDEDİLDİ: {description}")
            time.sleep(0.5)

        except Exception as e:
            print(f"❌ {s.get('symbol', '?')}: {e}")
            continue

    return signals_found


def main():
    print("🚀 Atlas Kripto Kartal Gözü başlatıldı...")
    print("💡 Hedef: $0.000001 - $2 arası, hacim patlaması olan coinler")
    print(f"⏰ {datetime.now(timezone.utc).strftime('%H:%M UTC')}")

    # Supabase tablosu oluştur
    try:
        supabase.table("crypto_signals").select("id").limit(1).execute()
        print("✅ crypto_signals tablosu hazır")
    except Exception as e:
        print(f"⚠️ Tablo kontrolü: {e}")

    scan_count = 0

    while True:
        try:
            found = scan_once()
            scan_count += 1
            print(f"✅ Tarama #{scan_count} bitti. {found} sinyal. 5 dk bekleniyor...")
            time.sleep(300)  # 5 dk'da bir tara — kripto hızlı hareket eder

        except Exception as e:
            print(f"❌ Ana döngü hatası: {e}")
            time.sleep(60)


if __name__ == "__main__":
    main()
