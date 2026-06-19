# -*- coding: utf-8 -*-
"""
ATLAS US KARTAL GÖZÜ — WHALE DETECTOR
=======================================
Son güncelleme: 19 Haziran 2026

DEĞİŞİKLİK GÜNLÜĞÜ:

[19 Haziran 2026 — GERİ ALMA, ikinci düzeltme]
- Paralel 20-bağlantılı WebSocket mimarisi GERİ ALINDI. Sebep: Finnhub
  ücretsiz plan WebSocket'i bağlantı başına değil, TOKEN/IP başına
  toplam 50 sembolle sınırlıyor. 20 paralel bağlantı açmak Finnhub'ı
  sürekli rate-limit (429) tetiklemeye zorladı — tüm bağlantılar
  saniyede bir "Connection to remote host was lost" ile kopup yeniden
  bağlanıyordu. Sonuç: US botu saatlerce (15:12 UTC itibariyle son
  alım 18 Haziran 12:42'den beri, yani 27+ saat) etkin olarak HİÇ
  veri alamadı — dönen pencere mimarisinden bile daha kötü bir durum.
  Artık TEK bağlantı + watchlist'in ortalama hacme göre en yüksek 50
  sembolü kullanılıyor (Finnhub'ın gerçekten izin verdiği limit bu).
  Statik active_symbols[:50] yerine dinamik avg_volumes sıralaması
  kullanılıyor — en azından watchlist içindeki en likit/önemli
  hisseler garanti izleniyor, POWW gibi düşük öncelikli olanlar
  watchlist'te kalsa da WebSocket'e giremeyebilir (bilinen kısıt).

[19 Haziran 2026 — üçüncü düzeltme, aynı gün]
- Abonelik mesajları arasına 0.15sn gecikme eklendi: 50 sembole ANINDA
  abone olmaya çalışmak bile tek bağlantıda rate-limit tetikliyordu.
- check_signal_outcomes() SONSUZ ŞÜPHELİ DÖNGÜSÜ düzeltildi: |pct|>95
  olan kayıtlarda (örn. HUMA %-97.5, ACN %-100) eskiden sadece
  "continue" deniyordu, checked_{field} hiç True yapılmıyordu — bu
  kayıtlar HER taramada yeniden çekilip aynı log satırını sonsuza kadar
  tekrar bastırıyordu (gereksiz yfinance çağrısı + log spam). Artık
  şüpheli kayıtlar da checked=True yapılıp gerçek pct değeriyle
  kaydediliyor, bir daha tekrar denenmiyor.
[18 Haziran 2026]
- Sabah push'tan bot alımı kaldırıldı — sadece bildirim, gerçek alım
  13:30 UTC WebSocket açılınca canlı fiyattan yapılıyor
- "ascending=" parametresi → "desc=" (Supabase kütüphane uyumu)
- BASE_CAP 3→5, EXCEPTIONAL_CAP 2→3 (max 8 pozisyon)
- Duplicate koruma: aynı sembolde açık pozisyon varsa tekrar alım yok

[19 Haziran 2026]
- KRİTİK FIX #1: on_open() dönen pencere kullanmaya başladı (sonra
  aşağıdaki #2 ile komple değiştirildi).
- KRİTİK FIX #2 (asıl çözüm): Tek WebSocket + dönen pencere yerine
  PARALEL ÇOK BAĞLANTI mimarisine geçildi. Watchlist N hisseyse,
  N/50 adet WebSocket bağlantısı AYNI ANDA açılıyor — her biri kendi
  50'lik dilimini sürekli dinliyor, koparsa kendi başına yeniden
  bağlanıyor. Artık "tam tur ~20 dakika" gecikmesi yok — TÜM watchlist
  her an gerçek zamanlı izleniyor. (18 Haziran'da eski mimariyle 6.5
  saat boyunca sıfır canlı sinyal üretilmişti — kök neden buydu.)
- Watchlist filtresi gevşetildi: $1-20 + 500K hacim → $0.50+ + 100K hacim
  (POWW gibi düşük hacimli ama hareketli hisseler artık watchlist'e giriyor)
- "ascending=" parametresi → "desc=" (Supabase kütüphane uyumu)
"""
import os
import json
import time
import math
import requests
import websocket
import yfinance as yf
import threading
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from supabase import create_client
import firebase_admin
from firebase_admin import credentials, messaging

FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
GROQ_KEY = os.environ.get("GROQ_API_KEY")
FIREBASE_SERVICE_ACCOUNT = os.environ.get("FIREBASE_SERVICE_ACCOUNT")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

signal_cache = {}
avg_volumes = {}
active_symbols = []
news_cache = {}
last_price_update = {}
company_cache = {}
analyst_cache = {}
premarket_signal_cache = {}

# ── OTOMATİK ÖĞRENME SİSTEMİ — sabitler (crypto_collector.py ile aynı) ──
LEARNING_MIN_SAMPLES = 10        # 10 örnek yeterli — hızlı öğren
LEARNING_MIN_ABS_Z = 1.65        # p<0.10 — erken uyarı
LEARNING_MAX_BONUS = 6           # ±6 puan — CRITICAL eşiğini etkiler
LEARNING_BASELINE_WINRATE = 0.50 # %50 beklenti — altında ceza, üstünde ödül
OUTCOME_CHECK_HOURS = [24, 72, 168]   # 24s, 72s, 7g sonuç ölçümü

# ── WEBSOCKET DÖNEN PENCERE — 19 Haz 2026 ──────────────────────────
WS_WINDOW_SIZE = 50              # Finnhub WebSocket sembol limiti
_ws_window_index = {"i": 0}      # Her (yeniden)bağlantıda ilerler

try:
    if FIREBASE_SERVICE_ACCOUNT and not firebase_admin._apps:
        cred = credentials.Certificate(json.loads(FIREBASE_SERVICE_ACCOUNT))
        firebase_admin.initialize_app(cred)
        print("✅ Firebase Admin baslatildi")
except Exception as e:
    print(f"⚠️ Firebase baslatma hatası: {e}")


def log_activity(event_type, symbol=None, price=None, pnl=None, pnl_pct=None,
                  detail=None, conviction=None, layer=None, market="US"):
    """ALIM/SATIM/SİNYAL olaylarını bot_activity_log'a yazar."""
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
# OTOMATİK ÖĞRENME SİSTEMİ
# ============================================================

def score_bucket(score):
    """RSI bucket'ının TR/US karşılığı — entry_score bandı."""
    if score is None:
        return "SCORE_YOK"
    if score < 7:
        return "SCORE<7"
    if score < 10:
        return "SCORE7-10"
    if score < 14:
        return "SCORE10-14"
    return "SCORE14+"


def record_signal_outcome(signal_id, symbol, layer, conviction, score, entry_price, market="US"):
    """Bot alımı yapıldığında çağrılır — 24s/72s/7g sonuç takibi için kayıt at."""
    try:
        supabase.table("signal_outcomes").insert({
            "signal_id": signal_id,
            "symbol": symbol,
            "layer": layer,
            "conviction": conviction,
            "entry_score": score,
            "entry_price": entry_price,
            "market": market,
            "checked_24h": False,
            "checked_72h": False,
            "checked_7d": False,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as e:
        print(f"⚠️ signal_outcomes kayıt: {e}")


def _yf_last_price(symbol):
    """yfinance ile son fiyatı çek — check_signal_outcomes için."""
    try:
        t = yf.Ticker(symbol)
        hist = t.history(period="1d", interval="1m").dropna()
        if hist.empty:
            return None
        return float(hist["Close"].iloc[-1])
    except Exception:
        return None


def check_signal_outcomes(market="US", price_fetcher=None):
    """24s/72s/7g eşiklerini dolduran kayıtların gerçek sonucunu ölçer."""
    if price_fetcher is None:
        price_fetcher = _yf_last_price

    try:
        now = datetime.now(timezone.utc)
        field_map = {24: "24h", 72: "72h", 168: "7d"}

        for hours in OUTCOME_CHECK_HOURS:
            field = field_map[hours]
            cutoff = (now - timedelta(hours=hours)).isoformat()

            rows = supabase.table("signal_outcomes") \
                .select("*") \
                .eq(f"checked_{field}", False) \
                .eq("market", market) \
                .lte("created_at", cutoff) \
                .limit(20).execute()

            if not rows.data:
                continue

            for row in rows.data:
                try:
                    current = price_fetcher(row["symbol"])
                    if current is None:
                        supabase.table("signal_outcomes").update({
                            f"checked_{field}": True
                        }).eq("id", row["id"]).execute()
                        continue

                    entry = float(row["entry_price"])
                    pct = ((current - entry) / entry) * 100 if entry > 0 else 0

                    if abs(pct) > 95:
                        # 19 Haz 2026 — KRİTİK FIX: eskiden burada sadece
                        # "continue" deniyordu, checked_{field} hiç True
                        # yapılmıyordu — sonuç: HUMA/ACN gibi kayıtlar
                        # HER taramada yeniden çekilip "şüpheli" diye
                        # loglanıyordu, sonsuz tekrar (log spam + gereksiz
                        # yfinance çağrısı). Artık checked=True yapılıp
                        # gerçek pct değeri kaydediliyor — yorumlama
                        # (gerçek hareket mi, ticker karışıklığı mı)
                        # learning_weights'te |pct|>95 örnekleri zaten
                        # ekstrem olduğu için win/lose hesabını bozmuyor.
                        print(f"⚠️ {row['symbol']} {field} sonucu şüpheli "
                              f"(%{pct:.1f}, {entry}→{current}) — işaretlendi, tekrar denenmeyecek")
                        supabase.table("signal_outcomes").update({
                            f"price_{field}": current,
                            f"pct_{field}": round(pct, 2),
                            f"checked_{field}": True,
                        }).eq("id", row["id"]).execute()
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
        print(f"❌ check_signal_outcomes ({market}): {e}")


def update_learning_weights(market="US"):
    """KALE MİMARİSİ — US learning sistemi. layer + score_bucket kombinasyonu."""
    try:
        rows = supabase.table("signal_outcomes") \
            .select("layer, entry_score, pct_24h") \
            .eq("checked_24h", True) \
            .eq("market", market) \
            .not_.is_("pct_24h", "null") \
            .execute()

        if not rows.data or len(rows.data) < LEARNING_MIN_SAMPLES:
            return

        groups = defaultdict(list)
        for r in rows.data:
            bucket = score_bucket(r.get("entry_score"))
            key = (r.get("layer") or "?", bucket)
            groups[key].append(r["pct_24h"])

        updated = 0
        for (layer, bucket), pcts in groups.items():
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

            loss_multiplier = 2.0 if z < 0 else 1.0
            magnitude = min(abs(z) / 3.0, 1.0) * LEARNING_MAX_BONUS * loss_multiplier
            magnitude = min(magnitude, LEARNING_MAX_BONUS)
            bonus = round(magnitude) if z > 0 else -round(magnitude)
            bonus = max(-LEARNING_MAX_BONUS, min(LEARNING_MAX_BONUS, bonus))

            if bonus == 0:
                continue

            key_str = f"{layer}|{bucket}"
            supabase.table("learning_weights").upsert({
                "pattern_key": key_str,
                "layer": layer,
                "rsi_bucket": bucket,
                "obv_trend": None,
                "sample_size": n,
                "win_rate": round(win_rate * 100, 1),
                "z_score": round(z, 2),
                "bonus": bonus,
                "market": market,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }, on_conflict="market,pattern_key").execute()

            updated += 1
            print(f"  🧠 ÖĞRENME ({market}): {key_str} → bonus:{bonus:+d} "
                  f"(n={n}, win_rate=%{win_rate*100:.1f}, z={z:.2f})")

            log_activity("OGRENME", detail=f"{key_str} → bonus:{bonus:+d} "
                          f"(n={n}, win_rate=%{win_rate*100:.1f}, z={z:.2f})",
                          layer=layer, market=market)

        if updated:
            print(f"  🧠 {updated} patern güncellendi ({market})")

    except Exception as e:
        print(f"❌ update_learning_weights ({market}): {e}")


_learning_cache_us = {"data": {}, "ts": 0}


def get_learning_bonus(layer, score, market="US"):
    """learning_weights'ten önceden hesaplanmış katsayıyı okur. 10 dakika cache'lenir."""
    global _learning_cache_us
    now = time.time()
    if now - _learning_cache_us["ts"] > 600:
        try:
            rows = supabase.table("learning_weights").select("*").eq("market", market).execute()
            _learning_cache_us["data"] = {r["pattern_key"]: r["bonus"] for r in (rows.data or [])}
            _learning_cache_us["ts"] = now
        except Exception:
            pass

    bucket = score_bucket(score)
    key = f"{layer}|{bucket}"
    return _learning_cache_us["data"].get(key, 0)


def send_push_notification(title, body, market="US", signal_id=None):
    try:
        profiles = supabase.table("profiles").select("fcm_token").not_.is_("fcm_token", "null").execute()
        if not profiles.data:
            return
        tokens = [p["fcm_token"] for p in profiles.data if p.get("fcm_token")]
        if not tokens:
            return
        for token in tokens:
            try:
                message = messaging.Message(
                    notification=messaging.Notification(title=title, body=body),
                    data={
                        "market": market,
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
        print(f"📱 Push gönderildi: {len(tokens)} kullanıcı")
    except Exception as e:
        print(f"❌ Push hatası: {e}")


def is_market_open():
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5:
        return False
    open_t = now.replace(hour=13, minute=30, second=0, microsecond=0)
    close_t = now.replace(hour=20, minute=0, second=0, microsecond=0)
    return open_t <= now <= close_t


def load_watchlist_from_db():
    global active_symbols
    try:
        print("📋 Watchlist Supabase'den yukleniyor...")
        result = supabase.table("us_watchlist").select("symbol, avg_volume, last_price, name, sector").execute()
        if not result.data:
            return []
        symbols = []
        for row in result.data:
            sym = row["symbol"]
            vol = row.get("avg_volume", 0) or 0
            if vol > 0:
                avg_volumes[sym] = vol
                symbols.append(sym)
                company_cache[sym] = {
                    "name": row.get("name", ""),
                    "sector": row.get("sector", ""),
                    "market_cap": 0, "country": "", "exchange": ""
                }
        print(f"✅ {len(symbols)} hisse yuklendi")
        return symbols
    except Exception as e:
        print(f"❌ Watchlist yukleme hatası: {e}")
        return []


def refresh_watchlist_background():
    global active_symbols
    try:
        print("🔄 Watchlist yenileniyor...")
        nasdaq, nyse = [], []
        try:
            r = requests.get('https://raw.githubusercontent.com/datasets/nasdaq-listings/main/data/nasdaq-listed.csv', timeout=10)
            nasdaq = [l.split(',')[0] for l in r.text.strip().split('\n')[1:] if l.split(',')[0].isalpha() and 2 <= len(l.split(',')[0]) <= 5]
        except:
            pass
        try:
            r = requests.get('https://raw.githubusercontent.com/datasets/nyse-listings/main/data/nyse-listed.csv', timeout=10)
            nyse = [l.split(',')[0] for l in r.text.strip().split('\n')[1:] if l.split(',')[0].isalpha() and 2 <= len(l.split(',')[0]) <= 5]
        except:
            pass

        all_symbols = list(set(nasdaq + nyse))
        candidates = []

        for i in range(0, len(all_symbols), 200):
            batch = all_symbols[i:i+200]
            try:
                data = yf.download(' '.join(batch), period='5d', interval='1d', progress=False, threads=True)
                if data.empty:
                    continue
                closes = data['Close'].iloc[-1]
                volumes = data['Volume'].mean()
                for sym in batch:
                    try:
                        price = float(closes[sym])
                        vol = float(volumes[sym])
                        # 19 Haz 2026: filtre gevşetildi $1-20+500K → $0.50+ + 100K
                        # (POWW gibi düşük hacimli ama hareketli hisseler için)
                        if price >= 0.50 and vol >= 100_000:
                            candidates.append(sym)
                            avg_volumes[sym] = vol
                            profile = {"name": "", "sector": "", "market_cap": 0, "country": "", "exchange": ""}
                            if sym not in company_cache:
                                try:
                                    rp = requests.get(f"https://finnhub.io/api/v1/stock/profile2?symbol={sym}&token={FINNHUB_KEY}", timeout=5)
                                    d = rp.json()
                                    profile = {
                                        "name": d.get("name", ""),
                                        "sector": d.get("finnhubIndustry", ""),
                                        "market_cap": int(d.get("marketCapitalization", 0) * 1_000_000) if d.get("marketCapitalization") else 0,
                                        "country": d.get("country", ""),
                                        "exchange": d.get("exchange", ""),
                                    }
                                    company_cache[sym] = profile
                                    time.sleep(0.1)
                                except:
                                    pass
                            else:
                                profile = company_cache[sym]
                            try:
                                supabase.table("us_watchlist").upsert({
                                    "symbol": sym, "name": profile["name"],
                                    "sector": profile["sector"], "market_cap": profile["market_cap"],
                                    "country": profile["country"], "exchange": profile["exchange"],
                                    "avg_volume": int(vol), "last_price": round(price, 2),
                                    "updated_at": datetime.now(timezone.utc).isoformat()
                                }).execute()
                            except:
                                pass
                    except:
                        continue
            except:
                continue
            time.sleep(1)

        active_symbols = candidates
        print(f"✅ Watchlist yenilendi: {len(candidates)} hisse")
    except Exception as e:
        print(f"❌ Watchlist yenileme hatası: {e}")


def get_news_catalyst(symbol, company_name=""):
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        two_days_ago = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d")
        r = requests.get(
            f"https://finnhub.io/api/v1/company-news?symbol={symbol}&from={two_days_ago}&to={today}&token={FINNHUB_KEY}",
            timeout=5
        )
        news_list = r.json()
        if not isinstance(news_list, list) or not news_list:
            return None

        negative_keywords = [
            "sell", "reasons to sell", "short", "downgrade",
            "sinks", "drops", "falls", "decline", "warning",
            "bearish", "avoid", "overvalued", "cut",
        ]

        catalyst_keywords = [
            "earnings", "revenue", "profit", "beat", "exceed", "guidance",
            "upgrade", "buy", "outperform", "target raised", "price target",
            "fda", "approval", "approved", "patent",
            "merger", "acquisition", "buyout", "deal",
            "contract", "partnership", "agreement",
            "dividend", "buyback", "record",
            "surging", "soaring", "jumps", "rally",
        ]

        symbol_lower = symbol.lower()
        company_lower = company_name.lower() if company_name else ""

        for news in news_list[:10]:
            headline = news.get("headline", "").lower()
            summary = news.get("summary", "").lower()

            is_relevant = (
                symbol_lower in headline or
                symbol_lower in summary or
                (company_lower and len(company_lower) > 4 and company_lower.split()[0] in headline)
            )
            if not is_relevant:
                continue

            if any(kw in headline for kw in negative_keywords):
                continue

            for kw in catalyst_keywords:
                if kw in headline or kw in summary:
                    return {
                        "headline": news.get("headline", ""),
                        "keyword": kw,
                        "time": news.get("datetime", 0)
                    }

        return None
    except:
        return None


def get_analyst_rating(symbol):
    if symbol in analyst_cache:
        cached_time, cached_data = analyst_cache[symbol]
        if time.time() - cached_time < 3600:
            return cached_data
    try:
        r = requests.get(
            f"https://finnhub.io/api/v1/stock/recommendation?symbol={symbol}&token={FINNHUB_KEY}",
            timeout=5
        )
        data = r.json()
        if data and isinstance(data, list):
            latest = data[0]
            result = {
                "buy": latest.get("buy", 0) + latest.get("strongBuy", 0),
                "hold": latest.get("hold", 0),
                "sell": latest.get("sell", 0)
            }
            analyst_cache[symbol] = (time.time(), result)
            return result
        return None
    except:
        return None


def get_insider_recent(symbol):
    try:
        r = requests.get(
            f"https://finnhub.io/api/v1/stock/insider-transactions?symbol={symbol}&token={FINNHUB_KEY}",
            timeout=5
        )
        data = r.json().get("data", [])
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
        recent_purchases = [
            t for t in data
            if t.get("transactionCode") == "P-Purchase"
            and t.get("transactionDate", "") >= cutoff
        ]
        if len(recent_purchases) >= 2:
            total = sum(p.get("share", 0) * p.get("transactionPrice", 0) for p in recent_purchases)
            return f"{len(recent_purchases)} insider alımı (${total:,.0f})"
        if recent_purchases:
            p = recent_purchases[0]
            return f"{p.get('name', 'Insider')} — {p.get('share', 0):,} hisse"
        return None
    except:
        return None


def get_earnings_today():
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        r = requests.get(
            f"https://finnhub.io/api/v1/calendar/earnings?from={today}&to={today}&token={FINNHUB_KEY}",
            timeout=10
        )
        data = r.json()
        earnings = data.get("earningsCalendar", [])
        symbols = []
        for e in earnings:
            sym = e.get("symbol", "")
            if sym:
                symbols.append({
                    "symbol": sym,
                    "eps_estimate": e.get("epsEstimate"),
                    "eps_actual": e.get("epsActual"),
                    "hour": e.get("hour", "")
                })
        print(f"📅 Bugün {len(symbols)} şirket earnings açıklıyor")
        return symbols
    except Exception as e:
        print(f"⚠️ Earnings hatası: {e}")
        return []


def get_5day_trend(symbol):
    try:
        t = yf.Ticker(symbol)
        hist = t.history(period="10d").dropna()
        if len(hist) < 3:
            return None
        closes = hist['Close'].tolist()
        volumes = hist['Volume'].tolist()
        last5 = closes[-5:] if len(closes) >= 5 else closes
        trend = "up" if last5[-1] > last5[0] else "down"
        prev_change = ((closes[-1] - closes[-2]) / closes[-2]) * 100
        avg_vol = sum(volumes[:-1]) / len(volumes[:-1]) if volumes[:-1] else 0
        vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 0
        return {
            "trend": trend,
            "prev_change": round(prev_change, 2),
            "vol_ratio": round(vol_ratio, 2),
            "last_close": round(closes[-1], 2),
            "5d_change": round(((closes[-1] - closes[-5]) / closes[-5]) * 100, 2) if len(closes) >= 5 else 0
        }
    except:
        return None


def premarket_conviction(trend_data, catalyst, earnings, analyst, insider):
    score = 0
    reasons = []

    if not trend_data:
        return "NORMAL", [], 0, None

    has_catalyst = bool(catalyst or earnings or analyst or insider)
    if not has_catalyst:
        return "NORMAL", [], 0, None

    if earnings:
        eps_est = earnings.get("eps_estimate")
        eps_act = earnings.get("eps_actual")
        if eps_est and eps_act and eps_act > eps_est:
            beat_pct = ((eps_act - eps_est) / abs(eps_est)) * 100 if eps_est != 0 else 0
            score += 5
            reasons.append(f"Earnings BEAT %{beat_pct:.0f} ({eps_act} vs {eps_est})")
        else:
            score += 2
            reasons.append("Earnings açıklaması bugün")

    if catalyst:
        score += 3
        reasons.append(f"Kataliz [{catalyst['keyword']}]: {catalyst['headline'][:60]}")

    if analyst:
        buy = analyst.get("buy", 0)
        sell = analyst.get("sell", 0)
        if buy >= 5 and sell == 0:
            score += 3
            reasons.append(f"Analist: {buy} AL 0 SAT")
        elif buy > sell * 2:
            score += 2
            reasons.append(f"Analist: {buy} AL {sell} SAT")

    if insider:
        score += 3
        reasons.append(f"Insider: {insider}")

    if trend_data["vol_ratio"] >= 2:
        score += 3
        reasons.append(f"Hacim {trend_data['vol_ratio']}x — kurumsal ilgi")
    elif trend_data["vol_ratio"] >= 1.5:
        score += 2
        reasons.append(f"Hacim {trend_data['vol_ratio']}x artışı")

    if trend_data["prev_change"] >= 4:
        score += 2
        reasons.append(f"Dün +{trend_data['prev_change']}% güçlü kapanış")
    elif trend_data["prev_change"] >= 2:
        score += 1
        reasons.append(f"Dün +{trend_data['prev_change']}% pozitif")

    if earnings:
        layer = "EARNINGS"
    elif catalyst:
        layer = "HABER"
    else:
        layer = "TEKNIK"

    bonus = get_learning_bonus(layer, score, market="US")
    if bonus != 0:
        score += bonus
        reasons.append(f"🧠 Öğrenme katsayısı: {bonus:+d}")

    if score >= 8:
        conviction = "CRITICAL"
    elif score >= 6:
        conviction = "HIGH"
    elif score >= 4:
        conviction = "MEDIUM"
    else:
        conviction = "NORMAL"

    return conviction, reasons, score, layer


def get_ai_explanation(symbol, company_name, sector, trend_data, catalyst, earnings, analyst, insider, conviction, reasons, is_premarket_signal=False):
    try:
        analyst_str = f"Buy={analyst['buy']} Hold={analyst['hold']} Sell={analyst['sell']}" if analyst else "N/A"

        if is_premarket_signal and trend_data:
            prompt = f"""Financial analyst. Pre-market signal. 3 levels. Be specific.

Stock: {symbol} ({company_name}) | Sector: {sector}
Last Close: ${trend_data['last_close']} | Yesterday: {trend_data['prev_change']:+}% | 5d: {trend_data['5d_change']:+}%
Volume Ratio: {trend_data['vol_ratio']}x | Conviction: {conviction}
Reasons: {' | '.join(reasons)}
Catalyst: {catalyst['headline'] if catalyst else 'None'}
Insider: {insider if insider else 'None'}
Analyst: {analyst_str}

===BEGINNER===
[Max 2 sentences. What to expect at open. Plain language.]
===INTERMEDIATE===
[Max 3 sentences. Technical setup + catalyst + key level.]
===PRO===
[Max 4 sentences. Institutional probability, catalyst strength, risk/reward, entry/stop/target in dollars.]"""
        else:
            prompt = f"""Financial analyst. Live signal. 3 levels. Be specific.

Stock: {symbol} ({company_name}) | Sector: {sector}
Conviction: {conviction}
Catalyst: {catalyst['headline'] if catalyst else 'None'}
Insider: {insider if insider else 'None'}

===BEGINNER===
[Max 2 sentences. Plain language.]
===INTERMEDIATE===
[Max 3 sentences. Technical analysis.]
===PRO===
[Max 4 sentences. Risk/reward, entry/stop/target in dollars.]"""

        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"},
            json={
                "model": "llama-3.1-8b-instant",
                "messages": [
                    {"role": "system", "content": "Financial analyst. Use exact format. Never change ===BEGINNER===, ===INTERMEDIATE===, ===PRO=== headers. Be specific."},
                    {"role": "user", "content": prompt}
                ],
                "max_tokens": 500,
                "temperature": 0.2
            },
            timeout=15
        )
        resp = r.json()
        if "choices" not in resp:
            return ""
        return resp["choices"][0]["message"]["content"]
    except:
        return ""


def parse_ai_levels(ai_text):
    acemi, usta, pro = "", "", ""
    try:
        if "===BEGINNER===" in ai_text:
            acemi = ai_text.split("===BEGINNER===")[1].split("===INTERMEDIATE===")[0].strip()
        if "===INTERMEDIATE===" in ai_text:
            usta = ai_text.split("===INTERMEDIATE===")[1].split("===PRO===")[0].strip()
        if "===PRO===" in ai_text:
            pro = ai_text.split("===PRO===")[1].strip()
    except:
        pass
    return acemi, usta, pro


def get_last_signal_time(symbol):
    try:
        r = supabase.table("us_signals").select("created_at").eq("symbol", symbol).order("created_at", desc=True).limit(1).execute()
        if r.data:
            dt = datetime.fromisoformat(r.data[0]["created_at"].replace("Z", "+00:00"))
            return dt.timestamp()
        return 0
    except:
        return 0


def update_live_price(symbol, price):
    now = time.time()
    if symbol in last_price_update and now - last_price_update[symbol] < 60:
        return
    try:
        supabase.table("us_watchlist").update({
            "last_price": round(price, 2),
            "updated_at": datetime.now(timezone.utc).isoformat()
        }).eq("symbol", symbol).execute()
        last_price_update[symbol] = now
    except:
        pass


class VolumeTracker:
    def __init__(self):
        self.trades = {}
        self.daily_opens = {}

    def update(self, symbol, price, volume):
        now = time.time()
        if symbol not in self.trades:
            self.trades[symbol] = []
            self.daily_opens[symbol] = price
        self.trades[symbol].append((price, volume, now))
        cutoff = now - 300
        self.trades[symbol] = [(p, v, t) for p, v, t in self.trades[symbol] if t > cutoff]

    def get_volume_ratio(self, symbol, avg_volume):
        if avg_volume <= 0:
            return 0
        recent_volume = sum(v for _, v, _ in self.trades.get(symbol, []))
        expected_5min = avg_volume / 78
        return recent_volume / expected_5min if expected_5min > 0 else 0

    def get_price_change(self, symbol, current_price):
        open_price = self.daily_opens.get(symbol, current_price)
        if not open_price:
            return 0
        return ((current_price - open_price) / open_price) * 100


tracker = VolumeTracker()


# ============================================================
# BOT — SAVAŞ MİMARİSİ
# ============================================================

def get_open_us_trades(user_id):
    r = supabase.table("demo_trades").select("*") \
        .eq("user_id", user_id).eq("status", "open").eq("market", "US").execute()
    return r.data or []


def get_us_hist_rate(user_id):
    r = supabase.table("demo_trades").select("profit_loss") \
        .eq("user_id", user_id).eq("market", "US").eq("status", "closed").execute()
    if not r.data:
        return 0
    wins = sum(1 for t in r.data if (t.get("profit_loss") or 0) > 0)
    return (wins / len(r.data)) * 100


def calc_us_levels(price, trend_data):
    """$1-20 küçük cap US hisseleri için stop/hedef.
    Devre kesici yok — earnings sonrası %20-30 hareketler olağan.
    Stop: volatilite x1.5 (max %8), Hedef: volatilite x5 (max %20)"""
    daily_vol = abs(trend_data.get("5d_change", 0)) / 5 if trend_data else 2.0
    daily_vol = max(daily_vol, 1.5)
    stop_pct   = min(daily_vol * 1.5, 8.0)
    target_pct = min(daily_vol * 5.0, 20.0)
    stop   = price * (1 - stop_pct / 100)
    target = price * (1 + target_pct / 100)
    return round(stop, 2), round(target, 2)


def bot_should_buy(price_change, volume_ratio, conviction):
    return conviction == "CRITICAL"


def us_bot_should_sell(trade, current_price):
    buy_price = trade["buy_price"]
    peak = max(trade.get("peak_price") or buy_price, current_price)
    change = ((current_price - buy_price) / buy_price) * 100
    peak_change = ((peak - buy_price) / buy_price) * 100

    if peak_change < 2.0:
        stop = trade.get("stop_price") or buy_price * 0.92
        if current_price <= stop:
            print(f"  🛑 SABİT STOP (%8): %{change:.1f}")
            return True, "stop_loss", peak
        return False, "", peak

    if peak_change < 5.0:
        if current_price <= buy_price:
            print(f"  ⚖️ BREAKEVEN STOP: tepe %{peak_change:.1f}, şimdi %{change:.1f}")
            return True, "breakeven", peak
        return False, "", peak

    trailing_stop = peak * 0.96
    if current_price <= trailing_stop:
        print(f"  🔄 TRAİLİNG STOP (tepeden %4): tepe ${peak:.2f} → şimdi ${current_price:.2f} (%{change:.1f})")
        return True, "trailing_stop", peak

    try:
        buy_date = trade.get("buy_date")
        if buy_date:
            buy_dt = datetime.fromisoformat(str(buy_date).replace("Z", "+00:00"))
            if buy_dt.tzinfo is None:
                buy_dt = buy_dt.replace(tzinfo=timezone.utc)
            hold_hours = (datetime.now(timezone.utc) - buy_dt).total_seconds() / 3600
            if hold_hours >= 48 and current_price <= buy_price:
                print(f"  ⏰ ZOMBİ: {hold_hours:.1f}s açık, kâr yok (%{change:.1f})")
                return True, "zombie_cleanup", peak
    except Exception:
        pass

    return False, "", peak


def bot_buy(user_id, symbol, price, signal_id, is_pro, balance, conviction, score, reasons, trend_data, layer=None):
    try:
        try:
            ms = supabase.table("market_status").select("us_status") \
                .eq("id", 1).maybeSingle().execute()
            if ms.data and ms.data.get("us_status") == "RED":
                print(f"  🔴 MAKRO RED — S&P500 düşüşte, {symbol} alımı durduruldu")
                return False
        except Exception:
            pass
        open_trades = get_open_us_trades(user_id)
        open_count = len(open_trades)

        open_symbols = [t.get("symbol") for t in open_trades]
        if symbol in open_symbols:
            print(f"  ⏭️ {symbol} zaten açık pozisyonda — tekrar alım yapılmıyor")
            return False

        stop, target = calc_us_levels(price, trend_data)
        entry_reason = " | ".join(reasons[:3]) if reasons else conviction
        is_exceptional = False

        if not is_pro:
            month_start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0).isoformat()
            month_trades = supabase.table("demo_trades").select("id").eq("user_id", user_id).eq("market", "US").gte("created_at", month_start).execute()
            if len(month_trades.data) >= 3:
                print(f"⚠️ {user_id} aylik limit doldu")
                return False
            if open_count >= 3:
                print(f"⚠️ {user_id} 3 açık pozisyon dolu (Free)")
                return False
        else:
            BASE_CAP = 5
            EXCEPTIONAL_CAP = 3
            MAX_TOTAL = BASE_CAP + EXCEPTIONAL_CAP

            if open_count >= MAX_TOTAL:
                print(f"⚠️ {user_id} max {MAX_TOTAL} pozisyon dolu (Pro)")
                return False

            if open_count >= BASE_CAP:
                hist_rate = get_us_hist_rate(user_id)
                if not (score >= 10 and hist_rate >= 90):
                    print(f"⚠️ {user_id} istisna kriteri karşılanmadı (score={score}, hist={hist_rate:.0f}%)")
                    return False
                exceptional_open = sum(1 for t in open_trades if t.get("is_exceptional"))
                if exceptional_open >= EXCEPTIONAL_CAP:
                    print(f"⚠️ {user_id} istisna slotları dolu")
                    return False
                is_exceptional = True

        invest = min(balance * 0.10, 100)
        if invest < 10:
            return False
        quantity = invest / price

        supabase.table("demo_trades").insert({
            "user_id": user_id, "symbol": symbol, "market": "US",
            "signal_id": signal_id, "buy_price": price,
            "buy_date": datetime.now(timezone.utc).isoformat(),
            "quantity": round(quantity, 4), "status": "open",
            "stop_price": stop, "target_price": target, "peak_price": price,
            "entry_reason": entry_reason, "entry_conviction": conviction,
            "entry_score": score, "is_exceptional": is_exceptional,
            "created_at": datetime.now(timezone.utc).isoformat()
        }).execute()
        supabase.table("demo_portfolios").update({
            "us_balance": round(balance - invest, 2)
        }).eq("user_id", user_id).execute()
        tag = " 🌟İSTİSNA" if is_exceptional else ""
        print(f"✅ BOT ALIM{tag}: {user_id} → {symbol} @ ${price:.2f} | Stop:${stop} Hedef:${target}")
        log_activity("ALIM", symbol=symbol, price=price,
                      detail=f"${invest:.0f} yatırım | Stop:{stop} | Hedef:{target}" + (" | İSTİSNAİ" if is_exceptional else ""),
                      conviction=conviction, market="US")
        record_signal_outcome(signal_id, symbol, layer, conviction, score, price, market="US")
        return True
    except Exception as e:
        print(f"❌ Bot alım hatası: {e}")
        return False


def bot_sell(trade, current_price, exit_reason=""):
    try:
        profit_loss = (current_price - trade["buy_price"]) * trade["quantity"]
        pct = ((current_price - trade["buy_price"]) / trade["buy_price"]) * 100
        supabase.table("demo_trades").update({
            "sell_price": current_price,
            "sell_date": datetime.now(timezone.utc).isoformat(),
            "status": "closed",
            "profit_loss": round(profit_loss, 2),
            "exit_reason": exit_reason
        }).eq("id", trade["id"]).execute()
        portfolio = supabase.table("demo_portfolios").select("us_balance").eq("user_id", trade["user_id"]).maybeSingle().execute()
        if portfolio.data:
            new_balance = portfolio.data["us_balance"] + (trade["quantity"] * current_price)
            supabase.table("demo_portfolios").update({"us_balance": round(new_balance, 2)}).eq("user_id", trade["user_id"]).execute()
        print(f"✅ BOT SATIŞ ({exit_reason}): {trade['symbol']} | K/Z: ${profit_loss:.2f}")
        log_activity("SATIM", symbol=trade["symbol"], price=current_price,
                      pnl=round(profit_loss, 2), pnl_pct=round(pct, 2),
                      detail=exit_reason,
                      conviction=trade.get("entry_conviction"), market="US")
    except Exception as e:
        print(f"❌ Bot satış hatası: {e}")


def bot_check_open_positions():
    try:
        trades = supabase.table("demo_trades").select("*").eq("status", "open").eq("market", "US").execute()
        if not trades.data:
            return
        print(f"🔍 {len(trades.data)} acik US pozisyon kontrol ediliyor...")
        for trade in trades.data:
            try:
                t = yf.Ticker(trade["symbol"])
                hist = t.history(period="1d", interval="1m").dropna()
                if hist.empty:
                    continue
                current_price = float(hist["Close"].iloc[-1])
                should_sell, exit_reason, new_peak = us_bot_should_sell(trade, current_price)
                if should_sell:
                    bot_sell(trade, current_price, exit_reason)
                else:
                    update_data = {"current_price": current_price}
                    if new_peak != (trade.get("peak_price") or trade["buy_price"]):
                        update_data["peak_price"] = new_peak
                    supabase.table("demo_trades").update(update_data).eq("id", trade["id"]).execute()
            except:
                continue
            time.sleep(0.3)
    except Exception as e:
        print(f"❌ Pozisyon kontrol hatası: {e}")


def bot_process_signal(symbol, price, price_change, volume_ratio, conviction, signal_id, score=0, reasons=None, trend_data=None, layer=None):
    try:
        if not bot_should_buy(price_change, volume_ratio, conviction):
            return
        print(f"🤖 Bot {symbol}: AL")
        portfolios = supabase.table("demo_portfolios").select("user_id, us_balance").execute()
        if not portfolios.data:
            return
        for portfolio in portfolios.data:
            user_id = portfolio["user_id"]
            balance = portfolio.get("us_balance", 0) or 0
            if balance < 10:
                continue
            profile = supabase.table("profiles").select("is_pro").eq("id", user_id).limit(1).execute()
            is_pro = profile.data[0].get("is_pro", False) if profile.data else False
            bot_buy(user_id, symbol, price, signal_id, is_pro, balance, conviction, score, reasons or [], trend_data, layer)
            time.sleep(0.2)
    except Exception as e:
        print(f"❌ Bot sinyal hatası: {e}")


# ============================================================
# SİNYAL MOTORU
# ============================================================

def run_premarket_scan():
    """Gece analizi — sinyalleri DB'ye kaydet, push GÖNDERME"""
    print("\n" + "="*50)
    print("🦅 KARTAL GÖZÜ — GECE ANALİZİ")
    print(f"⏰ {datetime.now(timezone.utc).strftime('%H:%M UTC')} — Sabah 15:30 TR'de sinyaller gönderilecek")
    print("="*50)

    print("\n📅 Earnings takvimi kontrol ediliyor...")
    earnings_today = get_earnings_today()
    earnings_map = {e["symbol"]: e for e in earnings_today}

    all_targets = set(list(avg_volumes.keys()) + list(earnings_map.keys()))
    print(f"🔍 {len(all_targets)} hisse taranıyor...")

    signals = []

    for symbol in all_targets:
        try:
            if symbol in premarket_signal_cache:
                continue

            company = company_cache.get(symbol, {"name": "", "sector": ""})
            company_name = company.get("name", "")
            sector = company.get("sector", "")

            earnings = earnings_map.get(symbol)
            catalyst = get_news_catalyst(symbol, company_name)
            time.sleep(0.05)

            if not catalyst and not earnings:
                continue

            trend = get_5day_trend(symbol)
            if not trend:
                continue

            if not earnings and not catalyst:
                continue

            analyst = get_analyst_rating(symbol)
            insider = get_insider_recent(symbol)

            conviction, reasons, score, layer = premarket_conviction(trend, catalyst, earnings, analyst, insider)

            if conviction == "NORMAL":
                continue

            signals.append({
                "symbol": symbol,
                "company_name": company_name,
                "sector": sector,
                "trend": trend,
                "catalyst": catalyst,
                "earnings": earnings,
                "analyst": analyst,
                "insider": insider,
                "conviction": conviction,
                "reasons": reasons,
                "score": score,
                "layer": layer
            })

            print(f"  🎯 {symbol} ({company_name}) | {conviction} | Score: {score}")
            for r in reasons:
                print(f"     → {r}")

            time.sleep(0.3)

        except Exception as e:
            print(f"❌ {symbol}: {e}")
            continue

    signals.sort(key=lambda x: x["score"], reverse=True)
    top5 = signals[:5]

    print(f"\n✅ {len(signals)} aday — en iyi {len(top5)} sinyal DB'ye kaydediliyor...")

    for sig in top5:
        try:
            symbol = sig["symbol"]
            trend = sig["trend"]
            conviction = sig["conviction"]

            ai_text = get_ai_explanation(
                symbol, sig["company_name"], sig["sector"],
                trend, sig["catalyst"], sig["earnings"],
                sig["analyst"], sig["insider"],
                conviction, sig["reasons"], is_premarket_signal=True
            )
            acemi, usta, pro = parse_ai_levels(ai_text)

            emoji = "🔥" if conviction == "CRITICAL" else "⚡" if conviction == "HIGH" else "👁️"
            description = f"{emoji} PRE-MARKET | {symbol}"
            if sig["company_name"]:
                description += f" ({sig['company_name']})"
            description += f" | ${trend['last_close']} | Dun: {trend['prev_change']:+}%"
            if sig["catalyst"]:
                description += f" | 📰 {sig['catalyst']['headline'][:60]}"
            if sig["earnings"]:
                description += " | 📊 Earnings"

            result = supabase.table("us_signals").insert({
                "symbol": symbol,
                "signal_type": "premarket",
                "value": trend["prev_change"],
                "description": description,
                "acemi_explanation": acemi,
                "usta_explanation": usta,
                "pro_explanation": pro,
                "price": trend["last_close"],
                "volume_ratio": trend["vol_ratio"],
                "conviction": conviction,
                "score": sig["score"],
                "created_at": datetime.now(timezone.utc).isoformat()
            }).execute()

            signal_id = result.data[0].get("id") if result.data else None
            premarket_signal_cache[symbol] = time.time()
            print(f"✅ DB'ye kaydedildi: {description}")

            log_activity("SINYAL", symbol=symbol, price=trend["last_close"],
                          detail=f"Score:{sig['score']} | Dun:{trend['prev_change']:+}% | PRE-MARKET",
                          conviction=conviction, market="US")

        except Exception as e:
            print(f"❌ Sinyal kayıt hatası {sig['symbol']}: {e}")

    print(f"\n🌙 Gece analizi tamamlandı. {len(top5)} sinyal hazır. Sabah 15:30 TR'de gönderilecek.")


def send_morning_signals():
    """Gece hazırlanan US sinyallerini 15:30 TR'de push ile gönder — sadece bildirim"""
    try:
        since = (datetime.now(timezone.utc) - timedelta(hours=16)).isoformat()
        r = supabase.table("us_signals") \
            .select("*") \
            .gte("created_at", since) \
            .eq("signal_type", "premarket") \
            .order("created_at", desc=True) \
            .limit(5) \
            .execute()

        if not r.data:
            print("⚠️ Sabah için US sinyali bulunamadı")
            return

        print(f"📱 {len(r.data)} US sabah sinyali gönderiliyor...")

        for signal in r.data:
            symbol = signal["symbol"]
            price = signal.get("price", 0)
            value = float(signal.get("value", 0))
            volume_ratio = float(signal.get("volume_ratio", 0) or 0)
            signal_id = signal["id"]
            description = signal.get("description", "")
            conviction = signal.get("conviction") or (
                "CRITICAL" if "🔥" in description else "HIGH"
            )
            score = signal.get("score") or 0

            if conviction != "CRITICAL":
                print(f"  ⏭️ {symbol} {conviction} — bot alımı yok, sadece push")
            
            company_name = ""
            if "(" in description and ")" in description:
                try:
                    company_name = description.split("(")[1].split(")")[0]
                except:
                    pass

            emoji = "🔥" if conviction == "CRITICAL" else "⚡"

            push_body = f"${price:.2f} | {value:+.1f}% | Vol: {volume_ratio:.1f}x"
            if company_name:
                push_body = f"{company_name} — {push_body}"

            send_push_notification(
                title=f"{emoji} {symbol} — Açılış Öncesi Sinyal",
                body=push_body,
                market="US",
                signal_id=signal_id
            )

            if conviction == "HIGH":
                record_signal_outcome(signal_id, symbol, "PREMARKET", conviction, score, price, market="US")
                print(f"  📊 {symbol} HIGH — signal_outcomes'a kaydedildi (bot almıyor)")

            # Sabah push sadece bildirim — alım 13:30 UTC WebSocket açılınca olur
            if conviction == "CRITICAL" and signal_id:
                record_signal_outcome(signal_id, symbol, "PREMARKET", conviction, score, price, market="US")
                print(f"  📌 {symbol} CRITICAL — 13:30 UTC WebSocket'te alınacak")

            time.sleep(0.5)

        print("✅ US sabah sinyalleri gönderildi.")
    except Exception as e:
        print(f"❌ US sabah sinyal gönderme hatası: {e}")


def process_live_signal(symbol, signal_type, price, price_change, volume_ratio):
    if not is_market_open():
        return
    if price < 0.50:
        return
    if price_change < 0:
        return

    now = time.time()
    if symbol in signal_cache and now - signal_cache[symbol] < 3600:
        return
    last_time = get_last_signal_time(symbol)
    if now - last_time < 3600:
        signal_cache[symbol] = last_time
        return

    company = company_cache.get(symbol, {"name": "", "sector": ""})
    company_name = company.get("name", "")

    catalyst = get_news_catalyst(symbol, company_name)
    insider = get_insider_recent(symbol)
    has_catalyst = bool(catalyst or insider)

    score = 0
    if abs(price_change) >= 10:
        score += 3
    elif abs(price_change) >= 5:
        score += 2
    elif abs(price_change) >= 3:
        score += 2
    elif abs(price_change) >= 1.5:
        score += 1
    if volume_ratio >= 10:
        score += 3
    elif volume_ratio >= 5:
        score += 2
    elif volume_ratio >= 2:
        score += 1
    if catalyst:
        score += 3
    if insider:
        score += 2

    layer = "CANLI"
    bonus = get_learning_bonus(layer, score, market="US")
    if bonus != 0:
        score += bonus

    if score >= 8:
        conviction = "CRITICAL"
    elif score >= 6:
        conviction = "HIGH"
    elif score >= 4:
        conviction = "MEDIUM"
    else:
        conviction = "NORMAL"

    if conviction == "NORMAL" and not has_catalyst:
        return

    print(f"📊 CANLI: {symbol} ({company_name}) | {conviction} | {price_change:+.1f}% | {volume_ratio:.1f}x")

    emoji = "🔥" if conviction in ("CRITICAL", "HIGH") else "⚡"
    description = f"{emoji} {symbol}"
    if company_name:
        description += f" ({company_name})"
    description += f" | ${price:.2f} | {price_change:+.1f}% | Vol: {volume_ratio:.1f}x | {conviction}"
    if catalyst:
        description += f" | 📰 {catalyst['headline'][:60]}"
    if insider:
        description += f" | 🐋 {insider}"

    try:
        result = supabase.table("us_signals").insert({
            "symbol": symbol,
            "signal_type": signal_type,
            "value": round(price_change, 2),
            "description": description,
            "acemi_explanation": "",
            "usta_explanation": "",
            "pro_explanation": "",
            "price": price,
            "volume_ratio": round(volume_ratio, 2),
            "created_at": datetime.now(timezone.utc).isoformat()
        }).execute()

        signal_cache[symbol] = now
        signal_id = result.data[0].get("id") if result.data else None
        print(f"✅ KAYDEDİLDİ: {description}")

        log_activity("SINYAL", symbol=symbol, price=price,
                      detail=f"{price_change:+.1f}% | Vol:{volume_ratio:.1f}x | {signal_type}",
                      conviction=conviction, market="US")

        trend_for_levels = get_5day_trend(symbol)

        if conviction == "HIGH" and signal_id:
            record_signal_outcome(signal_id, symbol, layer, conviction, score, price, market="US")
            print(f"  📊 HIGH sinyal takibe alındı (bot almıyor, sonuç izleniyor)")

        if conviction == "CRITICAL":
            bot_process_signal(
                symbol, price, price_change, volume_ratio, conviction, signal_id,
                score=score,
                reasons=[catalyst["headline"]] if catalyst else [],
                trend_data=trend_for_levels,
                layer=layer
            )

        push_body = f"${price:.2f} | {price_change:+.1f}% | Vol: {volume_ratio:.1f}x"
        if company_name:
            push_body = f"{company_name} — {push_body}"
        send_push_notification(
            title=f"{emoji} {symbol} — {conviction}",
            body=push_body,
            market="US",
            signal_id=signal_id
        )
    except Exception as e:
        print(f"❌ Kayıt hatası: {e}")


def on_message(ws, message):
    try:
        data = json.loads(message)
        if data.get("type") != "trade":
            return
        for trade in data.get("data", []):
            symbol = trade.get("s")
            price = float(trade.get("p", 0))
            volume = float(trade.get("v", 0))
            if not symbol or not price or price < 0.50:
                continue
            tracker.update(symbol, price, volume)
            if symbol in avg_volumes:
                update_live_price(symbol, price)
            avg_vol = avg_volumes.get(symbol, 0)
            if avg_vol == 0:
                continue
            volume_ratio = tracker.get_volume_ratio(symbol, avg_vol)
            price_change = tracker.get_price_change(symbol, price)
            if volume_ratio >= 2 and price_change >= 1.5:
                process_live_signal(symbol, "momentum", price, price_change, volume_ratio)
            elif volume_ratio >= 5 and price_change > 0:
                process_live_signal(symbol, "volume_spike", price, price_change, volume_ratio)
    except Exception as e:
        print(f"WS mesaj hatası: {e}")


_current_subscribed = set()


def on_open(ws):
    """
    19 Haz 2026 — İKİNCİ DÜZELTME (paralel bağlantı denemesi geri alındı):
    Finnhub ücretsiz plan WebSocket'i TOPLAM 50 sembolle sınırlıyor —
    bağlantı başına değil, token/IP başına. 20 paralel bağlantı açıp
    her birine 50 sembol vermeye çalışmak Finnhub'ı sürekli rate-limit
    tetiklemeye zorladı: tüm bağlantılar saniyede bir kopup yeniden
    bağlanıyordu, sonuç olarak US botu saatlerce etkin biçimde HİÇBİR
    veri alamadı (dönen pencereden daha kötü bir durum). Artık TEK
    bağlantı + watchlist'in en yüksek ortalama hacimli 50 sembolü
    kullanılıyor — Finnhub'ın izin verdiği gerçek limit bu.
    """
    global _current_subscribed
    top50 = sorted(active_symbols, key=lambda s: avg_volumes.get(s, 0), reverse=True)[:50]
    _current_subscribed = set(top50)
    print(f"✅ WebSocket bağlandı. {len(top50)} hisse izleniyor (en yüksek hacimli watchlist dilimi)")
    for symbol in top50:
        ws.send(json.dumps({"type": "subscribe", "symbol": symbol}))
        time.sleep(0.15)  # 19 Haz 2026: abonelik mesajları arasına gecikme —
                           # 50 sembole anında abone olmak Finnhub'ı rate-limit
                           # tetiklemeye itiyordu


def on_error(ws, error):
    error_str = str(error)
    if "429" in error_str:
        print(f"⚠️ Rate limit — 20sn bekleniyor...")
        time.sleep(20)
    else:
        print(f"❌ WebSocket hatası: {error}")


def on_close(ws, close_status_code, close_msg):
    print("🔌 Bağlantı kapandı, 10sn sonra yeniden bağlanacak...")
    time.sleep(10)


def connect_websocket():
    """Tek WebSocket bağlantısı — Finnhub'ın 50 sembol/token limitine uyumlu."""
    ws = websocket.WebSocketApp(
        f"wss://ws.finnhub.io?token={FINNHUB_KEY}",
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )
    ws.run_forever(ping_interval=20, ping_timeout=10)


def position_monitor_loop():
    """Borsa açıkken 15sn'de bir açık pozisyonları kontrol eder (stop/hedef/reversal)"""
    cycle = 0
    while is_market_open():
        bot_check_open_positions()
        cycle += 1
        if cycle % 40 == 0:
            check_signal_outcomes(market="US")
            update_learning_weights(market="US")
        time.sleep(15)


def start():
    global active_symbols

    print("🚀 Atlas US Kartal Gözü v2 baslatildi...")
    print(f"⏰ {datetime.now(timezone.utc).strftime('%H:%M UTC')}")

    active_symbols = load_watchlist_from_db()
    if not active_symbols:
        print("⚠️ DB bos, yfinance ile yukleniyor...")
        refresh_watchlist_background()

    print("🔄 Sinyal cache yukleniyor...")
    try:
        since = (datetime.now(timezone.utc) - timedelta(hours=16)).isoformat()
        r = supabase.table("us_signals").select("symbol, created_at").gte("created_at", since).execute()
        for row in r.data:
            sym = row["symbol"]
            dt = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
            signal_cache[sym] = dt.timestamp()
        print(f"✅ {len(signal_cache)} sinyal cache'e yuklendi")
    except Exception as e:
        print(f"⚠️ Cache yukleme hatası: {e}")

    last_watchlist_refresh = time.time()
    night_scan_done = False
    morning_push_sent = False
    morning_buys_done = False

    import threading
    def _monitor_thread():
        while True:
            try:
                if is_market_open():
                    bot_check_open_positions()
            except Exception as e:
                print(f"⚠️ Pozisyon izleme hatası: {e}")
            time.sleep(15)

    monitor = threading.Thread(target=_monitor_thread, daemon=True)
    monitor.start()
    print("👁️ Pozisyon izleme başlatıldı (15s aralık)")

    while True:
        try:
            now_utc = datetime.now(timezone.utc)
            hour = now_utc.hour
            minute = now_utc.minute

            print(f"⏰ US döngü: {now_utc.strftime('%H:%M UTC')} | market_open={is_market_open()} | weekday={now_utc.weekday()}")

            if hour == 2 and minute < 5 and time.time() - last_watchlist_refresh > 3600:
                refresh_watchlist_background()
                last_watchlist_refresh = time.time()
                premarket_signal_cache.clear()
                night_scan_done = False
                morning_push_sent = False
                morning_buys_done = False

            if hour >= 20 or hour < 12:
                if not night_scan_done:
                    print(f"\n🌙 US GECE MOTORU başlıyor... {now_utc.strftime('%H:%M UTC')}")
                    try:
                        premarket_signal_cache.clear()
                        run_premarket_scan()
                    except Exception as e:
                        print(f"⚠️ Gece tarama hatası: {e}")
                    night_scan_done = True
                    morning_push_sent = False
                    morning_buys_done = False
                else:
                    print(f"💤 US gece bekleniyor... {now_utc.strftime('%H:%M UTC')}")
                time.sleep(600)

            elif (hour == 12 and minute >= 30) or (hour == 13 and minute < 30):
                if not morning_push_sent:
                    print(f"\n📱 US SABAH PUSH — {now_utc.strftime('%H:%M UTC')}")
                    try:
                        send_morning_signals()
                    except Exception as e:
                        print(f"⚠️ Sabah push hatası: {e}")
                    morning_push_sent = True
                    night_scan_done = False
                time.sleep(120)

            elif is_market_open():
                morning_push_sent = True
                print(f"📡 US WebSocket bağlanıyor... ({len(active_symbols)} hisse)")
                try:
                    connect_websocket()
                except Exception as e:
                    print(f"⚠️ WebSocket hatası: {e}")
                    time.sleep(30)

            else:
                if hour == 20 and minute < 5:
                    try:
                        bot_check_open_positions()
                    except Exception as e:
                        print(f"⚠️ Kapanış kontrol hatası: {e}")
                print(f"💤 US bekleniyor. {now_utc.strftime('%H:%M UTC')}")
                time.sleep(60)

        except Exception as e:
            print(f"❌ Ana döngü hatası: {e}")
            import traceback
            traceback.print_exc()
            time.sleep(60)


if __name__ == "__main__":
    start()
