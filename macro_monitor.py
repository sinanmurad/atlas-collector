# -*- coding: utf-8 -*-
"""
Atlas Makro Piyasa İzleme Sistemi — V2
=======================================
5 Katmanlı Premarket Yön Sistemi:
1. S&P500 Futures (ES=F) — %35 ağırlık
2. Asya Piyasaları (Nikkei, HSI, KOSPI) — %20 ağırlık  
3. Kripto (BTC + ETH) — %20 ağırlık
4. VIX Korku Endeksi — %15 ağırlık
5. ABD 10Y Tahvil — %10 ağırlık

Tüm veriler gerçek zamanlı, kaynak belli, halüsinasyon yok.
"""

import os, time, json, requests
from datetime import datetime, timezone
from supabase import create_client
import firebase_admin
from firebase_admin import credentials, messaging

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
FIREBASE_SERVICE_ACCOUNT = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
FINNHUB_KEY = os.environ.get("FINNHUB_KEY", "")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}

# ── Alarm eşikleri ──────────────────────────────────────────
BTC_ALARM_DROP   = -3.0
BTC_ALARM_PUMP   =  5.0
SP500_CRASH      = -2.5
SP500_WARN       = -1.5
BIST_ALARM_DROP  = -2.0
VIX_HIGH         = 25
VIX_EXTREME      = 30

# ── Firebase ────────────────────────────────────────────────
try:
    if FIREBASE_SERVICE_ACCOUNT:
        cred = credentials.Certificate(json.loads(FIREBASE_SERVICE_ACCOUNT))
        firebase_admin.initialize_app(cred)
        print("✅ Firebase baslatildi")
except Exception as e:
    print(f"Firebase hatasi: {e}")


def send_push(title, body, event_type="MACRO"):
    try:
        profiles = supabase.table("profiles").select("fcm_token")\
            .not_.is_("fcm_token", "null").execute()
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
        print(f"📱 Push: {title}")
    except Exception as e:
        print(f"Push hatasi: {e}")


def save_event(market, event_type, value, title, body, status):
    try:
        supabase.table("macro_events").insert({
            "market": market, "event_type": event_type,
            "value": round(float(value), 2), "title": title,
            "body": body, "status": status,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as e:
        print(f"save_event hatasi: {e}")


def update_market_status(**kwargs):
    try:
        update = {"id": 1, "updated_at": datetime.now(timezone.utc).isoformat()}
        for k, v in kwargs.items():
            if v is None:
                continue
            update[k] = round(float(v), 4) if isinstance(v, float) else v
        supabase.table("market_status").upsert(update, on_conflict="id").execute()
    except Exception as e:
        print(f"update_market_status hatasi: {e}")


# ============================================================
# VERİ ÇEKME FONKSİYONLARI — Hepsi gerçek, kaynak belli
# ============================================================

def yahoo_chart(symbol, interval="1d", range_="2d"):
    """Yahoo Finance chart API — TR collector'da zaten çalışıyor."""
    try:
        r = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
            params={"interval": interval, "range": range_},
            headers=HEADERS, timeout=10
        )
        if r.status_code == 200:
            return r.json()["chart"]["result"][0]
    except Exception:
        pass
    return None


def get_price_change(symbol, min_price=0):
    """Günlük % değişim ve anlık fiyat."""
    data = yahoo_chart(symbol)
    if not data:
        return None, None
    meta = data["meta"]
    price = meta.get("regularMarketPrice", 0)
    prev  = meta.get("chartPreviousClose") or meta.get("previousClose", 0)
    if price and prev and price > min_price:
        return ((price - prev) / prev) * 100, price
    return None, None


def get_btc_change():
    """BTC 1 saatlik değişim — MEXC (Railway'de kesin çalışıyor)."""
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
                prev = float(data[0][4] if "mexc" in url else data[0][2])
                curr = float(data[1][4] if "mexc" in url else data[1][2])
                return ((curr - prev) / prev) * 100, curr
        except Exception:
            continue
    return None, None


def get_eth_change():
    """ETH 1 saatlik değişim — MEXC."""
    try:
        r = requests.get(
            "https://api.mexc.com/api/v3/klines",
            params={"symbol": "ETHUSDT", "interval": "1h", "limit": 2},
            headers=HEADERS, timeout=8
        )
        data = r.json()
        if len(data) >= 2:
            prev, curr = float(data[0][4]), float(data[1][4])
            return ((curr - prev) / prev) * 100, curr
    except Exception:
        pass
    return None, None


def get_spy_futures():
    """S&P500 Futures (ES=F) — en güvenilir premarket göstergesi."""
    pct, price = get_price_change("ES%3DF", min_price=1000)
    return pct, price


def get_spy_cash():
    """S&P500 nakit (^GSPC) — borsa açıkken kullan."""
    pct, price = get_price_change("%5EGSPC", min_price=1000)
    return pct, price


def get_nasdaq_futures():
    """Nasdaq Futures (NQ=F)."""
    pct, price = get_price_change("NQ%3DF", min_price=1000)
    return pct, price


def get_vix():
    """VIX korku endeksi."""
    # Önce Finnhub (anlık)
    if FINNHUB_KEY:
        try:
            r = requests.get(
                f"https://finnhub.io/api/v1/quote?symbol=^VIX&token={FINNHUB_KEY}",
                headers=HEADERS, timeout=8)
            if r.status_code == 200:
                v = r.json().get("c", 0)
                if v: return float(v)
        except Exception:
            pass
    # Fallback: Yahoo Finance
    _, price = get_price_change("%5EVIX", min_price=0)
    return price


def get_us10y():
    """ABD 10 Yıllık Tahvil Faizi (^TNX)."""
    _, price = get_price_change("%5ETNX", min_price=0)
    return price


def get_nikkei():
    """Nikkei 225 (^N225) — Asya piyasası."""
    pct, price = get_price_change("%5EN225", min_price=1000)
    return pct, price


def get_hsi():
    """Hang Seng Index (^HSI) — Asya piyasası."""
    pct, price = get_price_change("%5EHSI", min_price=1000)
    return pct, price


def get_kospi():
    """KOSPI (^KS11) — Güney Kore piyasası."""
    pct, price = get_price_change("%5EKS11", min_price=100)
    return pct, price


def get_bist100():
    """BIST100 (XU100.IS)."""
    pct, price = get_price_change("XU100.IS", min_price=100)
    return pct, price


def get_dxy():
    """Dolar Endeksi (DX-Y.NYB) — güçlü dolar = hisse baskısı."""
    pct, price = get_price_change("DX-Y.NYB", min_price=80)
    return pct, price


def get_gold():
    """Altın Futures (GC=F) — risk iştahı göstergesi."""
    pct, price = get_price_change("GC%3DF", min_price=100)
    return pct, price


# ============================================================
# PREMARKET YÖN HESAPLAYICI — 5 Katmanlı Ağırlıklı Skor
# ============================================================

def calculate_premarket_direction():
    """
    Gerçek verilerle premarket yön hesapla.
    
    Ağırlıklar (kurumsal yatırımcıların kullandığı sırayla):
    1. S&P500 Futures: %35 — en doğrudan gösterge
    2. Asya Piyasaları: %20 — küresel risk iştahı
    3. Kripto (BTC+ETH): %20 — risk iştahı öncü göstergesi
    4. VIX: %15 — korku/açgözlülük
    5. ABD 10Y + DXY: %10 — makroekonomik baskı
    
    Skor: -10 (güçlü düşüş) → +10 (güçlü yükseliş)
    """
    
    skor = 0.0
    detaylar = []
    uyarilar = []
    
    print("\n  📊 5 KATMANLI PREMARKET ANALİZİ:")
    print("  " + "─" * 45)
    
    # ── KATMAN 1: S&P500 FUTURES (%35) ──────────────────
    futures_pct, futures_price = get_spy_futures()
    nq_pct, _ = get_nasdaq_futures()
    
    if futures_pct is not None:
        print(f"  [1] S&P500 Futures: {futures_pct:+.2f}% @ {futures_price:,.0f}")
        if futures_pct >= 1.0:
            skor += 3.5
            detaylar.append(f"🟢 S&P500 Futures {futures_pct:+.2f}% — güçlü yükseliş sinyali")
        elif futures_pct >= 0.3:
            skor += 1.5
            detaylar.append(f"🟢 S&P500 Futures {futures_pct:+.2f}% — hafif pozitif")
        elif futures_pct <= -1.0:
            skor -= 3.5
            detaylar.append(f"🔴 S&P500 Futures {futures_pct:+.2f}% — güçlü düşüş sinyali")
            uyarilar.append(f"S&P500 Futures kritik düşüşte: {futures_pct:+.2f}%")
        elif futures_pct <= -0.3:
            skor -= 1.5
            detaylar.append(f"🔴 S&P500 Futures {futures_pct:+.2f}% — negatif baskı")
        else:
            detaylar.append(f"🟡 S&P500 Futures {futures_pct:+.2f}% — nötr")
    else:
        print("  [1] S&P500 Futures: ❌ veri yok")

    if nq_pct is not None:
        print(f"       Nasdaq Futures: {nq_pct:+.2f}%")
        if abs(nq_pct) > abs(futures_pct or 0) * 1.5:
            # Nasdaq divergans — teknoloji öne çekiyor veya geri kalıyor
            if nq_pct > 0:
                skor += 0.5
                detaylar.append(f"💡 Nasdaq liderlik ediyor: {nq_pct:+.2f}%")
            else:
                skor -= 0.5

    # ── KATMAN 2: ASYA PİYASALARI (%20) ─────────────────
    nikkei_pct, nikkei_p = get_nikkei()
    hsi_pct, hsi_p       = get_hsi()
    kospi_pct, _         = get_kospi()
    
    asya_pctler = [p for p in [nikkei_pct, hsi_pct, kospi_pct] if p is not None]
    if asya_pctler:
        asya_ort = sum(asya_pctler) / len(asya_pctler)
        print(f"  [2] Asya Ort: {asya_ort:+.2f}%", end="")
        if nikkei_pct: print(f" | Nikkei: {nikkei_pct:+.2f}%", end="")
        if hsi_pct:    print(f" | HSI: {hsi_pct:+.2f}%", end="")
        print()
        
        if asya_ort >= 1.0:
            skor += 2.0
            detaylar.append(f"🟢 Asya piyasaları güçlü: ort {asya_ort:+.2f}%")
        elif asya_ort >= 0.3:
            skor += 0.8
            detaylar.append(f"🟢 Asya piyasaları pozitif: ort {asya_ort:+.2f}%")
        elif asya_ort <= -1.0:
            skor -= 2.0
            detaylar.append(f"🔴 Asya piyasaları düştü: ort {asya_ort:+.2f}%")
            uyarilar.append(f"Asya piyasaları negatif kapandı: {asya_ort:+.2f}%")
        elif asya_ort <= -0.3:
            skor -= 0.8
            detaylar.append(f"🔴 Asya piyasaları zayıf: ort {asya_ort:+.2f}%")
        else:
            detaylar.append(f"🟡 Asya piyasaları nötr: {asya_ort:+.2f}%")
    else:
        print("  [2] Asya piyasaları: ❌ veri yok")

    # ── KATMAN 3: KRİPTO (BTC + ETH) (%20) ──────────────
    btc_pct, btc_price = get_btc_change()
    eth_pct, eth_price = get_eth_change()
    
    kripto_skorlari = []
    if btc_pct is not None:
        print(f"  [3] BTC: {btc_pct:+.2f}% @ ${btc_price:,.0f}", end="")
        kripto_skorlari.append(btc_pct)
    if eth_pct is not None:
        print(f" | ETH: {eth_pct:+.2f}%", end="")
        kripto_skorlari.append(eth_pct)
    if kripto_skorlari:
        print()
        kripto_ort = sum(kripto_skorlari) / len(kripto_skorlari)
        if kripto_ort >= 2.0:
            skor += 2.0
            detaylar.append(f"🟢 Kripto güçlü: BTC {btc_pct:+.2f}% — risk iştahı yüksek")
        elif kripto_ort >= 0.5:
            skor += 0.8
            detaylar.append(f"🟢 Kripto pozitif: BTC {btc_pct:+.2f}%")
        elif kripto_ort <= -3.0:
            skor -= 2.0
            detaylar.append(f"🔴 Kripto çöküyor: BTC {btc_pct:+.2f}% — risk iştahı düştü")
            uyarilar.append(f"BTC kritik düşüşte: {btc_pct:+.2f}%")
        elif kripto_ort <= -1.0:
            skor -= 0.8
            detaylar.append(f"🔴 Kripto zayıf: BTC {btc_pct:+.2f}%")
        else:
            detaylar.append(f"🟡 Kripto nötr: BTC {btc_pct:+.2f}%")
    else:
        print()

    # ── KATMAN 4: VIX (%15) ──────────────────────────────
    vix = get_vix()
    if vix is not None:
        print(f"  [4] VIX: {vix:.1f}", end="")
        if vix < 15:
            skor += 1.5
            detaylar.append(f"🟢 VIX {vix:.1f} — piyasa çok sakin, düşük risk")
            print(" (SAKIN)")
        elif vix < 20:
            skor += 0.5
            detaylar.append(f"🟢 VIX {vix:.1f} — normal seviye")
            print(" (NORMAL)")
        elif vix < 25:
            detaylar.append(f"🟡 VIX {vix:.1f} — hafif gergin")
            print(" (DİKKAT)")
        elif vix < 30:
            skor -= 1.5
            detaylar.append(f"🔴 VIX {vix:.1f} — yüksek korku")
            uyarilar.append(f"VIX yüksek: {vix:.1f} — piyasada korku hakim")
            print(" (YÜKSEK KORKU)")
        else:
            skor -= 3.0
            detaylar.append(f"🔴 VIX {vix:.1f} — AŞIRI KORKU, kriz seviyesi")
            uyarilar.append(f"VIX KRİZ SEVİYESİ: {vix:.1f}")
            print(" (KRİZ)")
    else:
        print("  [4] VIX: ❌ veri yok")

    # ── KATMAN 5: ABD 10Y + DXY (%10) ───────────────────
    us10y    = get_us10y()
    dxy_pct, dxy_p = get_dxy()
    gold_pct, _    = get_gold()
    
    print(f"  [5]", end="")
    if us10y:
        print(f" 10Y: %{us10y:.2f}", end="")
        if us10y > 4.5:
            skor -= 1.0
            detaylar.append(f"🔴 ABD 10Y %{us10y:.2f} — yüksek faiz hisse baskısı")
        elif us10y < 4.0:
            skor += 0.5
            detaylar.append(f"🟢 ABD 10Y %{us10y:.2f} — faiz baskısı düşük")
        else:
            detaylar.append(f"🟡 ABD 10Y %{us10y:.2f} — orta seviye")

    if dxy_pct is not None:
        print(f" | DXY: {dxy_pct:+.2f}%", end="")
        if dxy_pct >= 0.5:
            skor -= 0.5
            detaylar.append(f"🔴 Dolar güçleniyor ({dxy_pct:+.2f}%) — gelişen piyasa baskısı")
        elif dxy_pct <= -0.5:
            skor += 0.5
            detaylar.append(f"🟢 Dolar zayıflıyor ({dxy_pct:+.2f}%) — risk iştahı artıyor")

    if gold_pct is not None:
        print(f" | Altın: {gold_pct:+.2f}%", end="")
        if gold_pct >= 1.0 and (dxy_pct or 0) <= 0:
            # Altın yükseliyor ama dolar düşmüyor = güvenli liman talebi = risk kapalı
            skor -= 0.5
            detaylar.append(f"⚠️ Altın {gold_pct:+.2f}% — güvenli liman talebi var")
    print()

    # ── SONUÇ ────────────────────────────────────────────
    print("  " + "─" * 45)
    skor = max(-10, min(10, skor))

    if skor >= 4:
        yon = "🟢 GÜÇLÜ YÜKSELİŞ BEKLENİYOR"
        status = "GREEN"
        renk = "🟢"
    elif skor >= 1.5:
        yon = "🟢 HAFİF YÜKSELİŞ BEKLENİYOR"
        status = "GREEN"
        renk = "🟢"
    elif skor <= -4:
        yon = "🔴 GÜÇLÜ DÜŞÜŞ BEKLENİYOR"
        status = "RED"
        renk = "🔴"
    elif skor <= -1.5:
        yon = "🔴 HAFİF DÜŞÜŞ BEKLENİYOR"
        status = "RED"
        renk = "🔴"
    else:
        yon = "🟡 NÖTR / BELİRSİZ"
        status = "YELLOW"
        renk = "🟡"

    print(f"\n  🎯 YÖN: {yon}")
    print(f"  📊 SKOR: {skor:+.1f} / 10")
    print(f"\n  DETAYLAR:")
    for d in detaylar:
        print(f"    {d}")

    # Supabase'e kaydet
    detay_str = " | ".join(detaylar[:4])  # ilk 4 detay
    update_market_status(
        us_premarket_skor=round(skor, 1),
        us_premarket_yon=yon,
        us_premarket_detay=detay_str,
    )

    return yon, status, skor, detaylar, uyarilar


# ============================================================
# ANA KONTROL
# ============================================================

_last_status = {"crypto": "GREEN", "us": "GREEN", "bist": "GREEN"}
_last_premarket_status = "YELLOW"
_push_cooldown = {}


def should_push(key, minutes=15):
    now = time.time()
    if now - _push_cooldown.get(key, 0) > minutes * 60:
        _push_cooldown[key] = now
        return True
    return False


def check_all():
    now_utc = datetime.now(timezone.utc)
    hour    = now_utc.hour
    print(f"\n📡 Makro kontrol {now_utc.strftime('%H:%M UTC')}")

    # ── BTC (her zaman izle) ─────────────────────────────
    btc_pct, btc_price = get_btc_change()
    if btc_pct is not None:
        print(f"  BTC: ${btc_price:,.0f} | 1s: {btc_pct:+.2f}%")
        update_market_status(btc_change_1h=btc_pct, crypto_status=_last_status["crypto"])

        if btc_pct <= BTC_ALARM_DROP and should_push("btc_drop"):
            t = f"🔴 BTC ÇÖKÜYOR {btc_pct:+.1f}%"
            b = (f"BTC ${btc_price:,.0f}\n"
                 f"Kripto alımları durduruldu\n"
                 f"VIX: {get_vix() or '?'} | S&P Futures kontrol edilsin")
            send_push(t, b, "BTC_DROP")
            save_event("CRYPTO", "BTC_DROP", btc_pct, t, b, "RED")
            update_market_status(crypto_status="RED")
            _last_status["crypto"] = "RED"

        elif btc_pct >= BTC_ALARM_PUMP and should_push("btc_pump"):
            t = f"🚀 BTC POMPALIYOR {btc_pct:+.1f}%"
            b = f"BTC ${btc_price:,.0f} | Risk iştahı yüksek"
            send_push(t, b, "BTC_PUMP")
            save_event("CRYPTO", "BTC_PUMP", btc_pct, t, b, "GREEN")
            update_market_status(crypto_status="GREEN")
            _last_status["crypto"] = "GREEN"

        elif _last_status["crypto"] == "RED" and btc_pct >= 1.0 and should_push("btc_recover"):
            t = f"✅ BTC TOPARLIYOR {btc_pct:+.1f}%"
            b = f"BTC ${btc_price:,.0f} | Kripto alımları tekrar aktif"
            send_push(t, b, "BTC_RECOVER")
            save_event("CRYPTO", "BTC_RECOVER", btc_pct, t, b, "GREEN")
            update_market_status(crypto_status="GREEN")
            _last_status["crypto"] = "GREEN"

    # ── US BORSASI AÇIKKEN ───────────────────────────────
    if 13 <= hour < 20:
        spy_pct, spy_price = get_spy_cash()
        vix = get_vix()
        if spy_pct is not None:
            print(f"  S&P500: {spy_price:,.0f} | Gün: {spy_pct:+.2f}%")
        if vix is not None:
            print(f"  VIX: {vix:.1f}")
        update_market_status(
            spy_change_1d=spy_pct or 0,
            vix=vix or 0,
        )
        us_new = "GREEN"
        if (spy_pct and spy_pct <= SP500_CRASH) or (vix and vix >= VIX_EXTREME):
            us_new = "RED"
        elif (spy_pct and spy_pct <= SP500_WARN) or (vix and vix >= VIX_HIGH):
            us_new = "YELLOW"

        if us_new != _last_status["us"]:
            if us_new == "RED" and should_push("us_crash"):
                t = f"🔴 US PİYASA ALARM"
                b = (f"S&P500: {spy_pct:+.1f}% | VIX: {vix:.0f}\n"
                     f"US alımları durduruldu")
                send_push(t, b, "US_CRASH")
                save_event("US", "CRASH", spy_pct or 0, t, b, "RED")
            elif us_new == "GREEN" and _last_status["us"] in ("RED", "YELLOW"):
                if should_push("us_recover"):
                    t = "✅ US PİYASA TOPARLIYOR"
                    b = f"S&P500: {spy_pct:+.1f}% | Alımlar aktif"
                    send_push(t, b, "US_RECOVER")
            update_market_status(us_status=us_new)
            _last_status["us"] = us_new

    # ── PREMARKET / GECE ANALİZİ ─────────────────────────
    # US kapalıyken (20:00-13:29 UTC) yön analizi yap
    else:
        global _last_premarket_status
        yon, status, skor, detaylar, uyarilar = calculate_premarket_direction()

        # Kritik değişimde push gönder
        if status != _last_premarket_status:
            if status == "RED" and should_push("premarket_red"):
                t = "🔴 PREMARKET UYARISI"
                b = f"{yon}\nSkor: {skor:+.1f}/10\n" + "\n".join(detaylar[:3])
                send_push(t, b, "PREMARKET_RED")
                save_event("US", "PREMARKET_RED", skor, t, b, "RED")
            elif status == "GREEN" and _last_premarket_status == "RED":
                if should_push("premarket_green"):
                    t = "🟢 PREMARKET POZİTİF"
                    b = f"{yon}\nSkor: {skor:+.1f}/10\n" + "\n".join(detaylar[:3])
                    send_push(t, b, "PREMARKET_GREEN")
            _last_premarket_status = status

        update_market_status(
            us_status=status,
            us_premarket_skor=round(skor, 1),
            us_premarket_yon=yon,
        )

    # ── BIST AÇIKKEN ─────────────────────────────────────
    if 7 <= hour < 15:
        bist_pct, bist_price = get_bist100()
        if bist_pct is not None:
            print(f"  BIST100: {bist_price:,.0f} | Gün: {bist_pct:+.2f}%")
            update_market_status(bist_change_1d=bist_pct)

            if bist_pct <= BIST_ALARM_DROP:
                bist_new = "RED"
                if should_push("bist_drop"):
                    t = f"🔴 BIST DÜŞÜYOR {bist_pct:+.1f}%"
                    b = f"BIST100: {bist_price:,.0f} | Alımlar durduruldu"
                    send_push(t, b, "BIST_DROP")
                    save_event("BIST", "DROP", bist_pct, t, b, "RED")
            elif _last_status["bist"] == "RED" and bist_pct >= 0.5:
                bist_new = "GREEN"
                if should_push("bist_recover"):
                    t = f"✅ BIST TOPARLIYOR {bist_pct:+.1f}%"
                    b = f"BIST100: {bist_price:,.0f} | Alımlar aktif"
                    send_push(t, b, "BIST_RECOVER")
                    save_event("BIST", "RECOVER", bist_pct, t, b, "GREEN")
            else:
                bist_new = "GREEN" if bist_pct >= 0 else "YELLOW"

            if bist_new != _last_status["bist"]:
                update_market_status(bist_status=bist_new)
                _last_status["bist"] = bist_new
    else:
        print("  BIST: Kapalı")


def main():
    print("\n" + "=" * 60)
    print("🚀 ATLAS MAKRO PİYASA İZLEME — V2")
    print("📊 5 Katmanlı Premarket Yön Sistemi")
    print("   Futures | Asya | Kripto | VIX | Tahvil")
    print("   Gerçek veri | Kaynak belli | Halüsinasyon yok")
    print("=" * 60)

    # Supabase kolonlarını güvenli ekle
    try:
        update_market_status(
            crypto_status="GREEN", us_status="GREEN", bist_status="GREEN",
            btc_change_1h=0, spy_change_1d=0, bist_change_1d=0, vix=20,
            us_premarket_skor=0, us_premarket_yon="🟡 NÖTR / BELİRSİZ",
            us_premarket_detay="",
        )
    except Exception:
        pass

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
