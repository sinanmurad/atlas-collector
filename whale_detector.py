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
premarket_signal_cache = {}  # Açılış öncesi sinyal cache

try:
    if FIREBASE_SERVICE_ACCOUNT and not firebase_admin._apps:
        cred = credentials.Certificate(json.loads(FIREBASE_SERVICE_ACCOUNT))
        firebase_admin.initialize_app(cred)
        print("✅ Firebase Admin başlatıldı")
except Exception as e:
    print(f"⚠️ Firebase başlatma hatası: {e}")


def send_push_notification(title, body, market="US", signal_id=None):
    try:
        profiles = supabase.table("profiles") \
            .select("fcm_token") \
            .not_.is_("fcm_token", "null") \
            .execute()
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
                        notification=messaging.AndroidNotification(
                            channel_id="atlas_signals",
                        ),
                    ),
                    apns=messaging.APNSConfig(
                        payload=messaging.APNSPayload(
                            aps=messaging.Aps(sound="default", badge=1)
                        )
                    ),
                    token=token,
                )
                messaging.send(message)
            except Exception as e:
                print(f"⚠️ Push hatası: {e}")
        print(f"📱 Push gönderildi: {len(tokens)} kullanıcı")
    except Exception as e:
        print(f"❌ Push notification hatası: {e}")


def is_market_open():
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5:
        return False
    open_t = now.replace(hour=13, minute=30, second=0, microsecond=0)
    close_t = now.replace(hour=20, minute=0, second=0, microsecond=0)
    return open_t <= now <= close_t


def is_premarket():
    """ABD açılışından 4 saat önce — kartal gözü aktif"""
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5:
        return False
    premarket_start = now.replace(hour=9, minute=30, second=0, microsecond=0)
    market_open = now.replace(hour=13, minute=30, second=0, microsecond=0)
    return premarket_start <= now < market_open


def get_nasdaq_symbols():
    try:
        r = requests.get(
            'https://raw.githubusercontent.com/datasets/nasdaq-listings/main/data/nasdaq-listed.csv',
            timeout=10
        )
        lines = r.text.strip().split('\n')
        return [l.split(',')[0] for l in lines[1:]
                if l.split(',')[0].isalpha() and 2 <= len(l.split(',')[0]) <= 5]
    except:
        return []


def get_nyse_symbols():
    try:
        r = requests.get(
            'https://raw.githubusercontent.com/datasets/nyse-listings/main/data/nyse-listed.csv',
            timeout=10
        )
        lines = r.text.strip().split('\n')
        return [l.split(',')[0] for l in lines[1:]
                if l.split(',')[0].isalpha() and 2 <= len(l.split(',')[0]) <= 5]
    except:
        return []


def get_company_profile(symbol):
    if symbol in company_cache:
        return company_cache[symbol]
    try:
        r = requests.get(
            f"https://finnhub.io/api/v1/stock/profile2?symbol={symbol}&token={FINNHUB_KEY}",
            timeout=5
        )
        data = r.json()
        profile = {
            "name": data.get("name", ""),
            "sector": data.get("finnhubIndustry", ""),
            "market_cap": int(data.get("marketCapitalization", 0) * 1_000_000) if data.get("marketCapitalization") else 0,
            "country": data.get("country", ""),
            "exchange": data.get("exchange", ""),
        }
        company_cache[symbol] = profile
        return profile
    except:
        return {"name": "", "sector": "", "market_cap": 0, "country": "", "exchange": ""}


def get_news(symbol, days=1):
    cache_key = f"{symbol}_{days}"
    if cache_key in news_cache:
        cached_time, cached_news = news_cache[cache_key]
        if time.time() - cached_time < 1800:
            return cached_news
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        from_date = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        r = requests.get(
            f"https://finnhub.io/api/v1/company-news?symbol={symbol}&from={from_date}&to={today}&token={FINNHUB_KEY}",
            timeout=5
        )
        news_list = r.json()
        headlines = [n.get("headline", "") for n in news_list[:3]] if isinstance(news_list, list) else []
        result = " | ".join(headlines) if headlines else ""
        news_cache[cache_key] = (time.time(), result)
        return result
    except:
        return ""


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
            buy = latest.get("buy", 0)
            hold = latest.get("hold", 0)
            sell = latest.get("sell", 0)
            strong_buy = latest.get("strongBuy", 0)
            result = {"buy": buy + strong_buy, "hold": hold, "sell": sell}
            analyst_cache[symbol] = (time.time(), result)
            return result
        return None
    except:
        return None


def get_insider(symbol):
    try:
        r = requests.get(
            f"https://finnhub.io/api/v1/stock/insider-transactions?symbol={symbol}&token={FINNHUB_KEY}",
            timeout=5
        )
        data = r.json().get("data", [])
        purchases = [t for t in data[:10] if t.get("transactionCode") == "P-Purchase"]
        if len(purchases) >= 2:
            return f"{len(purchases)} insider alımı"
        if purchases:
            return f"{purchases[0].get('name', '')} bought {purchases[0].get('share', 0):,} shares"
        return ""
    except:
        return ""


def get_5day_trend(symbol):
    """5 günlük trend — düşüş mü yükseliş mi, momentum var mı"""
    try:
        t = yf.Ticker(symbol)
        hist = t.history(period="10d").dropna()
        if len(hist) < 3:
            return None
        closes = hist['Close'].tolist()
        volumes = hist['Volume'].tolist()
        # Son 5 gün trendi
        last5 = closes[-5:]
        trend = "up" if last5[-1] > last5[0] else "down"
        # Dün kapanış değişimi
        prev_change = ((closes[-1] - closes[-2]) / closes[-2]) * 100
        # Hacim trendi
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


def premarket_conviction(symbol, trend_data, news, analyst, insider):
    """
    Kartal gözü conviction — açılış öncesi analiz
    Borsa açılmadan önce hareket edecek hisseyi tespit et
    """
    score = 0
    reasons = []

    if not trend_data:
        return "NORMAL", []

    # 5 günlük düşüş + bounce adayı
    if trend_data["trend"] == "down" and trend_data["5d_change"] <= -5:
        score += 2
        reasons.append(f"5g düşüş {trend_data['5d_change']}% — bounce adayı")

    # Dün güçlü kapanış
    if trend_data["prev_change"] >= 3:
        score += 2
        reasons.append(f"Dün +{trend_data['prev_change']}% güçlü kapanış")
    elif trend_data["prev_change"] >= 1:
        score += 1
        reasons.append(f"Dün +{trend_data['prev_change']}% pozitif kapanış")

    # Hacim artışı
    if trend_data["vol_ratio"] >= 2:
        score += 3
        reasons.append(f"Hacim {trend_data['vol_ratio']}x — kurumsal ilgi")
    elif trend_data["vol_ratio"] >= 1.5:
        score += 2
        reasons.append(f"Hacim {trend_data['vol_ratio']}x artışı")

    # Haber katalisti
    if news:
        score += 3
        reasons.append(f"Haber: {news[:60]}")

    # Analist desteği
    if analyst:
        buy = analyst.get("buy", 0)
        sell = analyst.get("sell", 0)
        if buy >= 5 and sell == 0:
            score += 3
            reasons.append(f"Analist: {buy} AL 0 SAT")
        elif buy > sell:
            score += 1
            reasons.append(f"Analist: {buy} AL {sell} SAT")

    # Insider alımı
    if insider:
        score += 3
        reasons.append(f"Insider: {insider}")

    if score >= 8:
        return "CRITICAL", reasons
    elif score >= 6:
        return "HIGH", reasons
    elif score >= 4:
        return "MEDIUM", reasons
    return "NORMAL", reasons


def get_premarket_ai_explanation(symbol, company_name, sector, trend_data, news, insider, analyst, conviction, reasons):
    try:
        analyst_str = f"Buy={analyst['buy']} Hold={analyst['hold']} Sell={analyst['sell']}" if analyst else "N/A"
        prompt = f"""You are a financial analyst. A stock is being analyzed BEFORE market open.
Write a pre-market signal explanation in 3 levels.

Stock: {symbol} ({company_name})
Sector: {sector}
Last Close: ${trend_data['last_close'] if trend_data else 'N/A'}
Yesterday Change: {trend_data['prev_change'] if trend_data else 'N/A'}%
5-Day Change: {trend_data['5d_change'] if trend_data else 'N/A'}%
Volume Ratio: {trend_data['vol_ratio'] if trend_data else 'N/A'}x
Conviction: {conviction}
Reasons: {', '.join(reasons)}
News: {news if news else 'None'}
Insider: {insider if insider else 'None'}
Analyst Ratings: {analyst_str}

===BEGINNER===
[1-2 sentences, what to expect at market open, plain language]
===INTERMEDIATE===
[technical setup, volume analysis, key levels]
===PRO===
[catalyst analysis, risk/reward, institutional probability, entry strategy]"""

        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"},
            json={
                "model": "llama-3.1-8b-instant",
                "messages": [
                    {"role": "system", "content": "Financial analyst. Use exact format. Never change ===BEGINNER===, ===INTERMEDIATE===, ===PRO=== headers."},
                    {"role": "user", "content": prompt}
                ],
                "max_tokens": 600,
                "temperature": 0.3
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
    Kartal gözü — borsa açılmadan 4 saat önce çalışır
    Açılışta hareket edecek hisseleri önceden tespit eder
    """
    print(f"\n🦅
