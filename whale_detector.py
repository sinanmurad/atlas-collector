import os
import json
import time
import requests
import websocket
from datetime import datetime, timezone, timedelta
from supabase import create_client

FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
GROQ_KEY = os.environ.get("GROQ_API_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# Kaliteli hisseler — S&P500 ve NASDAQ100'den seçilmiş, $10+ fiyat
QUALITY_SYMBOLS = [
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "AMD",
    "NFLX", "ORCL", "CRM", "ADBE", "PYPL", "INTC", "QCOM", "AVGO",
    "TXN", "MU", "AMAT", "LRCX", "KLAC", "MRVL", "SMCI", "ARM",
    "PLTR", "SNOW", "DDOG", "ZS", "CRWD", "PANW", "NET", "MDB",
    "COIN", "HOOD", "RBLX", "UBER", "LYFT", "ABNB", "DASH", "SHOP",
    "SQ", "AFRM", "SOFI", "UPST", "LC", "OPEN", "OPFI", "DAVE",
    "RIVN", "LCID", "NIO", "XPEV", "LI", "JOBY", "ACHR", "ASTS",
    "IONQ", "RGTI", "QUBT", "ARQQ", "SOUN", "BBAI", "RCAT", "LUNR"
]

last_signal_time = {}

def is_market_open():
    """ABD borsası açık mı? 09:30-16:00 ET = 13:30-20:00 UTC"""
    now = datetime.now(timezone.utc)
    # Hafta sonu kontrolü
    if now.weekday() >= 5:
        return False
    market_open = now.replace(hour=13, minute=30, second=0, microsecond=0)
    market_close = now.replace(hour=20, minute=0, second=0, microsecond=0)
    return market_open <= now <= market_close

def get_quote(symbol):
    """Finnhub'dan günlük veri al — open, close, high, low, volume"""
    try:
        r = requests.get(
            f"https://finnhub.io/api/v1/quote?symbol={symbol}&token={FINNHUB_KEY}",
            timeout=5
        )
        q = r.json()
        return {
            "open": q.get("o", 0),
            "high": q.get("h", 0),
            "low": q.get("l", 0),
            "prev_close": q.get("pc", 0),
            "current": q.get("c", 0),
        }
    except:
        return None

def get_avg_volume(symbol):
    """Finnhub metric endpoint — free plan'da çalışıyor"""
    try:
        r = requests.get(
            f"https://finnhub.io/api/v1/stock/metric?symbol={symbol}&metric=all&token={FINNHUB_KEY}",
            timeout=5
        )
        data = r.json()
        # 10 günlük ortalama hacim
        avg_vol = data.get("metric", {}).get("10DayAverageTradingVolume", 0)
        if avg_vol:
            return avg_vol * 1_000_000  # milyon cinsinden geliyor
        return 0
    except:
        return 0

def get_news(symbol):
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
        r = requests.get(
            f"https://finnhub.io/api/v1/company-news?symbol={symbol}&from={week_ago}&to={today}&token={FINNHUB_KEY}",
            timeout=5
        )
        news = r.json()
        if isinstance(news, list) and news:
            return news[0].get("headline", "")
        return ""
    except:
        return ""

def get_insider(symbol):
    try:
        r = requests.get(
            f"https://finnhub.io/api/v1/stock/insider-transactions?symbol={symbol}&token={FINNHUB_KEY}",
            timeout=5
        )
        data = r.json().get("data", [])
        for t in data[:5]:
            if t.get("transactionCode") == "P-Purchase":
                return f"{t.get('name')} bought {t.get('share', 0):,} shares"
        return ""
    except:
        return ""

def get_ai_explanation(symbol, price, price_change, volume_ratio, news, insider):
    try:
        prompt = f"""You are a financial analyst. Explain this signal at 3 levels:

Stock: {symbol} (US Market)
Price: ${price:.2f}
Change from open: {price_change:+.1f}%
Volume: {volume_ratio:.1f}x above average
News: {news if news else 'None'}
Insider: {insider if insider else 'None'}

===BEGINNER=== (1-2 sentences, plain language for someone who knows nothing about investing)
===INTERMEDIATE=== (technical terms, for experienced investor)
===PRO=== (professional analysis, full technical)

Use only this format, nothing else."""

        response = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "llama3-8b-8192",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 600
            },
            timeout=15
        )
        return response.json()["choices"][0]["message"]["content"]
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
    """Spam kontrolü — Supabase'den oku"""
    try:
        r = supabase.table("us_signals") \
            .select("created_at") \
            .eq("symbol", symbol) \
            .order("created_at", ascending=False) \
            .limit(1) \
            .execute()
        if r.data:
            last = r.data[0]["created_at"]
            dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
            return dt.timestamp()
        return 0
    except:
        return 0

class VolumeTracker:
    def __init__(self):
        self.trades = {}  # symbol -> [(price, volume, timestamp)]
        self.daily_opens = {}  # symbol -> open_price

    def update(self, symbol, price, volume):
        now = time.time()
        if symbol not in self.trades:
            self.trades[symbol] = []
            self.daily_opens[symbol] = price

        self.trades[symbol].append((price, volume, now))

        # Son 5 dakikayı tut
        cutoff = now - 300
        self.trades[symbol] = [(p, v, t) for p, v, t in self.trades[symbol] if t > cutoff]

    def get_volume_ratio(self, symbol, current_volume, avg_volume):
        if avg_volume <= 0:
            return 0
        # Son 5 dakika toplam hacim
        recent_volume = sum(v for _, v, _ in self.trades.get(symbol, []))
        # Günlük ortalamayı 5 dakikaya böl (6.5 saat = 78 adet 5 dakika)
        expected_5min = avg_volume / 78
        if expected_5min <= 0:
            return 0
        return recent_volume / expected_5min

    def get_price_change(self, symbol, current_price):
        open_price = self.daily_opens.get(symbol, current_price)
        if not open_price:
            return 0
        return ((current_price - open_price) / open_price) * 100

tracker = VolumeTracker()
active_symbols = []
avg_volumes = {}

def process_signal(symbol, signal_type, price, price_change, volume_ratio):
    # Borsa kapalıysa işleme
    if not is_market_open():
        return

    # Fiyat filtresi — min $5
    if price < 5:
        return

    # Spam kontrolü
    last_time = get_last_signal_time(symbol)
    if time.time() - last_time < 3600:  # 1 saat
        return

    print(f"🔍 {symbol} araştırılıyor...")

    news = get_news(symbol)
    insider = get_insider(symbol)
    ai_text = get_ai_explanation(symbol, price, price_change, volume_ratio, news, insider)
    acemi, usta, pro = parse_ai_levels(ai_text)

    description = f"🚀 {symbol} | ${price:.2f} | {price_change:+.1f}% | Vol: {volume_ratio:.1f}x"
    if news:
        description += f" | 📰 {news[:100]}"
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
        print(f"✅ KAYDEDİLDİ: {description}")
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

            if not symbol or not price or price < 5:
                continue

            tracker.update(symbol, price, volume)

            avg_vol = avg_volumes.get(symbol, 0)
            if avg_vol == 0:
                continue

            volume_ratio = tracker.get_volume_ratio(symbol, volume, avg_vol)
            price_change = tracker.get_price_change(symbol, price)

            # Momentum: hacim 5x + fiyat %5
            if volume_ratio >= 5 and abs(price_change) >= 5:
                process_signal(symbol, "momentum", price, price_change, volume_ratio)

            # Volume spike: hacim 10x
            elif volume_ratio >= 10:
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

def start():
    global active_symbols, avg_volumes

    print("🔍 Hisse listesi ve ortalama hacimler yükleniyor...")
    active_symbols = QUALITY_SYMBOLS

    # Her hisse için ortalama hacim çek
    for symbol in active_symbols:
        avg_vol = get_avg_volume(symbol)
        avg_volumes[symbol] = avg_vol
        print(f"  {symbol}: avg_vol={avg_vol:,.0f}")
        time.sleep(0.2)

    print(f"✅ {len(active_symbols)} hisse hazır")
    print(f"📡 WebSocket bağlanıyor...")

    ws = websocket.WebSocketApp(
        f"wss://ws.finnhub.io?token={FINNHUB_KEY}",
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )
    ws.run_forever()

if __name__ == "__main__":
    print("🚀 Atlas US Sinyal Motoru başlatıldı...")
    start()
