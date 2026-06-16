# -*- coding: utf-8 -*-
"""
Atlas Makro Piyasa İzleme Sistemi
BTC, S&P500, BIST100 referanslarini 60sn'de bir kontrol eder.
Google News RSS ile gerçek zamanlı haber çeker.
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
GROQ_KEY = os.environ.get("GROQ_API_KEY")
FIREBASE_SERVICE_ACCOUNT = os.environ.get("FIREBASE_SERVICE_ACCOUNT")

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
# GOOGLE NEWS RSS - HABER ÇEK (YENİ)
# ============================================================

def get_news_headlines(query, limit=3):
    """
    Google News RSS'den gerçek haber başlıkları çek
    
    Args:
        query: Aranacak kelime (örn: "Bitcoin crash")
        limit: Kaç haber başlığı çekilecek (varsayılan: 3)
    
    Returns:
        list: Haber başlıkları listesi
    """
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
                # Başlığı temizle
                temiz_baslik = title.text.strip()
                if len(temiz_baslik) > 100:
                    temiz_baslik = temiz_baslik[:97] + "..."
                haberler.append(temiz_baslik)
        
        return haberler
        
    except Exception as e:
        print(f"Haber hatasi: {e}")
        return []

def format_news_for_push(haberler, market, change_pct):
    """
    Haberleri push mesajı formatına çevir
    
    Args:
        haberler: Haber başlıkları listesi
        market: Piyasa adı
        change_pct: Değişim yüzdesi
    
    Returns:
        str: Formatlanmış haber metni
    """
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
# VERİ ÇEKME FONKSİYONLARI
# ============================================================

def get_btc_change():
    for url, params in [
        ("https://api.mexc.com/api/v3/klines",
         {"symbol": "BTCUSDT", "interval": "1h", "limit": 2}),
        ("https://api.gateio.ws/api/v4/spot/candlesticks",
         {"currency_pair": "BTC_USDT", "interval": "1h", "limit": 2}),
    ]:
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=8)
            data = r.json()
            if len(data) >= 2:
                if "symbol" in url or "mexc" in url:
                    prev, curr = float(data[0][4]), float(data[1][4])
                else:
                    prev, curr = float(data[0][2]), float(data[1][2])
                return ((curr - prev) / prev) * 100, curr
        except Exception:
            continue
    return None, None

def get_spy_change():
    """S&P500 gunluk degisim — Finnhub REST veya MEXC SPX."""
    finnhub_key = os.environ.get("FINNHUB_KEY", "")
    if finnhub_key:
        try:
            r = requests.get(
                f"https://finnhub.io/api/v1/quote?symbol=SPY&token={finnhub_key}",
                headers=HEADERS, timeout=8)
            if r.status_code == 200:
                d = r.json()
                price = d.get("c", 0)
                prev  = d.get("pc", 0)
                if price and prev:
                    return ((price - prev) / prev) * 100, price
        except Exception:
            pass

    try:
        r = requests.get(
            "https://api.mexc.com/api/v3/ticker/24hr?symbol=SPXUSDT",
            headers=HEADERS, timeout=8)
        if r.status_code == 200:
            d = r.json()
            price = float(d.get("lastPrice", 0) or 0)
            prev  = float(d.get("prevClosePrice", 0) or 0)
            if price and prev:
                pct = ((price - prev) / prev) * 100
                return pct, price
    except Exception:
        pass

    return None, None

def get_vix():
    """VIX — Finnhub REST."""
    finnhub_key = os.environ.get("FINNHUB_KEY", "")
    if not finnhub_key:
        return None
    try:
        r = requests.get(
            f"https://finnhub.io/api/v1/quote?symbol=^VIX&token={finnhub_key}",
            headers=HEADERS, timeout=8)
        if r.status_code == 200:
            vix = r.json().get("c", 0)
            return float(vix) if vix else None
    except Exception:
        pass
    return None

def get_bist100_change():
    """BIST100 — Yahoo endpoint."""
    try:
        r = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/XU100.IS",
            params={"interval": "1d", "range": "2d"},
            headers=HEADERS, timeout=8)
        meta = r.json()["chart"]["result"][0]["meta"]
        price = meta.get("regularMarketPrice", 0)
        prev = meta.get("previousClose", 0)
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
# ANA KONTROL
# ============================================================

def check_all():
    now_utc = datetime.now(timezone.utc)
    hour = now_utc.hour
    print(f"\n Makro kontrol {now_utc.strftime('%H:%M UTC')}")

    # ==========================================================
    # BTC
    # ==========================================================
    btc_pct, btc_price = get_btc_change()
    if btc_pct is not None:
        print(f"  BTC: ${btc_price:,.0f} | 1s: {btc_pct:+.2f}%")
        crypto_new = _last_status["crypto"]

        if btc_pct <= BTC_ALARM_DROP:
            crypto_new = "RED"
            if _last_status["crypto"] != "RED" and should_push("btc_drop"):
                t = f"BTC COKUYOR {btc_pct:+.1f}%"
                b = f"BTC ${btc_price:,.0f} | Kripto alimlar durduruldu"
                
                # ⭐ HABER EKLE
                haberler = get_news_headlines("Bitcoin crash reasons", 3)
                if haberler:
                    b += format_news_for_push(haberler, "Bitcoin", btc_pct)
                
                send_push(t, b, "BTC_DROP")
                save_event("CRYPTO", "BTC_DROP", btc_pct, t, b, "RED")
                
        elif btc_pct >= BTC_ALARM_PUMP and should_push("btc_pump"):
            t = f"BTC POMPALIYOR {btc_pct:+.1f}%"
            b = f"BTC ${btc_price:,.0f} | Firsat yaklasiyor"
            
            # ⭐ HABER EKLE
            haberler = get_news_headlines("Bitcoin rally reasons", 3)
            if haberler:
                b += format_news_for_push(haberler, "Bitcoin", btc_pct)
            
            send_push(t, b, "BTC_PUMP")
            save_event("CRYPTO", "BTC_PUMP", btc_pct, t, b, "GREEN")
            crypto_new = "GREEN"
            
        elif _last_status["crypto"] == "RED" and btc_pct >= BTC_RECOVER:
            crypto_new = "GREEN"
            if should_push("btc_recover"):
                t = f"BTC TOPARLADI {btc_pct:+.1f}%"
                b = f"BTC ${btc_price:,.0f} | Kripto alimlar aktif"
                
                # ⭐ HABER EKLE
                haberler = get_news_headlines("Bitcoin recovery news", 3)
                if haberler:
                    b += format_news_for_push(haberler, "Bitcoin", btc_pct)
                
                send_push(t, b, "BTC_RECOVER")
                save_event("CRYPTO", "BTC_RECOVER", btc_pct, t, b, "GREEN")

        update_market_status(crypto_status=crypto_new, btc_change_1h=btc_pct)
        _last_status["crypto"] = crypto_new

    # ==========================================================
    # S&P500 + VIX (borsa saatlerinde)
    # ==========================================================
    if 13 <= hour < 20:
        spy_pct, spy_price = get_spy_change()
        vix = get_vix()
        if spy_pct is not None:
            print(f"  SPY: ${spy_price:.2f} | Gun: {spy_pct:+.2f}%")
        if vix is not None:
            print(f"  VIX: {vix:.1f}")

        us_new = "GREEN"
        if (spy_pct and spy_pct <= SP500_CRASH) or (vix and vix >= VIX_EXTREME):
            us_new = "RED"
        elif (spy_pct and spy_pct <= SP500_ALARM_DROP) or (vix and vix >= VIX_HIGH):
            us_new = "YELLOW"

        if us_new != _last_status["us"]:
            emoji = "🔴" if us_new == "RED" else "⚠️" if us_new == "YELLOW" else "🟢"
            t = f"{emoji} US PIYASA {'ALARM' if us_new=='RED' else 'UYARI' if us_new=='YELLOW' else 'NORMALE DONDU'}"
            b = f"S&P500: {spy_pct:+.1f}% | VIX: {vix:.0f}" if spy_pct and vix else ""
            
            # ⭐ HABER EKLE (sadece RED veya YELLOW ise)
            if us_new in ["RED", "YELLOW"]:
                haberler = get_news_headlines("S&P500 market crash reasons", 3)
                if haberler:
                    b += format_news_for_push(haberler, "S&P500", spy_pct or 0)
            
            if should_push(f"us_{us_new.lower()}"):
                send_push(t, b, f"US_{us_new}")
                save_event("US", us_new, spy_pct or 0, t, b, us_new)
            update_market_status(us_status=us_new,
                                  spy_change_1d=spy_pct or 0,
                                  vix=vix or 0)
            _last_status["us"] = us_new
        elif spy_pct is not None:
            update_market_status(spy_change_1d=spy_pct, vix=vix or 0)

    # ==========================================================
    # BIST100 (borsa saatlerinde)
    # ==========================================================
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
                    
                    # ⭐ HABER EKLE
                    haberler = get_news_headlines("BIST100 Turkey market drop reasons", 3)
                    if haberler:
                        b += format_news_for_push(haberler, "BIST100", bist_pct)
                    
                    send_push(t, b, "BIST_DROP")
                    save_event("BIST", "DROP", bist_pct, t, b, "RED")
                    
            elif _last_status["bist"] == "RED" and bist_pct >= BIST_RECOVER:
                bist_new = "GREEN"
                if should_push("bist_recover"):
                    t = f"BIST TOPARLADI {bist_pct:+.1f}%"
                    b = f"BIST100: {bist_price:,.0f} | BIST alimlari aktif"
                    
                    # ⭐ HABER EKLE
                    haberler = get_news_headlines("BIST100 Turkey market recovery", 3)
                    if haberler:
                        b += format_news_for_push(haberler, "BIST100", bist_pct)
                    
                    send_push(t, b, "BIST_RECOVER")
                    save_event("BIST", "RECOVER", bist_pct, t, b, "GREEN")

            update_market_status(bist_status=bist_new, bist_change_1d=bist_pct)
            _last_status["bist"] = bist_new

# ============================================================
# ANA DÖNGÜ
# ============================================================

def main():
    print("Atlas Makro Izleme Sistemi baslatildi")
    print("Google News RSS ile gerçek zamanlı haber entegre edildi")
    update_market_status(crypto_status="GREEN", us_status="GREEN",
                          bist_status="GREEN", btc_change_1h=0,
                          spy_change_1d=0, bist_change_1d=0, vix=20)
    while True:
        try:
            check_all()
        except Exception as e:
            print(f"Makro hata: {e}")
        time.sleep(60)

if __name__ == "__main__":
    main()
