# -*- coding: utf-8 -*-
import os
import json
import time
import requests
import websocket
import yfinance as yf
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

try:
    if FIREBASE_SERVICE_ACCOUNT and not firebase_admin._apps:
        cred = credentials.Certificate(json.loads(FIREBASE_SERVICE_ACCOUNT))
        firebase_admin.initialize_app(cred)
        print("✅ Firebase Admin baslatildi")
except Exception as e:
    print(f"⚠️ Firebase baslatma hatası: {e}")


# ============================================================
# PUSH NOTIFICATION
# ============================================================

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
        print(f"❌ Push notification hatası: {e}")


# ============================================================
# ZAMAN KONTROL
# ============================================================

def is_market_open():
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5:
        return False
    return now.replace(hour=13, minute=30, second=0, microsecond=0) <= now <= now.replace(hour=20, minute=0, second=0, microsecond=0)


def is_premarket():
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5:
        return False
    return now.replace(hour=9, minute=30, second=0, microsecond=0) <= now < now.replace(hour=13, minute=30, second=0, microsecond=0)


# ============================================================
# WATCHLIST — SUPABASE'DEN YUKLE
# ============================================================

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
                        if 1.0 <= price <= 20.0 and vol >= 500_000:
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


# ============================================================
# KARTAL GÖZÜ — PRE-MARKET KATALIZ MOTORU
# ============================================================

def get_earnings_today():
    """Bugün earnings raporu açıklayacak hisseler"""
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
            hour = e.get("hour", "")
            # BMO = Before Market Open, sabah açıklayanlar
            if sym and hour in ["bmo", "amc", ""]:
                symbols.append({
                    "symbol": sym,
                    "eps_estimate": e.get("epsEstimate"),
                    "eps_actual": e.get("epsActual"),
                    "hour": hour
                })
        print(f"📅 Bugün {len(symbols)} şirket earnings açıklıyor")
        return symbols
    except Exception as e:
        print(f"⚠️ Earnings takvim hatası: {e}")
        return []


def get_analyst_upgrades_today():
    """Son 24 saatte analist upgrade alan hisseler"""
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        r = requests.get(
            f"https://finnhub.io/api/v1/calendar/recommendation?from={yesterday}&to={today}&token={FINNHUB_KEY}",
            timeout=10
        )
        data = r.json()
        upgrades = []
        if isinstance(data, list):
            for item in data:
                action = item.get("action", "")
                if action in ["upgrade", "init", "reiterated"]:
                    upgrades.append({
                        "symbol": item.get("symbol", ""),
                        "from": item.get("fromGrade", ""),
                        "to": item.get("toGrade", ""),
                        "firm": item.get("company", ""),
                        "action": action
                    })
        print(f"📈 Son 24 saatte {len(upgrades)} analist aksiyonu")
        return upgrades
    except Exception as e:
        print(f"⚠️ Analist upgrade hatası: {e}")
        return []


def get_news_catalyst(symbol):
    """Son 48 saatte önemli haber var mı — sadece gerçek kataliz"""
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

        # Önemli anahtar kelimeler — gerçek kataliz
        catalyst_keywords = [
            "earnings", "revenue", "profit", "beat", "exceed", "guidance",
            "upgrade", "buy", "outperform", "target", "raised",
            "fda", "approval", "approved", "patent",
            "merger", "acquisition", "buyout", "deal",
            "contract", "partnership", "agreement",
            "dividend", "buyback",
            "ceo", "appointed", "hired",
        ]

        for news in news_list[:10]:
            headline = news.get("headline", "").lower()
            summary = news.get("summary", "").lower()
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


def get_gap_data(symbol):
    """Dün kapanış vs bugün beklenen açılış — gap hesapla"""
    try:
        t = yf.Ticker(symbol)
        hist = t.history(period="5d").dropna()
        if len(hist) < 2:
            return None

        prev_close = float(hist['Close'].iloc[-1])
        volumes = hist['Volume'].tolist()
        closes = hist['Close'].tolist()

        avg_vol = sum(volumes[:-1]) / len(volumes[:-1]) if len(volumes) > 1 else 0
        last_vol = volumes[-1]
        vol_ratio = last_vol / avg_vol if avg_vol > 0 else 0

        # 5 günlük trend
        five_day_change = ((closes[-1] - closes[-5]) / closes[-5]) * 100 if len(closes) >= 5 else 0
        prev_day_change = ((closes[-1] - closes[-2]) / closes[-2]) * 100 if len(closes) >= 2 else 0

        return {
            "prev_close": round(prev_close, 2),
            "vol_ratio": round(vol_ratio, 2),
            "five_day_change": round(five_day_change, 2),
            "prev_day_change": round(prev_day_change, 2),
            "avg_vol": int(avg_vol),
            "last_vol": int(last_vol)
        }
    except:
        return None


def get_insider_recent(symbol):
    """Son 30 günde insider alımı var mı"""
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


def calculate_premarket_score(symbol, gap_data, catalyst, earnings, analyst_upgrade, insider):
    """
    Profesyonel pre-market puanlama sistemi
    Kataliz olmadan sinyal yok — bu kural değişmez
    """
    score = 0
    reasons = []
    conviction = "NORMAL"

    # ZORUNLU: Kataliz yoksa sinyal yok
    has_catalyst = bool(catalyst or earnings or analyst_upgrade)
    if not has_catalyst:
        return "NORMAL", [], 0

    if not gap_data:
        return "NORMAL", [], 0

    # 1. EARNINGS — en güçlü kataliz
    if earnings:
        eps_est = earnings.get("eps_estimate")
        eps_act = earnings.get("eps_actual")
        if eps_est and eps_act:
            if eps_act > eps_est:
                beat_pct = ((eps_act - eps_est) / abs(eps_est)) * 100 if eps_est != 0 else 0
                score += 5
                reasons.append(f"Earnings BEAT: %{beat_pct:.0f} üstünde ({eps_act} vs {eps_est} tahmin)")
            else:
                score -= 2
                reasons.append(f"Earnings MISS: {eps_act} vs {eps_est} tahmin")
        else:
            score += 2
            reasons.append("Earnings açıklaması bugün")

    # 2. ANALIST UPGRADE
    if analyst_upgrade:
        score += 4
        reasons.append(f"Analist {analyst_upgrade['action'].upper()}: {analyst_upgrade['firm']} — {analyst_upgrade['from']} → {analyst_upgrade['to']}")

    # 3. HABER KATALİZİ
    if catalyst:
        score += 3
        reasons.append(f"Kataliz haber [{catalyst['keyword']}]: {catalyst['headline'][:70]}")

    # 4. INSIDER ALIMI
    if insider:
        score += 3
        reasons.append(f"Son 30g Insider: {insider}")

    # 5. HACİM ARTIŞI — katalizi doğrular
    if gap_data["vol_ratio"] >= 2:
        score += 3
        reasons.append(f"Hacim {gap_data['vol_ratio']}x — kurumsal ilgi doğrulandı")
    elif gap_data["vol_ratio"] >= 1.5:
        score += 2
        reasons.append(f"Hacim {gap_data['vol_ratio']}x artışı")
    elif gap_data["vol_ratio"] < 0.8:
        score -= 1
        reasons.append(f"Hacim düşük {gap_data['vol_ratio']}x — ilgi zayıf")

    # 6. FİYAT TREND
    if gap_data["prev_day_change"] >= 4:
        score += 2
        reasons.append(f"Dün +{gap_data['prev_day_change']}% güçlü kapanış")
    elif gap_data["prev_day_change"] >= 2:
        score += 1
        reasons.append(f"Dün +{gap_data['prev_day_change']}% pozitif")
    elif gap_data["prev_day_change"] <= -4:
        score -= 1
        reasons.append(f"Dün {gap_data['prev_day_change']}% zayıf kapanış")

    # SONUÇ
    if score >= 10:
        conviction = "CRITICAL"
    elif score >= 7:
        conviction = "HIGH"
    elif score >= 4:
        conviction = "MEDIUM"
    else:
        conviction = "NORMAL"

    return conviction, reasons, score


def get_ai_premarket_explanation(symbol, company_name, sector, gap_data, catalyst, earnings, analyst_upgrade, insider, conviction, reasons):
    try:
        earnings_str = f"Earnings: {earnings}" if earnings else "Earnings: None"
        analyst_str = f"Analyst: {analyst_upgrade['firm']} {analyst_upgrade['action']} {analyst_upgrade['from']}→{analyst_upgrade['to']}" if analyst_upgrade else "Analyst: None"
        catalyst_str = f"News: {catalyst['headline'][:100]}" if catalyst else "News: None"
        insider_str = f"Insider: {insider}" if insider else "Insider: None"

        prompt = f"""Professional financial analyst. Pre-market analysis. Be specific and actionable.

Stock: {symbol} ({company_name}) | Sector: {sector}
Price: ${gap_data['prev_close'] if gap_data else 'N/A'}
Yesterday: {gap_data['prev_day_change'] if gap_data else 'N/A':+}% | 5-Day: {gap_data['five_day_change'] if gap_data else 'N/A':+}%
Volume Ratio: {gap_data['vol_ratio'] if gap_data else 'N/A'}x
Conviction: {conviction} (Score drives this)
{earnings_str}
{analyst_str}
{catalyst_str}
{insider_str}
Key Reasons: {' | '.join(reasons)}

===BEGINNER===
[Max 2 sentences. What is happening and what to expect at open. No jargon.]
===INTERMEDIATE===
[Max 3 sentences. Technical setup + catalyst context + key price level to watch.]
===PRO===
[Max 4 sentences. Institutional probability, catalyst strength, risk/reward ratio, specific entry/stop/target levels in dollars.]"""

        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"},
            json={
                "model": "llama-3.1-8b-instant",
                "messages": [
                    {"role": "system", "content": "You are a professional financial analyst. Use ONLY the exact format given. Never change headers. Be specific, not generic."},
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


def run_premarket_scan():
    """
    KARTAL GÖZÜ — Profesyonel pre-market tarama
    Borsa açılmadan önce çalışır, sadece kataliz olan hisseleri tarar
    """
    print("\n" + "="*50)
    print("🦅 KARTAL GÖZÜ — PRE-MARKET ANALIZ")
    now_utc = datetime.now(timezone.utc)
    minutes_to_open = int((13.5 - now_utc.hour - now_utc.minute / 60) * 60)
    print(f"⏰ Acilisa {minutes_to_open} dakika var")
    print("="*50)

    # 1. BUGÜN EARNINGS AÇIKLAYACAKLAR
    print("\n📅 Earnings takvimi kontrol ediliyor...")
    earnings_today = get_earnings_today()
    earnings_map = {e["symbol"]: e for e in earnings_today}

    # 2. ANALIST UPGRADE/DOWNGRADE
    print("📈 Analist aksiyonları kontrol ediliyor...")
    upgrades_today = get_analyst_upgrades_today()
    upgrades_map = {u["symbol"]: u for u in upgrades_today}

    # 3. WATCHLIST + EARNINGS + UPGRADES BİRLEŞTİR
    all_targets = set(list(avg_volumes.keys()) + list(earnings_map.keys()) + list(upgrades_map.keys()))
    print(f"🔍 {len(all_targets)} hisse taranıyor (watchlist + earnings + upgrades)...")

    signals = []

    for symbol in all_targets:
        try:
            if symbol in premarket_signal_cache:
                continue

            # Şirket bilgisi
            company = company_cache.get(symbol, {"name": "", "sector": ""})
            company_name = company.get("name", "")
            sector = company.get("sector", "")

            # Earnings ve analist
            earnings = earnings_map.get(symbol)
            analyst_upgrade = upgrades_map.get(symbol)

            # Haber katalizi
            catalyst = get_news_catalyst(symbol)
            time.sleep(0.05)

            # Kataliz yok = atla
            if not catalyst and not earnings and not analyst_upgrade:
                continue

            # Fiyat ve hacim verisi
            gap_data = get_gap_data(symbol)
            if not gap_data:
                continue

            # Fiyat aralığı kontrolü — $1-20
            if not (1.0 <= gap_data["prev_close"] <= 20.0):
                # Earnings varsa büyük hisseler de dahil
                if not earnings:
                    continue

            # Insider
            insider = get_insider_recent(symbol)

            # PUANLAMA
            conviction, reasons, score = calculate_premarket_score(
                symbol, gap_data, catalyst, earnings, analyst_upgrade, insider
            )

            if conviction == "NORMAL":
                continue

            signals.append({
                "symbol": symbol,
                "company_name": company_name,
                "sector": sector,
                "gap_data": gap_data,
                "catalyst": catalyst,
                "earnings": earnings,
                "analyst_upgrade": analyst_upgrade,
                "insider": insider,
                "conviction": conviction,
                "reasons": reasons,
                "score": score
            })

            print(f"  🎯 {symbol} ({company_name}) | {conviction} | Score: {score}")
            for r in reasons:
                print(f"     → {r}")

            time.sleep(0.3)

        except Exception as e:
            print(f"❌ {symbol}: {e}")
            continue

    # EN YÜKSEK PUANLILARI SEÇ — max 5 sinyal
    signals.sort(key=lambda x: x["score"], reverse=True)
    top_signals = signals[:5]

    print(f"\n✅ {len(signals)} aday bulundu, en iyi {len(top_signals)} sinyal gönderiliyor...")

    for sig in top_signals:
        try:
            symbol = sig["symbol"]
            conviction = sig["conviction"]
            gap_data = sig["gap_data"]
            reasons = sig["reasons"]

            ai_text = get_ai_premarket_explanation(
                symbol, sig["company_name"], sig["sector"],
                gap_data, sig["catalyst"], sig["earnings"],
                sig["analyst_upgrade"], sig["insider"],
                conviction, reasons
            )
            acemi, usta, pro = parse_ai_levels(ai_text)

            emoji = "🔥" if conviction == "CRITICAL" else "⚡" if conviction == "HIGH" else "👁️"
            description = f"{emoji} PRE-MARKET | {symbol}"
            if sig["company_name"]:
                description += f" ({sig['company_name']})"
            description += f" | ${gap_data['prev_close']} | Dun: {gap_data['prev_day_change']:+}%"
            if sig["catalyst"]:
                description += f" | 📰 {sig['catalyst']['headline'][:60]}"
            if sig["earnings"]:
                description += f" | 📊 Earnings"
            if sig["analyst_upgrade"]:
                description += f" | 📈 {sig['analyst_upgrade']['firm']} {sig['analyst_upgrade']['action']}"

            result = supabase.table("us_signals").insert({
                "symbol": symbol,
                "signal_type": "premarket",
                "value": gap_data["prev_day_change"],
                "description": description,
                "acemi_explanation": acemi,
                "usta_explanation": usta,
                "pro_explanation": pro,
                "price": gap_data["prev_close"],
                "volume_ratio": gap_data["vol_ratio"],
                "created_at": datetime.now(timezone.utc).isoformat()
            }).execute()

            signal_id = result.data[0].get("id") if result.data else None
            premarket_signal_cache[symbol] = time.time()

            push_body = f"${gap_data['prev_close']} | {gap_data['prev_day_change']:+}% | {conviction}"
            if sig["company_name"]:
                push_body = f"{sig['company_name']} — {push_body}"

            send_push_notification(
                title=f"{emoji} {symbol} — Acilis Oncesi Sinyal",
                body=push_body,
                market="US",
                signal_id=signal_id
            )

            print(f"✅ SINYAL: {description}")

        except Exception as e:
            print(f"❌ Sinyal kayıt hatası {sig['symbol']}: {e}")

    print(f"\n🦅 Pre-market tarama tamamlandi. {len(top_signals)} sinyal gonderildi.")


# ============================================================
# CANLI MARKET — WEBSOCKET
# ============================================================

def get_last_signal_time(symbol):
    try:
        r = supabase.table("us_signals").select("created_at").eq("symbol", symbol).order("created_at", ascending=False).limit(1).execute()
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


def process_live_signal(symbol, signal_type, price, price_change, volume_ratio):
    if not is_market_open():
        return
    if price < 1.0 or price > 20.0:
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

    # Canlı sinyalde de kataliz kontrol et
    catalyst = get_news_catalyst(symbol)
    insider = get_insider_recent(symbol)
    has_catalyst = bool(catalyst or insider)

    score = 0
    if abs(price_change) >= 10:
        score += 3
    elif abs(price_change) >= 5:
        score += 2
    elif abs(price_change) >= 3:
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

    conviction = "HIGH" if score >= 7 else "MEDIUM" if score >= 4 else "NORMAL"

    # NORMAL + kataliz yok = sinyal yok
    if conviction == "NORMAL" and not has_catalyst:
        return

    company = company_cache.get(symbol, {"name": "", "sector": ""})
    company_name = company.get("name", "")
    sector = company.get("sector", "")

    print(f"📊 CANLI: {symbol} ({company_name}) | {conviction} | {price_change:+.1f}% | {volume_ratio:.1f}x")

    emoji = "🔥" if conviction == "HIGH" else "⚡"
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

        bot_process_signal(symbol, price, price_change, volume_ratio, conviction, signal_id)

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


# ============================================================
# BOT — KURAL TABANLI
# ============================================================

def bot_should_buy(price_change, volume_ratio, conviction):
    if price_change < 0:
        return False
    if conviction in ["CRITICAL", "HIGH"]:
        return True
    if price_change >= 3 and volume_ratio >= 3:
        return True
    if conviction == "MEDIUM" and price_change >= 1 and volume_ratio >= 2:
        return True
    if volume_ratio >= 5 and price_change > 0:
        return True
    return False


def bot_should_sell(buy_price, current_price):
    change = ((current_price - buy_price) / buy_price) * 100
    if change >= 10:
        print(f"  💰 %{change:.1f} kar — SAT")
        return True
    if change <= -5:
        print(f"  🛑 %{change:.1f} zarar — STOP LOSS")
        return True
    return False


def bot_buy(user_id, symbol, price, signal_id, is_pro, balance):
    try:
        if not is_pro:
            month_start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0).isoformat()
            month_trades = supabase.table("demo_trades").select("id").eq("user_id", user_id).gte("created_at", month_start).execute()
            if len(month_trades.data) >= 3:
                print(f"⚠️ {user_id} aylik limit doldu")
                return False
            open_trades = supabase.table("demo_trades").select("id").eq("user_id", user_id).eq("status", "open").execute()
            if len(open_trades.data) >= 1:
                print(f"⚠️ {user_id} açık pozisyon var")
                return False

        invest = min(balance * 0.10, 100)
        if invest < 10:
            return False
        quantity = invest / price

        supabase.table("demo_trades").insert({
            "user_id": user_id, "symbol": symbol, "market": "US",
            "signal_id": signal_id, "buy_price": price,
            "buy_date": datetime.now(timezone.utc).isoformat(),
            "quantity": round(quantity, 4), "status": "open",
            "created_at": datetime.now(timezone.utc).isoformat()
        }).execute()

        supabase.table("demo_portfolios").update({
            "balance": round(balance - invest, 2)
        }).eq("user_id", user_id).execute()

        print(f"✅ BOT ALIM: {user_id} → {symbol} @ ${price:.2f}")
        return True
    except Exception as e:
        print(f"❌ Bot alım hatası: {e}")
        return False


def bot_sell(trade, current_price):
    try:
        profit_loss = (current_price - trade["buy_price"]) * trade["quantity"]
        supabase.table("demo_trades").update({
            "sell_price": current_price,
            "sell_date": datetime.now(timezone.utc).isoformat(),
            "status": "closed",
            "profit_loss": round(profit_loss, 2)
        }).eq("id", trade["id"]).execute()

        portfolio = supabase.table("demo_portfolios").select("balance").eq("user_id", trade["user_id"]).maybeSingle().execute()
        if portfolio.data:
            new_balance = portfolio.data["balance"] + (trade["quantity"] * current_price)
            supabase.table("demo_portfolios").update({"balance": round(new_balance, 2)}).eq("user_id", trade["user_id"]).execute()

        print(f"✅ BOT SATIŞ: {trade['symbol']} | K/Z: ${profit_loss:.2f}")
    except Exception as e:
        print(f"❌ Bot satış hatası: {e}")


def bot_check_open_positions():
    try:
        trades = supabase.table("demo_trades").select("*").eq("status", "open").eq("market", "US").execute()
        if not trades.data:
            return
        print(f"🔍 {len(trades.data)} açık US pozisyon kontrol ediliyor...")
        for trade in trades.data:
            try:
                t = yf.Ticker(trade["symbol"])
                hist = t.history(period="1d", interval="1m").dropna()
                if hist.empty:
                    continue
                current_price = float(hist["Close"].iloc[-1])
                if bot_should_sell(trade["buy_price"], current_price):
                    bot_sell(trade, current_price)
            except:
                continue
            time.sleep(0.3)
    except Exception as e:
        print(f"❌ Pozisyon kontrol hatası: {e}")


def bot_process_signal(symbol, price, price_change, volume_ratio, conviction, signal_id):
    try:
        if not bot_should_buy(price_change, volume_ratio, conviction):
            return
        print(f"🤖 Bot {symbol}: AL")

        portfolios = supabase.table("demo_portfolios").select("user_id, balance").execute()
        if not portfolios.data:
            return

        for portfolio in portfolios.data:
            user_id = portfolio["user_id"]
            balance = portfolio["balance"]
            if balance < 10:
                continue
            profile = supabase.table("profiles").select("is_pro").eq("id", user_id).maybeSingle().execute()
            is_pro = profile.data.get("is_pro", False) if profile.data else False
            bot_buy(user_id, symbol, price, signal_id, is_pro, balance)
            time.sleep(0.2)
    except Exception as e:
        print(f"❌ Bot sinyal işleme hatası: {e}")


# ============================================================
# WEBSOCKET
# ============================================================

def on_message(ws, message):
    try:
        data = json.loads(message)
        if data.get("type") != "trade":
            return
        for trade in data.get("data", []):
            symbol = trade.get("s")
            price = float(trade.get("p", 0))
            volume = float(trade.get("v", 0))
            if not symbol or not price or price < 1.0 or price > 20.0:
                continue
            tracker.update(symbol, price, volume)
            if symbol in avg_volumes:
                update_live_price(symbol, price)
            avg_vol = avg_volumes.get(symbol, 0)
            if avg_vol == 0:
                continue
            volume_ratio = tracker.get_volume_ratio(symbol, avg_vol)
            price_change = tracker.get_price_change(symbol, price)
            if volume_ratio >= 2 and price_change >= 3:
                process_live_signal(symbol, "momentum", price, price_change, volume_ratio)
            elif volume_ratio >= 5 and price_change > 0:
                process_live_signal(symbol, "volume_spike", price, price_change, volume_ratio)
    except Exception as e:
        print(f"WS mesaj hatası: {e}")


def on_open(ws):
    print(f"✅ WebSocket bağlandı. {len(active_symbols)} hisse izleniyor...")
    for symbol in active_symbols:
        ws.send(json.dumps({"type": "subscribe", "symbol": symbol}))


def on_error(ws, error):
    print(f"❌ WebSocket hatası: {error}")


def on_close(ws, close_status_code, close_msg):
    print("🔌 Bağlantı kapandı, 5sn sonra yeniden...")
    time.sleep(5)
    connect_websocket()


def connect_websocket():
    ws = websocket.WebSocketApp(
        f"wss://ws.finnhub.io?token={FINNHUB_KEY}",
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )
    ws.run_forever()


# ============================================================
# ANA DONGU
# ============================================================

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
        since = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        r = supabase.table("us_signals").select("symbol, created_at").gte("created_at", since).execute()
        for row in r.data:
            sym = row["symbol"]
            dt = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
            signal_cache[sym] = dt.timestamp()
        print(f"✅ {len(signal_cache)} sinyal cache'e yuklendi")
    except Exception as e:
        print(f"⚠️ Cache yukleme hatası: {e}")

    premarket_done = False
    last_watchlist_refresh = time.time()

    while True:
        try:
            now_utc = datetime.now(timezone.utc)
            hour = now_utc.hour
            minute = now_utc.minute

            # Gece 02:00 UTC watchlist yenile
            if hour == 2 and minute < 5 and time.time() - last_watchlist_refresh > 3600:
                print("🌙 Gece watchlist yenileniyor...")
                refresh_watchlist_background()
                last_watchlist_refresh = time.time()
                premarket_signal_cache.clear()
                premarket_done = False

            # PRE-MARKET — kartal gözü
            if is_premarket() and not premarket_done:
                run_premarket_scan()
                premarket_done = True
                print("💤 Pre-market bitti. Açılış bekleniyor...")
                time.sleep(300)

            # BORSA ACIK — WebSocket
            elif is_market_open():
                premarket_done = False
                print(f"📡 WebSocket bağlanıyor... ({len(active_symbols)} hisse)")
                connect_websocket()

            # BORSA KAPALI
            else:
                if hour == 21 and minute < 5:
                    bot_check_open_positions()
                    premarket_done = False
                print(f"💤 Borsa kapali. {now_utc.strftime('%H:%M UTC')}")
                time.sleep(300)

        except Exception as e:
            print(f"❌ Ana döngü hatası: {e}")
            time.sleep(60)


if __name__ == "__main__":
    start()
