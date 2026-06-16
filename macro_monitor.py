# -*- coding: utf-8 -*-
"""
Atlas Makro Piyasa İzleme Sistemi
- Gerçek verilerle borsa öncesi yön tahmini
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
# GERÇEK VERİ ÇEKME FONKSİYONLARI
# ============================================================

def get_btc_change():
    """BTC fiyat ve 1 saatlik değişim - GERÇEK"""
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
    """S&P500 günlük değişim - GERÇEK"""
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
    """S&P500 Futures - GERÇEK (Borsa kapalıyken yön gösterir)"""
    try:
        # ES=F (S&P500 Futures) - Yahoo Finance
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
                pct = ((price - prev) / prev) * 100
                return pct, price
    except Exception:
        pass
    return None, None

def get_vix():
    """VIX endeksi - GERÇEK"""
    if not FINNHUB_KEY:
        return None
    try:
        r = requests.get(
            f"https://finnhub.io/api/v1/quote?symbol=^VIX&token={FINNHUB_KEY}",
            headers=HEADERS, timeout=8)
        if r.status_code == 200:
            vix = r.json().get("c", 0)
            return float(vix) if vix else None
    except Exception:
        pass
    return None

def get_us10y():
    """ABD 10 Yıllık Tahvil Faizi - GERÇEK"""
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
    """BIST100 günlük değişim - GERÇEK"""
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
    except Exception as e:
        print(f"save_event: {e}")

def update_market_status(**kwargs):
    try:
        update = {"id": 1, "updated_at": datetime.now(timezone.utc).isoformat()}
        update.update({k: round(float(v), 2) if isinstance(v, float) else v
                        for k, v in kwargs.items()})
        supabase.table("market_status").upsert(update, on_conflict="id").execute()
    except Exception as e:
        print(f"update_market_status: {e}")

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
# SENTİMENT ANALİZİ - GERÇEK VERİLERLE
# ============================================================

def analyze_premarket_with_real_data():
    """Borsa kapalıyken gerçek verilerle yön tahmini"""
    
    # 1. VIX - Korku endeksi
    vix = get_vix()
    vix_sinyal = ""
    if vix:
        if vix >= 30:
            vix_sinyal = "🔴 AŞIRI KORKU (DÜŞÜŞ)"
        elif vix >= 25:
            vix_sinyal = "🟡 YÜKSEK RİSK (TEDİRGİN)"
        else:
            vix_sinyal = "🟢 SAKİN (YÜKSELİŞ)"
    
    # 2. BTC - Küresel risk iştahı
    btc_pct, btc_price = get_btc_change()
    btc_sinyal = ""
    if btc_pct:
        if btc_pct >= 2:
            btc_sinyal = "🟢 RİSK İŞTAHI YÜKSEK"
        elif btc_pct <= -2:
            btc_sinyal = "🔴 RİSK İŞTAHI DÜŞÜK"
        else:
            btc_sinyal = "🟡 NÖTR"
    
    # 3. S&P500 Futures - Borsa öncesi yön
    futures_pct, futures_price = get_spy_futures()
    futures_sinyal = ""
    if futures_pct:
        if futures_pct >= 0.5:
            futures_sinyal = "🟢 YÜKSELİŞ BEKLENİYOR"
        elif futures_pct <= -0.5:
            futures_sinyal = "🔴 DÜŞÜŞ BEKLENİYOR"
        else:
            futures_sinyal = "🟡 NÖTR"
    
    # 4. ABD 10 Yıllık Tahvil
    us10y = get_us10y()
    tahvil_sinyal = ""
    if us10y:
        if us10y > 4.5:
            tahvil_sinyal = "🔴 FAİZ YÜKSEK (BASKI)"
        elif us10y < 4.0:
            tahvil_sinyal = "🟢 FAİZ DÜŞÜK (DESTEK)"
        else:
            tahvil_sinyal = "🟡 NÖTR"
    
    # 5. Haberler - Google News
    haberler = get_news_headlines("S&P500 pre-market news", 3)
    
    # Kombine Analiz
    print("\n  📊 PRE-MARKET ANALİZİ (GERÇEK VERİLER):")
    print(f"    VIX ({vix:.1f}): {vix_sinyal}" if vix else "    VIX: ❌ Veri yok")
    print(f"    BTC ({btc_pct:+.2f}%): {btc_sinyal}" if btc_pct else "    BTC: ❌ Veri yok")
    print(f"    S&P500 Futures ({futures_pct:+.2f}%): {futures_sinyal}" if futures_pct else "    S&P500 Futures: ❌ Veri yok")
    print(f"    ABD 10Y (%{us10y:.2f}): {tahvil_sinyal}" if us10y else "    ABD 10Y: ❌ Veri yok")
    
    if haberler:
        print("    📰 SON HABERLER:")
        for h in haberler:
            print(f"      • {h}")
    
    # Toplam Skor
    skor = 0
    if vix and vix < 25: skor += 1
    if vix and vix >= 25: skor -= 1
    if btc_pct and btc_pct > 0: skor += 1
    if btc_pct and btc_pct < 0: skor -= 1
    if futures_pct and futures_pct > 0: skor += 1
    if futures_pct and futures_pct < 0: skor -= 1
    if us10y and us10y < 4.2: skor += 1
    if us10y and us10y > 4.5: skor -= 1
    
    if skor >= 2:
        yon = "🟢 YÜKSELİŞ BEKLENİYOR"
        status = "GREEN"
    elif skor <= -2:
        yon = "🔴 DÜŞÜŞ BEKLENİYOR"
        status = "RED"
    else:
        yon = "🟡 NÖTR"
        status = "YELLOW"
    
    print(f"\n  🎯 SONUÇ: {yon}")
    return yon, status, skor

# ============================================================
# ANA KONTROL
# ============================================================

def check_all():
    now_utc = datetime.now(timezone.utc)
    hour = now_utc.hour
    print(f"\n📊 Makro kontrol {now_utc.strftime('%H:%M UTC')}")

    # ==================== BTC (HER ZAMAN) ====================
    btc_pct, btc_price = get_btc_change()
    if btc_pct is not None:
        print(f"  BTC: ${btc_price:,.0f} | 1s: {btc_pct:+.2f}%")
        crypto_new = _last_status["crypto"]

        if btc_pct <= BTC_ALARM_DROP:
            crypto_new = "RED"
            if _last_status["crypto"] != "RED" and should_push("btc_drop"):
                t = f"BTC COKUYOR {btc_pct:+.1f}%"
                b = f"BTC ${btc_price:,.0f} | Kripto alimlar durduruldu"
                haberler = get_news_headlines("Bitcoin crash reasons")
                if haberler:
                    b += format_news_for_push(haberler)
                send_push(t, b, "BTC_DROP")
                save_event("CRYPTO", "BTC_DROP", btc_pct, t, b, "RED")
                
        elif btc_pct >= BTC_ALARM_PUMP and should_push("btc_pump"):
            t = f"BTC POMPALIYOR {btc_pct:+.1f}%"
            b = f"BTC ${btc_price:,.0f} | Firsat yaklasiyor"
            haberler = get_news_headlines("Bitcoin rally reasons")
            if haberler:
                b += format_news_for_push(haberler)
            send_push(t, b, "BTC_PUMP")
            save_event("CRYPTO", "BTC_PUMP", btc_pct, t, b, "GREEN")
            crypto_new = "GREEN"
            
        elif _last_status["crypto"] == "RED" and btc_pct >= BTC_RECOVER:
            crypto_new = "GREEN"
            if should_push("btc_recover"):
                t = f"BTC TOPARLADI {btc_pct:+.1f}%"
                b = f"BTC ${btc_price:,.0f} | Kripto alimlar aktif"
                haberler = get_news_headlines("Bitcoin recovery news")
                if haberler:
                    b += format_news_for_push(haberler)
                send_push(t, b, "BTC_RECOVER")
                save_event("CRYPTO", "BTC_RECOVER", btc_pct, t, b, "GREEN")

        update_market_status(crypto_status=crypto_new, btc_change_1h=btc_pct)
        _last_status["crypto"] = crypto_new

    # ==================== S&P500 ====================
    # Borsa açıkken gerçek veri, kapalıyken futures + VIX + haberler
    spy_pct, spy_price = get_spy_change()
    vix = get_vix()
    
    if spy_pct is not None:
        print(f"  SPY: ${spy_price:.2f} | Gun: {spy_pct:+.2f}%")
    
    if vix is not None:
        print(f"  VIX: {vix:.1f}")
    
    # ⭐ BORSA KAPALIYSA (20:00-13:00 UTC) PRE-MARKET ANALİZİ
    us_new = _last_status["us"]
    premarket_yon = ""
    premarket_status = "GREEN"
    
    if not (13 <= hour < 20):
        yon, status, skor = analyze_premarket_with_real_data()
        premarket_yon = yon
        premarket_status = status
        
        # Veritabanına kaydet
        update_market_status(us_premarket_skor=skor, us_premarket_yon=yon)
    
    # Alarm kontrolü (borsa açıkken)
    if 13 <= hour < 20:
        if (spy_pct and spy_pct <= SP500_CRASH) or (vix and vix >= VIX_EXTREME):
            us_new = "RED"
        elif (spy_pct and spy_pct <= SP500_ALARM_DROP) or (vix and vix >= VIX_HIGH):
            us_new = "YELLOW"
        else:
            us_new = "GREEN"

        if us_new != _last_status["us"]:
            emoji = "🔴" if us_new == "RED" else "⚠️" if us_new == "YELLOW" else "🟢"
            t = f"{emoji} US PIYASA {'ALARM' if us_new=='RED' else 'UYARI' if us_new=='YELLOW' else 'NORMALE DONDU'}"
            b = f"S&P500: {spy_pct:+.1f}% | VIX: {vix:.0f}" if spy_pct and vix else ""
            
            if us_new in ["RED", "YELLOW"]:
                haberler = get_news_headlines("S&P500 market crash reasons")
                if haberler:
                    b += format_news_for_push(haberler)
            
            if should_push(f"us_{us_new.lower()}"):
                send_push(t, b, f"US_{us_new}")
                save_event("US", us_new, spy_pct or 0, t, b, us_new)
            
            update_market_status(us_status=us_new, spy_change_1d=spy_pct or 0, vix=vix or 0)
            _last_status["us"] = us_new
        elif spy_pct is not None:
            update_market_status(spy_change_1d=spy_pct, vix=vix or 0)
    
    # ==================== BIST ====================
    if 7 <= hour < 15:
        bist_pct, bist_price = get_bist100_change()
        if bist_pct is not None:
            print(f"  BIST100: {bist_price:,.0f} | Gun: {bist_pct:+.2f}%")
            bist_new = _last_status["bist"]

            if bist_pct <= BIST_ALARM_DROP:
                bist_new = "RED"
                if _last_status["bist"] != "RED" and should_push("bist_drop"):
                    t = f"BIST DUSUYOR {bist_pct:+.1f}%"
                    b = f"BIST100: {bist_price:,.0f} | BIST alimlari durduruldu"
                    haberler = get_news_headlines("BIST100 Turkey market drop")
                    if haberler:
                        b += format_news_for_push(haberler)
                    send_push(t, b, "BIST_DROP")
                    save_event("BIST", "DROP", bist_pct, t, b, "RED")
                    
            elif _last_status["bist"] == "RED" and bist_pct >= BIST_RECOVER:
                bist_new = "GREEN"
                if should_push("bist_recover"):
                    t = f"BIST TOPARLADI {bist_pct:+.1f}%"
                    b = f"BIST100: {bist_price:,.0f} | BIST alimlari aktif"
                    haberler = get_news_headlines("BIST100 Turkey market recovery")
                    if haberler:
                        b += format_news_for_push(haberler)
                    send_push(t, b, "BIST_RECOVER")
                    save_event("BIST", "RECOVER", bist_pct, t, b, "GREEN")

            update_market_status(bist_status=bist_new, bist_change_1d=bist_pct)
            _last_status["bist"] = bist_new
    else:
        # BIST kapalıyken haberleri göster
        print("  📰 BIST KAPALI - Pre-market haberler:")
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
            print(f"Makro hata: {e}")
        time.sleep(60)

if __name__ == "__main__":
    main()
