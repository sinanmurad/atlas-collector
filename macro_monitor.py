# -*- coding: utf-8 -*-
"""
Atlas Makro Piyasa İzleme Sistemi
BTC, S&P500, BIST100 referanslarini 60sn'de bir kontrol eder.
Google News RSS ile gerçek zamanlı haber çeker.
Tüm veriler Supabase'e kaydedilir.
"""

import os
import time
import json
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from supabase import create_client
import firebase_admin
from firebase_admin import credentials, messaging

# ============================================================
# KONFIGÜRASYON
# ============================================================

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
FIREBASE_SERVICE_ACCOUNT = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
FINNHUB_KEY = os.environ.get("FINNHUB_KEY", "")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
HEADERS = {'User-Agent': 'Mozilla/5.0'}

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

# ============================================================
# FIREBASE BAŞLAT
# ============================================================

try:
    if FIREBASE_SERVICE_ACCOUNT:
        cred = credentials.Certificate(json.loads(FIREBASE_SERVICE_ACCOUNT))
        firebase_admin.initialize_app(cred)
        print("Firebase Admin baslatildi")
except Exception as e:
    print(f"Firebase hatasi: {e}")

# ============================================================
# GOOGLE NEWS RSS - HABER ÇEK
# ============================================================

def get_news_headlines(query, limit=3):
    """Google News RSS'den gerçek haber başlıkları çek"""
    try:
        url = f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
        response = requests.get(url, timeout=10)
        if response.status_code != 200:
            return []
        root = ET.fromstring(response.text)
        items = root.findall('.//item')
        haberler = []
        for item in items[:limit]:
            title = item.find('title')
            if title is not None and title.text:
                temiz_baslik = title.text.strip()
                if len(temiz_baslik) > 100:
                    temiz_baslik = temiz_baslik[:97] + "..."
                haberler.append(temiz_baslik)
        return haberler
    except Exception as e:
        print(f"Haber hatasi: {e}")
        return []

def format_news_for_push(haberler):
    """Haberleri push mesajı formatına çevir"""
    if not haberler:
        return ""
    formatted = "\n\n📰 SON HABERLER:\n"
    for i, haber in enumerate(haberler, 1):
        formatted += f"{i}. {haber}\n"
    return formatted

# ============================================================
# PUSH BİLDİRİM
# ============================================================

def send_push(title, body, event_type="MACRO"):
    try:
        profiles = supabase.table("profiles").select("fcm_token").not_.is_("fcm_token", "null").execute()
        tokens = [p["fcm_token"] for p in (profiles.data or []) if p.get("fcm_token")]
        for token in tokens:
            try:
                msg = messaging.Message(
                    notification=messaging.Notification(title=title, body=body),
                    data={"event_type": event_type, "route": "home"},
                    android=messaging.AndroidConfig(priority="high",
                        notification=messaging.AndroidNotification(channel_id="atlas_macro")),
                    apns=messaging.APNSConfig(payload=messaging.APNSPayload(
                        aps=messaging.Aps(sound="default", badge=1))),
                    token=token,
                )
                messaging.send(msg)
            except Exception:
                pass
        print(f"Push: {title}")
    except Exception as e:
        print(f"Push hatasi: {e}")

# ============================================================
# VERİTABANI
# ============================================================

def save_event(market, event_type, value, title, body, status):
    try:
        supabase.table("macro_events").insert({
            "market": market, "event_type": event_type,
            "value": round(float(value), 2), "title": title,
            "body": body, "status": status,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
        print(f"  ✅ Event kaydedildi: {market} - {event_type}")
    except Exception as e:
        print(f"  ❌ save_event hatasi: {e}")

def update_market_status(**kwargs):
    try:
        update = {"id": 1, "updated_at": datetime.now(timezone.utc).isoformat()}
        for k, v in kwargs.items():
            if v is None:
                continue
            if isinstance(v, float):
                update[k] = round(float(v), 2)
            elif isinstance(v, int):
                update[k] = v
            else:
                update[k] = v
        supabase.table("market_status").upsert(update, on_conflict="id").execute()
        print(f"  ✅ market_status güncellendi: {list(kwargs.keys())}")
    except Exception as e:
        print(f"  ❌ update_market_status hatasi: {e}")

# ============================================================
# VERİ ÇEKME FONKSİYONLARI
# ============================================================

def get_btc_change():
    """BTC fiyat ve 1 saatlik değişim"""
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

def get_spy_change():
    """S&P500 günlük değişim"""
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
            prev = meta.get("chartPreviousClose", 0) or meta.get("previousClose", 0)
            if price and prev and price > 1000:
                return ((price - prev) / prev) * 100, price
    except Exception:
        pass
    return None, None

def get_spy_futures():
    """S&P500 Futures (ES=F)"""
    try:
        r = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/ES%3DF",
            params={"interval": "5m", "range": "1d"},
            headers={'User-Agent': 'Mozilla/5.0'},
            timeout=10
        )
        if r.status_code == 200:
            data = r.json()
            meta = data["chart"]["result"][0]["meta"]
            price = meta.get("regularMarketPrice", 0)
            prev = meta.get("previousClose", 0)
            if price and prev:
                return ((price - prev) / prev) * 100, price
    except Exception:
        pass
    return None, None

def get_vix():
    """VIX endeksi"""
    if FINNHUB_KEY:
        try:
            r = requests.get(
                f"https://finnhub.io/api/v1/quote?symbol=^VIX&token={FINNHUB_KEY}",
                headers=HEADERS, timeout=8)
            if r.status_code == 200:
                vix = r.json().get("c", 0)
                return float(vix) if vix else None
        except Exception:
            pass
    try:
        r = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX",
            params={"interval": "1d", "range": "2d"},
            headers={'User-Agent': 'Mozilla/5.0'},
            timeout=8
        )
        if r.status_code == 200:
            data = r.json()
            meta = data["chart"]["result"][0]["meta"]
            vix = meta.get("regularMarketPrice", 0)
            return float(vix) if vix else None
    except Exception:
        pass
    return None

def get_us10y():
    """ABD 10 Yıllık Tahvil Faizi"""
    try:
        r = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/%5ETNX",
            params={"interval": "1d", "range": "2d"},
            headers={'User-Agent': 'Mozilla/5.0'},
            timeout=8
        )
        if r.status_code == 200:
            data = r.json()
            meta = data["chart"]["result"][0]["meta"]
            price = meta.get("regularMarketPrice", 0)
            if price:
                return price
    except Exception:
        pass
    return None

def get_bist100_change():
    """BIST100 günlük değişim"""
    try:
        r = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/XU100.IS",
            params={"interval": "1d", "range": "2d"},
            headers={'User-Agent': 'Mozilla/5.0'},
            timeout=8
        )
        if r.status_code == 200:
            data = r.json()
            meta = data["chart"]["result"][0]["meta"]
            price = meta.get("regularMarketPrice", 0)
            prev = meta.get("chartPreviousClose", 0) or meta.get("previousClose", 0)
            if price and prev:
                return ((price - prev) / prev) * 100, price
    except Exception:
        pass
    return None, None

# ============================================================
# COOLDOWN
# ============================================================

_last_status = {"crypto": "GREEN", "us": "GREEN", "bist": "GREEN"}
_push_cooldown = {}

def should_push(key, minutes=15):
    now = time.time()
    if now - _push_cooldown.get(key, 0) > minutes * 60:
        _push_cooldown[key] = now
        return True
    return False

# ============================================================
# PRE-MARKET ANALİZİ
# ============================================================

def analyze_premarket_with_real_data():
    """Borsa kapalıyken gerçek verilerle yön tahmini"""
    vix = get_vix()
    btc_pct, _ = get_btc_change()
    futures_pct, _ = get_spy_futures()
    us10y = get_us10y()
    haberler = get_news_headlines("S&P500 pre-market news", 3)
    
    print("\n  📊 PRE-MARKET ANALİZİ:")
    print(f"    VIX: {vix:.1f}" if vix else "    VIX: ❌ Veri yok")
    print(f"    BTC: {btc_pct:+.2f}%" if btc_pct else "    BTC: ❌ Veri yok")
    print(f"    Futures: {futures_pct:+.2f}%" if futures_pct else "    Futures: ❌ Veri yok")
    print(f"    ABD 10Y: %{us10y:.2f}" if us10y else "    ABD 10Y: ❌ Veri yok")
    if haberler:
        print("    📰 HABERLER:")
        for h in haberler:
            print(f"      • {h}")
    
    skor = 0
    if vix and vix < 25:
        skor += 1
    elif vix and vix >= 25:
        skor -= 1
    if btc_pct and btc_pct > 0:
        skor += 1
    elif btc_pct and btc_pct < 0:
        skor -= 1
    if futures_pct and futures_pct > 0:
        skor += 1
    elif futures_pct and futures_pct < 0:
        skor -= 1
    if us10y and us10y < 4.2:
        skor += 1
    elif us10y and us10y > 4.5:
        skor -= 1
    
    if skor >= 2:
        yon = "🟢 YÜKSELİŞ BEKLENİYOR"
        status = "GREEN"
    elif skor <= -2:
        yon = "🔴 DÜŞÜŞ BEKLENİYOR"
        status = "RED"
    else:
        yon = "🟡 NÖTR"
        status = "YELLOW"
    
    print(f"\n  🎯 SONUÇ: {yon} (skor: {skor})")
    return yon, status, skor

# ============================================================
# ANA KONTROL
# ============================================================

def check_all():
    now_utc = datetime.now(timezone.utc)
    hour = now_utc.hour
    print(f"\n📊 Makro kontrol {now_utc.strftime('%H:%M UTC')}")

    # ==================== BTC ====================
    btc_pct, btc_price = get_btc_change()
    if btc_pct is not None:
        print(f"  BTC: ${btc_price:,.0f} | 1s: {btc_pct:+.2f}%")
        if btc_pct <= BTC_ALARM_DROP:
            if should_push("btc_drop"):
                t = f"BTC COKUYOR {btc_pct:+.1f}%"
                b = f"BTC ${btc_price:,.0f}"
                haberler = get_news_headlines("Bitcoin crash reasons")
                if haberler:
                    b += format_news_for_push(haberler)
                send_push(t, b, "BTC_DROP")
                save_event("CRYPTO", "BTC_DROP", btc_pct, t, b, "RED")
                update_market_status(crypto_status="RED", btc_change_1h=btc_pct)
            else:
                update_market_status(btc_change_1h=btc_pct)
        elif btc_pct >= BTC_ALARM_PUMP and should_push("btc_pump"):
            t = f"BTC POMPALIYOR {btc_pct:+.1f}%"
            b = f"BTC ${btc_price:,.0f}"
            haberler = get_news_headlines("Bitcoin rally reasons")
            if haberler:
                b += format_news_for_push(haberler)
            send_push(t, b, "BTC_PUMP")
            save_event("CRYPTO", "BTC_PUMP", btc_pct, t, b, "GREEN")
            update_market_status(crypto_status="GREEN", btc_change_1h=btc_pct)
        else:
            update_market_status(btc_change_1h=btc_pct)

    # ==================== S&P500 ====================
    spy_pct, spy_price = get_spy_change()
    vix = get_vix()
    
    if spy_pct is not None:
        print(f"  SPY: ${spy_price:.2f} | Gun: {spy_pct:+.2f}%")
    if vix is not None:
        print(f"  VIX: {vix:.1f}")
    
    update_market_status(spy_change_1d=spy_pct if spy_pct is not None else 0, vix=vix if vix is not None else 0)

    if not (13 <= hour < 20):
        yon, status, skor = analyze_premarket_with_real_data()
        update_market_status(us_premarket_skor=skor, us_premarket_yon=yon)
    
    if 13 <= hour < 20:
        us_new = "GREEN"
        if (spy_pct and spy_pct <= SP500_CRASH) or (vix and vix >= VIX_EXTREME):
            us_new = "RED"
        elif (spy_pct and spy_pct <= SP500_ALARM_DROP) or (vix and vix >= VIX_HIGH):
            us_new = "YELLOW"
        if us_new != _last_status["us"]:
            update_market_status(us_status=us_new)
            _last_status["us"] = us_new

    # ==================== BIST ====================
    if 7 <= hour < 15:
        bist_pct, bist_price = get_bist100_change()
        if bist_pct is not None:
            print(f"  BIST100: {bist_price:,.0f} | Gun: {bist_pct:+.2f}%")
            if bist_pct <= BIST_ALARM_DROP:
                if should_push("bist_drop"):
                    t = f"BIST DUSUYOR {bist_pct:+.1f}%"
                    b = f"BIST100: {bist_price:,.0f}"
                    haberler = get_news_headlines("BIST100 Turkey market drop")
                    if haberler:
                        b += format_news_for_push(haberler)
                    send_push(t, b, "BIST_DROP")
                    save_event("BIST", "DROP", bist_pct, t, b, "RED")
                    update_market_status(bist_status="RED", bist_change_1d=bist_pct)
                else:
                    update_market_status(bist_change_1d=bist_pct)
            elif _last_status["bist"] == "RED" and bist_pct >= BIST_RECOVER:
                if should_push("bist_recover"):
                    t = f"BIST TOPARLADI {bist_pct:+.1f}%"
                    b = f"BIST100: {bist_price:,.0f}"
                    haberler = get_news_headlines("BIST100 Turkey market recovery")
                    if haberler:
                        b += format_news_for_push(haberler)
                    send_push(t, b, "BIST_RECOVER")
                    save_event("BIST", "RECOVER", bist_pct, t, b, "GREEN")
                    update_market_status(bist_status="GREEN", bist_change_1d=bist_pct)
                else:
                    update_market_status(bist_change_1d=bist_pct)
            else:
                update_market_status(bist_change_1d=bist_pct)
    else:
        print("  📰 BIST KAPALI")
        haberler = get_news_headlines("BIST100 Turkey market news", 3)
        if haberler:
            for h in haberler:
                print(f"    • {h}")

# ============================================================
# ANA DÖNGÜ
# ============================================================

def main():
    print("\n" + "="*60)
    print("🚀 ATLAS MAKRO PİYASA İZLEME SİSTEMİ")
    print("📊 Google News RSS + Gerçek Veri Analizi")
    print("💱 BTC | 🇺🇸 S&P500 | 🇹🇷 BIST100")
    print("="*60 + "\n")
    
    update_market_status(
        crypto_status="GREEN",
        us_status="GREEN",
        bist_status="GREEN",
        btc_change_1h=0,
        spy_change_1d=0,
        bist_change_1d=0,
        vix=20,
        us_premarket_skor=0,
        us_premarket_yon="🟡 NÖTR"
    )
    
    while True:
        try:
            check_all()
        except Exception as e:
            print(f"❌ Makro hata: {e}")
            import traceback
            traceback.print_exc()
        time.sleep(60)

if __name__ == "__main__":
    main()
