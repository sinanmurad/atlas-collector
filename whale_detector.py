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

# Small cap + squeeze potansiyeli — $0.05-$50 bandı
QUALITY_SYMBOLS = [
    # Meme / squeeze candidates
    "GME", "AMC", "BB", "BBAI", "SOUN", "RCAT",
    # Kripto volatil
    "COIN", "HOOD", "MSTR", "RIOT", "MARA", "HUT", "CLSK",
    # EV / space / speculative
    "RIVN", "LCID", "NIO", "XPEV", "LI", "JOBY", "ACHR",
    "ASTS", "LUNR", "RKLB",
    # Quantum / AI speculative
    "IONQ", "RGTI", "QUBT", "ARQQ",
    # Fintech small cap
    "AFRM", "SOFI", "UPST", "LC", "OPEN", "OPFI", "DAVE",
    # Growth orta cap
    "PLTR", "SNOW", "DDOG", "NET", "MDB",
    "CVNA", "UBER", "LYFT", "ABNB", "DASH",
    # Volatil tech
    "NVDA", "TSLA", "AMD", "SMCI", "ARM",
]

signal_cache = {}  # RAM'de spam kontrolü


def is_market_open():
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5:
        return False
    open_t = now.replace(hour=13, minute=30, second=0, microsecond=0)
    close_t = now.replace(hour=20, minute=0, second=0, microsecond=0)
    return open_t <= now <= close_t


def get_avg_volume(symbol):
    try:
        r = requests.get(
            f"https://finnhub.io/api/v1/stock/metric?symbol={symbol}&metric=all&token={FINNHUB_KEY}",
            timeout=5
        )
        avg_vol = r.json().get("metric", {}).get("10DayAverageTradingVolume", 0)
        return (avg_vol or 0) * 1_000_000
    except:
        return 0


def get_short_interest(symbol):
    """Short squeeze potansiyeli"""
    try:
        r = requests.get(
            f"https://finnhub.io/api/v1/stock/metric?symbol={symbol}&metric=all&token={FINNHUB_KEY}",
            timeout=5
        )
        metric = r.json().get("metric", {})
        short_ratio = metric.get("shortRatio", 0) or 0
        short_pct = metric.get("shortPercentOutstanding", 0) or 0
        return {
            "short_ratio": short_ratio,
            "short_pct": short_pct,
            "squeeze": short_ratio > 3 and short_pct > 0.10
        }
    except:
        return {"short_ratio": 0, "short_pct": 0, "squeeze": False}


def get_insider(symbol):
    try:
        r = requests.get(
            f"https://finnhub.io/api/v1/stock/insider-transactions?symbol={symbol}&token={FINNHUB_KEY}",
            timeout=5
        )
        data = r.json().get("data", [])
        purchases = [t for t in data[:10] if t.get("transactionCode") == "P-Purchase"]
        if len(purchases) >= 2:
            return f"{len(purchases)} insider alımı — {purchases[0].get('name', '')}"
        if purchases:
            return f"{purchases[0].get('name')} bought {purchases[0].get('share', 0):,} shares"
        return ""
    except:
        return ""


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


def get_conviction(price_change, volume_ratio, short_info, insider):
    """Conviction skoru — HIGH / MEDIUM / NORMAL"""
    score = 0
    if abs(price_change) >= 5:
        score += 2
    elif abs(price_change) >= 3:
        score += 1
    if volume_ratio >= 5:
        score += 2
    elif volume_ratio >= 3:
        score += 1
    if short_info["squeeze"]:
        score += 2
    if insider and "insider" in insider.lower():
        score += 1

    if score >= 5:
        return "HIGH"
    elif score >= 3:
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
[1-2 sentences, plain language, mention conviction level]
===INTERMEDIATE===
[technical analysis with conviction context]
===PRO===
[professional analysis, short squeeze/insider if applicable]"""

        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "llama-3.1-8b-instant",
                "messages": [
                    {
                        "role": "system",
                        "content": "You are a financial analyst. Use only the given format. Never change ===BEGINNER===, ===INTERMEDIATE===, ===PRO=== headers."
                    },
                    {"role": "user", "content": prompt}
                ],
                "max_tokens": 500,
                "temperature": 0.3
            },
            timeout=15
        )
        resp = r.json()
        if "choices" not in resp:
            print(f"⚠️ Groq: {resp.get('error', {}).get('message', str(resp))}")
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
active_symbols = []
avg_volumes = {}
short_data = {}  # Her hisse için short interest cache


def process_signal(symbol, signal_type, price, price_change, volume_ratio):
    if not is_market_open():
        return

    if price < 0.05:
        return

    # RAM cache kontrolü
    now = time.time()
    if symbol in signal_cache and now - signal_cache[symbol] < 3600:
        return

    # Supabase kontrolü — restart güvencesi
    last_time = get_last_signal_time(symbol)
    if now - last_time < 3600:
        signal_cache[symbol] = last_time
        return

    print(f"🔍 {symbol} araştırılıyor...")

    short_info = short_data.get(symbol, {"short_ratio": 0, "short_pct": 0, "squeeze": False})
    news = get_news(symbol)
    insider = get_insider(symbol)
    conviction = get_conviction(price_change, volume_ratio, short_info, insider)

    print(f"📊 {symbol} | Conviction: {conviction} | Short squeeze: {short_info['squeeze']}")

    ai_text = get_ai_explanation(symbol, price, price_change, volume_ratio, news, insider, conviction)
    acemi, usta, pro = parse_ai_levels(ai_text)

    emoji = "🔥" if conviction == "HIGH" else "⚡" if conviction == "MEDIUM" else "🚀"
    description = f"{emoji} {symbol} | ${price:.2f} | {price_change:+.1f}% | Vol: {volume_ratio:.1f}x | {conviction}"
    if short_info["squeeze"]:
        description += f" | 🩳 Short: {short_info['short_ratio']:.1f}x"
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

            if not symbol or not price or price < 0.05:
                continue

            tracker.update(symbol, price, volume)

            avg_vol = avg_volumes.get(symbol, 0)
            if avg_vol == 0:
                continue

            volume_ratio = tracker.get_volume_ratio(symbol, avg_vol)
            price_change = tracker.get_price_change(symbol, price)

            # Threshold düşürüldü — daha erken yakalar
            if volume_ratio >= 2 and abs(price_change) >= 3:
                process_signal(symbol, "momentum", price, price_change, volume_ratio)
            elif volume_ratio >= 3:
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
    global active_symbols, avg_volumes, short_data

    print("🔍 Hisse listesi ve ortalama hacimler yükleniyor...")
    active_symbols = QUALITY_SYMBOLS

    for symbol in active_symbols:
        avg_vol = get_avg_volume(symbol)
        avg_volumes[symbol] = avg_vol
        if avg_vol > 0:
            print(f"  {symbol}: avg_vol={avg_vol:,.0f}")
        time.sleep(0.2)

    print(f"✅ {len(active_symbols)} hisse hazır")

    # Short interest — başlangıçta bir kez yükle
    print("📊 Short interest verileri yükleniyor...")
    for symbol in active_symbols:
        short_data[symbol] = get_short_interest(symbol)
        if short_data[symbol]["squeeze"]:
            print(f"  🩳 {symbol}: SQUEEZE POTANSIYELI — {short_data[symbol]['short_ratio']:.1f}x")
        time.sleep(0.2)

    # Başlangıçta mevcut sinyalleri cache'e yükle
    print("🔄 Mevcut sinyaller cache'e yükleniyor...")
    for symbol in active_symbols:
        last_time = get_last_signal_time(symbol)
        if last_time > 0:
            signal_cache[symbol] = last_time
    print(f"✅ {len(signal_cache)} sembol cache'e yüklendi")

    print("📡 WebSocket bağlanıyor...")
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
