# -*- coding: utf-8 -*-
"""
ATLAS MAKRO PİYASA İZLEME SİSTEMİ
DeepSeek Web Search ile Gerçek Zamanlı Haber Analizi
Desteklenen Piyasalar: KRİPTO (BTC), ABD (S&P500/VIX), BIST100
"""

import os
import time
import json
import requests
from datetime import datetime, timezone
from typing import Optional, Tuple
from supabase import create_client
import firebase_admin
from firebase_admin import credentials, messaging

# ============================================================
# KONFIGÜRASYON
# ============================================================

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")
FINNHUB_KEY = os.environ.get("FINNHUB_KEY", "")
FIREBASE_SERVICE_ACCOUNT = os.environ.get("FIREBASE_SERVICE_ACCOUNT")

BTC_ALARM_DROP = -3.0
BTC_ALARM_PUMP = 5.0
BTC_RECOVER = 1.0

SP500_ALARM_DROP = -1.5
SP500_CRASH = -2.5
SP500_RECOVER = 0.5

BIST_ALARM_DROP = -2.0
BIST_RECOVER = 0.5

VIX_HIGH = 25
VIX_EXTREME = 30

HEADERS = {'User-Agent': 'Mozilla/5.0'}

# ============================================================
# BAŞLANGIÇ
# ============================================================

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

try:
    if FIREBASE_SERVICE_ACCOUNT:
        cred = credentials.Certificate(json.loads(FIREBASE_SERVICE_ACCOUNT))
        firebase_admin.initialize_app(cred)
        print("✅ Firebase Admin başlatıldı")
except Exception as e:
    print(f"⚠️ Firebase hatası: {e}")

_last_status = {"crypto": "GREEN", "us": "GREEN", "bist": "GREEN"}
_push_cooldown = {}

# ============================================================
# DEEPSEEK WEB SEARCH
# ============================================================

def deepseek_web_search(market: str, price: float, change_pct: float, 
                        event_type: str, custom_query: str = None) -> Optional[str]:
    """DeepSeek web search ile piyasa haberlerini çeker"""
    
    if not DEEPSEEK_API_KEY:
        print("⚠️ DEEPSEEK_API_KEY bulunamadı")
        return None

    if custom_query:
        query = custom_query
    elif event_type == "DROP":
        query = f"{market} neden düştü? Son 1 saatte {change_pct:+.1f}% düşüş. Bloomberg, Reuters, CNBC haber başlıkları"
    elif event_type == "PUMP":
        query = f"{market} neden yükseldi? Son 1 saatte {change_pct:+.1f}% artış. Bloomberg, Reuters, CNBC haber başlıkları"
    elif event_type == "CRASH":
        query = f"{market} çöküş nedeni? {change_pct:+.1f}% ani düşüş. Acil haber analizi"
    else:
        query = f"{market} güncel durum ve son haberler"

    try:
        print(f"  🔍 DeepSeek sorgulanıyor: {query[:80]}...")
        
        response = requests.post(
            "https://api.deepseek.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "deepseek-chat",
                "messages": [
                    {
                        "role": "system",
                        "content": """Sen bir finans uzmanısın. Kullanıcıya piyasa hareketlerini 
                        açıklayan gerçek haberleri sun. Reuters, Bloomberg, CNBC, Financial Times 
                        kaynaklarından gerçek başlıkları kullan.
                        
                        Yanıtı şu formatta ver (Türkçe):
                        
                        📰 SON HABER BAŞLIKLARI:
                        • [Kaynak] Başlık
                        • [Kaynak] Başlık
                        • [Kaynak] Başlık
                        
                        📊 ANALİZ:
                        Kısa analiz (3-4 cümle)"""
                    },
                    {"role": "user", "content": query}
                ],
                "web_search": True,
                "temperature": 0.3,
                "max_tokens": 600
            },
            timeout=20
        )

        if response.status_code == 200:
            data = response.json()
            result = data["choices"][0]["message"]["content"]
            print(f"  ✅ DeepSeek yanıt aldı: {len(result)} karakter")
            return result
        else:
            print(f"  ❌ DeepSeek hata: {response.status_code}")
            return None

    except Exception as e:
        print(f"  ❌ DeepSeek hatası: {e}")
        return None

# ============================================================
# PUSH BİLDİRİM
# ============================================================

def send_push(title: str, body: str, event_type: str = "MACRO") -> None:
    """Push bildirimi gönder"""
    try:
        profiles = supabase.table("profiles").select("fcm_token").not_.is_("fcm_token", "null").execute()
        tokens = [p["fcm_token"] for p in (profiles.data or []) if p.get("fcm_token")]

        for token in tokens:
            try:
                msg = messaging.Message(
                    notification=messaging.Notification(title=title, body=body[:200]),
                    data={"event_type": event_type, "route": "home"},
                    android=messaging.AndroidConfig(
                        priority="high",
                        notification=messaging.AndroidNotification(
                            channel_id="atlas_macro",
                            sound="default"
                        )
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
                continue

        print(f"  📱 Push gönderildi: {title[:50]}...")
    except Exception as e:
        print(f"  ❌ Push hatası: {e}")

def send_push_with_news(title: str, body: str, event_type: str, 
                        market: str, change_pct: float, price: float) -> None:
    """DeepSeek haber analizi ile push bildirimi gönder"""
    
    news = deepseek_web_search(market, price, change_pct, event_type)
    
    if news:
        full_body = f"{body}\n\n{news}"
    else:
        full_body = body
    
    send_push(title, full_body, event_type)
    
    try:
        supabase.table("macro_events").insert({
            "market": market,
            "event_type": f"{event_type}_WITH_NEWS",
            "value": round(float(change_pct), 2),
            "title": title,
            "body": full_body[:500],
            "status": "RED" if "DROP" in event_type or "CRASH" in event_type else "GREEN",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as e:
        print(f"  ❌ Kayıt hatası: {e}")

def save_event(market: str, event_type: str, value: float, 
               title: str, body: str, status: str) -> None:
    """Olayı veritabanına kaydet"""
    try:
        supabase.table("macro_events").insert({
            "market": market,
            "event_type": event_type,
            "value": round(float(value), 2),
            "title": title,
            "body": body,
            "status": status,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as e:
        print(f"  ❌ Kayıt hatası: {e}")

def update_market_status(**kwargs) -> None:
    """Piyasa durumunu güncelle"""
    try:
        update = {"id": 1, "updated_at": datetime.now(timezone.utc).isoformat()}
        for k, v in kwargs.items():
            if isinstance(v, float):
                update[k] = round(float(v), 2)
            else:
                update[k] = v
        supabase.table("market_status").upsert(update, on_conflict="id").execute()
    except Exception as e:
        print(f"  ❌ Durum güncelleme hatası: {e}")

def should_push(key: str, minutes: int = 15) -> bool:
    """Push cooldown kontrolü"""
    now = time.time()
    if now - _push_cooldown.get(key, 0) > minutes * 60:
        _push_cooldown[key] = now
        return True
    return False

# ============================================================
# VERİ ÇEKME FONKSİYONLARI
# ============================================================

def get_btc_change() -> Tuple[Optional[float], Optional[float]]:
    """BTC değişim ve fiyat"""
    for url, params in [
        ("https://api.mexc.com/api/v3/klines", {"symbol": "BTCUSDT", "interval": "1h", "limit": 2}),
        ("https://api.gateio.ws/api/v4/spot/candlesticks", {"currency_pair": "BTC_USDT", "interval": "1h", "limit": 2}),
    ]:
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=8)
            data = r.json()
            if len(data) >= 2:
                if "mexc" in url:
                    prev, curr = float(data[0][4]), float(data[1][4])
                else:
                    prev, curr = float(data[0][2]), float(data[1][2])
                return ((curr - prev) / prev) * 100, curr
        except Exception:
            continue
    return None, None

def get_spy_change() -> Tuple[Optional[float], Optional[float]]:
    """S&P500 değişim ve fiyat - DÜZELTİLDİ"""
    
    # 1. YAHOO FINANCE - S&P500 Endeksi
    try:
        r = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC",
            params={"interval": "1d", "range": "2d"},
            headers={'User-Agent': 'Mozilla/5.0'},
            timeout=10
        )
        if r.status_code == 200:
            data = r.json()
            meta = data["chart"]["result"][0]["meta"]
            price = meta.get("regularMarketPrice", 0)
            prev = meta.get("previousClose", 0)
            if price and prev and price > 1000:
                return ((price - prev) / prev) * 100, price
    except Exception:
        pass

    # 2. FINNHUB - SPY ETF
    if FINNHUB_KEY:
        try:
            r = requests.get(
                f"https://finnhub.io/api/v1/quote?symbol=SPY&token={FINNHUB_KEY}",
                headers=HEADERS, timeout=8
            )
            if r.status_code == 200:
                d = r.json()
                price = d.get("c", 0)
                prev = d.get("pc", 0)
                if price and prev and price > 100:
                    return ((price - prev) / prev) * 100, price
        except Exception:
            pass

    # 3. MEXC SPXUSDT
    try:
        r = requests.get(
            "https://api.mexc.com/api/v3/ticker/24hr?symbol=SPXUSDT",
            headers=HEADERS, timeout=8
        )
        if r.status_code == 200:
            d = r.json()
            price = float(d.get("lastPrice", 0) or 0)
            prev = float(d.get("prevClosePrice", 0) or 0)
            if price and prev and price > 1000:
                return ((price - prev) / prev) * 100, price
    except Exception:
        pass

    return None, None

def get_vix() -> Optional[float]:
    """VIX endeksi"""
    if not FINNHUB_KEY:
        return None
    try:
        r = requests.get(
            f"https://finnhub.io/api/v1/quote?symbol=^VIX&token={FINNHUB_KEY}",
            headers=HEADERS, timeout=8
        )
        if r.status_code == 200:
            vix = r.json().get("c", 0)
            return float(vix) if vix else None
    except Exception:
        pass
    return None

def get_bist100_change() -> Tuple[Optional[float], Optional[float]]:
    """BIST100 değişim ve fiyat"""
    try:
        r = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/XU100.IS",
            params={"interval": "1d", "range": "2d"},
            headers=HEADERS, timeout=8
        )
        meta = r.json()["chart"]["result"][0]["meta"]
        price = meta.get("regularMarketPrice", 0)
        prev = meta.get("previousClose", 0)
        if price and prev:
            return ((price - prev) / prev) * 100, price
    except Exception:
        pass
    return None, None

# ============================================================
# ANA KONTROL FONKSİYONU
# ============================================================

def check_all() -> None:
    """Tüm piyasaları kontrol et"""
    now_utc = datetime.now(timezone.utc)
    hour = now_utc.hour
    print(f"\n{'='*50}")
    print(f"🔍 MAKRO KONTROL {now_utc.strftime('%H:%M UTC')}")
    print(f"{'='*50}")

    # ==========================================================
    # DEEPSEEK TEST - HER DÖNGÜDE BİR KERE
    # ==========================================================
    if DEEPSEEK_API_KEY and int(time.time()) % 300 < 60:  # 5 dakikada bir
        print("\n🧪 DeepSeek Test Sorgusu:")
        test_result = deepseek_web_search(
            "Bitcoin", 65598, -0.37, "TEST",
            "BTC son 1 saatteki haberler"
        )
        if test_result:
            print(f"  ✅ DeepSeek ÇALIŞIYOR! {len(test_result)} karakter")
            lines = test_result.split('\n')[:3]
            for line in lines:
                if line.strip():
                    print(f"  📰 {line[:100]}")
        else:
            print("  ❌ DeepSeek ÇALIŞMIYOR! API Key kontrol et")
    elif not DEEPSEEK_API_KEY:
        print("\n⚠️ DEEPSEEK_API_KEY bulunamadı!")

    # ==========================================================
    # KRİPTO (BTC)
    # ==========================================================
    print("\n💰 KRİPTO:")
    btc_pct, btc_price = get_btc_change()
    if btc_pct is not None:
        print(f"  BTC: ${btc_price:,.0f} | 1s: {btc_pct:+.2f}%")
        crypto_new = _last_status["crypto"]

        if btc_pct <= BTC_ALARM_DROP:
            crypto_new = "RED"
            if _last_status["crypto"] != "RED" and should_push("btc_drop"):
                t = f"🚨 BTC ÇÖKÜYOR {btc_pct:+.1f}%"
                b = f"BTC ${btc_price:,.0f}"
                send_push_with_news(t, b, "BTC_DROP", "Bitcoin", btc_pct, btc_price)
                save_event("CRYPTO", "BTC_DROP", btc_pct, t, b, "RED")

        elif btc_pct >= BTC_ALARM_PUMP and should_push("btc_pump"):
            t = f"🚀 BTC POMPALIYOR {btc_pct:+.1f}%"
            b = f"BTC ${btc_price:,.0f}"
            send_push_with_news(t, b, "BTC_PUMP", "Bitcoin", btc_pct, btc_price)
            save_event("CRYPTO", "BTC_PUMP", btc_pct, t, b, "GREEN")
            crypto_new = "GREEN"

        elif _last_status["crypto"] == "RED" and btc_pct >= BTC_RECOVER:
            crypto_new = "GREEN"
            if should_push("btc_recover"):
                t = f"✅ BTC TOPARLADI {btc_pct:+.1f}%"
                b = f"BTC ${btc_price:,.0f}"
                send_push_with_news(t, b, "BTC_RECOVER", "Bitcoin", btc_pct, btc_price)
                save_event("CRYPTO", "BTC_RECOVER", btc_pct, t, b, "GREEN")

        update_market_status(crypto_status=crypto_new, btc_change_1h=btc_pct)
        _last_status["crypto"] = crypto_new

    # ==========================================================
    # ABD (S&P500 + VIX)
    # ==========================================================
    if 13 <= hour < 20:
        print("\n🇺🇸 ABD PİYASASI:")
        spy_pct, spy_price = get_spy_change()
        vix = get_vix()

        if spy_pct is not None:
            print(f"  S&P500: ${spy_price:,.2f} | Gün: {spy_pct:+.2f}%")
        if vix is not None:
            print(f"  VIX: {vix:.1f}")

        if spy_pct is not None or vix is not None:
            us_new = _last_status["us"]

            if (spy_pct and spy_pct <= SP500_CRASH) or (vix and vix >= VIX_EXTREME):
                us_new = "RED"
                if _last_status["us"] != "RED" and should_push("us_crash"):
                    t = f"🔴 ABD PİYASA ÇÖKÜŞÜ!"
                    b = f"S&P500: {spy_pct:+.1f}% | VIX: {vix:.1f}" if spy_pct and vix else "Kritik seviyeler!"
                    send_push_with_news(t, b, "US_CRASH", "S&P500", spy_pct or 0, spy_price or 0)
                    save_event("US", "CRASH", spy_pct or 0, t, b, "RED")

            elif (spy_pct and spy_pct <= SP500_ALARM_DROP) or (vix and vix >= VIX_HIGH):
                us_new = "YELLOW"
                if _last_status["us"] != "YELLOW" and should_push("us_alert"):
                    t = f"⚠️ ABD PİYASA ALARM"
                    b = f"S&P500: {spy_pct:+.1f}% | VIX: {vix:.1f}" if spy_pct and vix else "Risk artıyor!"
                    send_push_with_news(t, b, "US_ALERT", "S&P500", spy_pct or 0, spy_price or 0)
                    save_event("US", "ALERT", spy_pct or 0, t, b, "YELLOW")

            elif _last_status["us"] in ["RED", "YELLOW"] and spy_pct and spy_pct >= SP500_RECOVER:
                us_new = "GREEN"
                if should_push("us_recover"):
                    t = f"🟢 ABD PİYASA NORMALE DÖNDÜ"
                    b = f"S&P500: {spy_pct:+.1f}% | VIX: {vix:.1f}" if spy_pct and vix else "Sakinleşme!"
                    send_push_with_news(t, b, "US_RECOVER", "S&P500", spy_pct or 0, spy_price or 0)
                    save_event("US", "RECOVER", spy_pct or 0, t, b, "GREEN")

            update_market_status(
                us_status=us_new,
                spy_change_1d=spy_pct or 0,
                vix=vix or 0
            )
            _last_status["us"] = us_new

    # ==========================================================
    # BIST100
    # ==========================================================
    if 7 <= hour < 15:
        print("\n🇹🇷 BIST100:")
        bist_pct, bist_price = get_bist100_change()
        if bist_pct is not None:
            print(f"  BIST100: {bist_price:,.0f} | Gün: {bist_pct:+.2f}%")
            bist_new = _last_status["bist"]

            if bist_pct <= BIST_ALARM_DROP:
                bist_new = "RED"
                if _last_status["bist"] != "RED" and should_push("bist_drop"):
                    t = f"🚨 BIST DÜŞÜYOR {bist_pct:+.1f}%"
                    b = f"BIST100: {bist_price:,.0f}"
                    send_push_with_news(t, b, "BIST_DROP", "BIST100", bist_pct, bist_price)
                    save_event("BIST", "DROP", bist_pct, t, b, "RED")

            elif _last_status["bist"] == "RED" and bist_pct >= BIST_RECOVER:
                bist_new = "GREEN"
                if should_push("bist_recover"):
                    t = f"✅ BIST TOPARLADI {bist_pct:+.1f}%"
                    b = f"BIST100: {bist_price:,.0f}"
                    send_push_with_news(t, b, "BIST_RECOVER", "BIST100", bist_pct, bist_price)
                    save_event("BIST", "RECOVER", bist_pct, t, b, "GREEN")

            update_market_status(bist_status=bist_new, bist_change_1d=bist_pct)
            _last_status["bist"] = bist_new

    print(f"\n✅ Kontrol tamamlandı | {datetime.now(timezone.utc).strftime('%H:%M UTC')}")

# ============================================================
# ANA DÖNGÜ
# ============================================================

def main() -> None:
    """Ana program"""
    print("\n" + "="*60)
    print("🚀 ATLAS MAKRO PİYASA İZLEME SİSTEMİ")
    print("📊 DeepSeek Web Search Entegrasyonu Aktif")
    print("💱 Kripto (BTC) | 🇺🇸 ABD (S&P500/VIX) | 🇹🇷 BIST100")
    print("="*60 + "\n")

    update_market_status(
        crypto_status="GREEN",
        us_status="GREEN",
        bist_status="GREEN",
        btc_change_1h=0,
        spy_change_1d=0,
        bist_change_1d=0,
        vix=20
    )

    while True:
        try:
            check_all()
        except Exception as e:
            print(f"❌ Makro hata: {e}")
            import traceback
            traceback.print_exc()

        print(f"\n⏳ 60 saniye bekleniyor... ({datetime.now(timezone.utc).strftime('%H:%M UTC')})")
        time.sleep(60)

if __name__ == "__main__":
    main()
