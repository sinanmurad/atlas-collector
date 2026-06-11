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

try:
    if FIREBASE_SERVICE_ACCOUNT and not firebase_admin._apps:
        cred = credentials.Certificate(json.loads(FIREBASE_SERVICE_ACCOUNT))
        firebase_admin.initialize_app(cred)
        print("✅ Firebase Admin başlatıldı")
except Exception as e:
    print(f"⚠️ Firebase başlatma hatası: {e}")


def send_push_notification(title, body, market="US"):
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
                    data={"market": market},
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


def get_nasdaq_symbols():
    try:
        r = requests.get(
            'https://raw.githubusercontent.com/datasets/nasdaq-listings/main/data/nasdaq-listed.csv',
            timeout=10
        )
        lines = r.text.strip().split('\n')
        return [l.split(',')[0] for l in lines[1:] if l.split(',')[0].isalpha() and 2 <= len(l.split(',')[0]) <= 5]
    except:
        return []


def get_nyse_symbols():
    try:
        r = requests.get(
            'https://raw.githubusercontent.com/datasets/nyse-listings/main/data/nyse-listed.csv',
            timeout=10
        )
        lines = r.text.strip().split('\n')
        return [l.split(',')[0] for l in lines[1:] if l.split(',')[0].isalpha() and 2 <= len(l.split(',')[0]) <= 5]
    except:
        return []


def get_news(symbol):
    if symbol in news_cache:
        cached_time, cached_news = news_cache[symbol]
        if time.time() - cached_time < 1800:
            return cached_news
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        r = requests.get(
            f"https://finnhub.io/api/v1/company-news?symbol={symbol}&from={yesterday}&to={today}&token={FINNHUB_KEY}",
            timeout=5
        )
        news = r.json()
        headline = news[0].get("headline", "") if isinstance(news, list) and news else ""
        news_cache[symbol] = (time.time(), headline)
        return headline
    except:
        return ""


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


def get_conviction(price_change, volume_ratio, has_news, insider):
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
    if has_news:
        score += 3
    if insider:
        score += 2
    if score >= 7:
        return "HIGH"
    elif score >= 4:
        return "MEDIUM"
    return "NORMAL"


def get_ai_explanation(symbol, price, price_change, volume_ratio, news, insider, conviction):
    try:
        prompt = f"""You are a financial analyst. Write 3-level explanation for this signal.

Stock: {symbol}
Price: ${price:.2f}
Change: {price_change:+.1f}%
Volume: {volume_ratio:.1f}x above average
Conviction: {conviction}
News: {news if news else 'None'}
Insider: {insider if insider else 'None'}

===BEGINNER===
[1-2 sentences, plain language]
===INTERMEDIATE===
[technical analysis]
===PRO===
[professional analysis with catalyst context]"""

        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"},
            json={
                "model": "llama-3.1-8b-instant",
                "messages": [
                    {"role": "system", "content": "You are a financial analyst. Use only the given format. Never change ===BEGINNER===, ===INTERMEDIATE===, ===PRO=== headers."},
                    {"role": "user", "content": prompt}
                ],
                "max_tokens": 500,
                "temperature": 0.3
            },
            timeout=15
        )
        resp = r.json()
        if "choices" not in resp:
            print(f"⚠️ Groq: {resp.get('error', {}).get('message', '')}")
            return ""
        result = resp["choices"][0]["message"]["content"]
        print(f"✅ AI: {result[:50]}...")
        return result
    except Exception as e:
        print(f"⚠️ AI hatası: {e}")
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
        r = supabase.table("us_signals") \
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


def update_live_price(symbol, price):
    now = time.time()
    if symbol in last_price_update and now - last_price_update[symbol] < 60:
        return
    try:
        supabase.table("us_watchlist").upsert({
            "symbol": symbol,
            "last_price": round(price, 2),
            "updated_at": datetime.now(timezone.utc).isoformat()
        }).execute()
        last_price_update[symbol] = now
    except Exception as e:
        print(f"⚠️ Live price hatası {symbol}: {e}")


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


def process_signal(symbol, signal_type, price, price_change, volume_ratio):
    if not is_market_open():
        return
    if price < 1.0 or price > 20.0:
        return
    now = time.time()
    if symbol in signal_cache and now - signal_cache[symbol] < 3600:
        return
    last_time = get_last_signal_time(symbol)
    if now - last_time < 3600:
        signal_cache[symbol] = last_time
        return

    print(f"🔍 {symbol} araştırılıyor...")
    news = get_news(symbol)
    insider = get_insider(symbol)
    has_news = bool(news)
    conviction = get_conviction(price_change, volume_ratio, has_news, insider)

    if conviction == "NORMAL" and not has_news:
        return

    print(f"📊 {symbol} | {conviction} | Haber: {has_news} | Insider: {bool(insider)}")

    ai_text = get_ai_explanation(symbol, price, price_change, volume_ratio, news, insider, conviction)
    acemi, usta, pro = parse_ai_levels(ai_text)

    emoji = "🔥" if conviction == "HIGH" else "⚡" if conviction == "MEDIUM" else "🚀"
    description = f"{emoji} {symbol} | ${price:.2f} | {price_change:+.1f}% | Vol: {volume_ratio:.1f}x | {conviction}"
    if news:
        description += f" | 📰 {news[:80]}"
    if insider:
        description += f" | 🐋 {insider}"

    signal = {
        "symbol": symbol,
        "signal_type": signal_type,
        "value": round(price_change, 2),
        "description": description,
        "acemi_explanation": acemi,
        "usta_explanation": usta,
        "pro_explanation": pro,
        "price": price,
        "volume_ratio": round(volume_ratio, 2),
        "created_at": datetime.now(timezone.utc).isoformat()
    }

    try:
        supabase.table("us_signals").insert(signal).execute()
        signal_cache[symbol] = now
        print(f"✅ KAYDEDİLDİ [{conviction}]: {description}")
        send_push_notification(
            title=f"{emoji} {symbol} Sinyali",
            body=f"${price:.2f} | {price_change:+.1f}% | Vol: {volume_ratio:.1f}x | {conviction}",
            market="US"
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
            if volume_ratio >= 2 and abs(price_change) >= 3:
                process_signal(symbol, "momentum", price, price_change, volume_ratio)
            elif volume_ratio >= 5:
                process_signal(symbol, "volume_spike", price, price_change, volume_ratio)
    except Exception as e:
        print(f"Mesaj hatası: {e}")


def on_open(ws):
    print(f"✅ Bağlandı. {len(active_symbols)} hisse izleniyor...")
    for symbol in active_symbols:
        ws.send(json.dumps({"type": "subscribe", "symbol": symbol}))


def on_error(ws, error):
    print(f"❌ WebSocket hatası: {error}")


def on_close(ws, close_status_code, close_msg):
    print("🔌 Bağlantı kapandı, 5 saniye sonra yeniden bağlanıyor...")
    time.sleep(5)
    start()


def build_watchlist():
    print("📋 NASDAQ listesi yükleniyor...")
    nasdaq = get_nasdaq_symbols()
    print(f"  NASDAQ: {len(nasdaq)} sembol")

    print("📋 NYSE listesi yükleniyor...")
    nyse = get_nyse_symbols()
    print(f"  NYSE: {len(nyse)} sembol")

    all_symbols = list(set(nasdaq + nyse))
    print(f"  Toplam: {len(all_symbols)} sembol — yfinance ile filtre başlıyor...")

    candidates = []
    batch_size = 200
    total_batches = (len(all_symbols) + batch_size - 1) // batch_size

    for i in range(0, len(all_symbols), batch_size):
        batch = all_symbols[i:i + batch_size]
        batch_num = i // batch_size + 1
        try:
            data = yf.download(
                ' '.join(batch),
                period='5d',
                interval='1d',
                progress=False,
                threads=True
            )
            if data.empty:
                continue

            closes = data['Close'].iloc[-1]
            volumes = data['Volume'].mean()

            for symbol in batch:
                try:
                    price = float(closes[symbol])
                    vol = float(volumes[symbol])
                    if 1.0 <= price <= 20.0 and vol >= 500_000:
                        candidates.append(symbol)
                        avg_volumes[symbol] = vol
                        try:
                            supabase.table("us_watchlist").upsert({
                                "symbol": symbol,
                                "avg_volume": int(vol),
                                "last_price": round(price, 2),
                                "updated_at": datetime.now(timezone.utc).isoformat()
                            }).execute()
                        except Exception as e:
                            print(f"  ⚠️ Supabase upsert hatası {symbol}: {e}")
                        print(f"  ✅ {symbol}: ${price:.2f} | avg_vol={vol:,.0f}")
                except:
                    continue
        except Exception as e:
            print(f"  Batch {batch_num} hatası: {e}")
            continue

        print(f"  Batch {batch_num}/{total_batches} tamamlandı | {len(candidates)} aday")
        time.sleep(1)

    print(f"\n✅ {len(candidates)} hisse belirlendi")
    return candidates


def start():
    global active_symbols

    print("🚀 Atlas US Sinyal Motoru v2 başlatıldı...")
    print(f"⏰ {datetime.now(timezone.utc).strftime('%H:%M UTC')}")

    active_symbols = build_watchlist()

    if not active_symbols:
        print("⚠️ Hisse listesi boş, 60 saniye sonra tekrar deneniyor...")
        time.sleep(60)
        start()
        return

    print("🔄 Cache yükleniyor...")
    for symbol in active_symbols:
        last_time = get_last_signal_time(symbol)
        if last_time > 0:
            signal_cache[symbol] = last_time

    print(f"📡 WebSocket bağlanıyor... ({len(active_symbols)} hisse)")
    ws = websocket.WebSocketApp(
        f"wss://ws.finnhub.io?token={FINNHUB_KEY}",
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )
    ws.run_forever()


if __name__ == "__main__":
    start()
