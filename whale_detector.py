import os
import json
import time
import requests
import websocket
from datetime import datetime, timezone
from supabase import create_client

FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
GROQ_KEY = os.environ.get("GROQ_API_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

FALLBACK = [
    "SOUN", "BBAI", "RCAT", "ONDS", "POET", "RZLV",
    "LUNR", "RDW", "ASTS", "ACHR", "JOBY", "IONQ",
    "RGTI", "QUBT", "ARQQ", "LCID", "NKLA", "BLNK",
    "CHPT", "QS", "MVST", "SOFI", "AFRM", "UPST",
    "DAVE", "SIGA", "OCGN", "NVAX", "MVIS", "IDEX"
]

# Son sinyal zamanı — aynı hisseden spam gelmesin
last_signal_time = {}

def get_active_symbols():
    try:
        url = f"https://finnhub.io/api/v1/stock/symbol?exchange=US&token={FINNHUB_KEY}"
        r = requests.get(url, timeout=10)
        all_symbols = [s["symbol"] for s in r.json() if "." not in s["symbol"]]
        filtered = []
        for symbol in all_symbols[:500]:
            try:
                quote = requests.get(
                    f"https://finnhub.io/api/v1/quote?symbol={symbol}&token={FINNHUB_KEY}",
                    timeout=5
                ).json()
                price = quote.get("c", 0)
                if 0.001 <= price <= 20:
                    filtered.append(symbol)
                if len(filtered) >= 100:
                    break
                time.sleep(0.1)
            except:
                continue
        print(f"✅ {len(filtered)} hisse filtrelendi")
        return filtered if filtered else FALLBACK
    except Exception as e:
        print(f"⚠️ Fallback devreye girdi: {e}")
        return FALLBACK

def get_news(symbol):
    """Finnhub'dan son haberleri çek"""
    try:
        url = f"https://finnhub.io/api/v1/company-news?symbol={symbol}&from=2026-06-01&to=2026-06-09&token={FINNHUB_KEY}"
        r = requests.get(url, timeout=5)
        news = r.json()
        if news:
            return news[0].get("headline", "")
        return ""
    except:
        return ""

def get_insider(symbol):
    """SEC Form 4 — insider alım/satım"""
    try:
        url = f"https://finnhub.io/api/v1/stock/insider-transactions?symbol={symbol}&token={FINNHUB_KEY}"
        r = requests.get(url, timeout=5)
        data = r.json().get("data", [])
        # Son 30 gün içinde alım var mı?
        for t in data[:5]:
            if t.get("transactionCode") == "P-Purchase":
                return f"{t.get('name')} {t.get('share', 0):,} hisse aldı"
        return ""
    except:
        return ""

def get_ai_explanation(symbol, price, price_change, volume_ratio, news, insider):
    """Groq AI ile neden hareketleniyor açıklaması"""
    try:
        context = f"""
Hisse: {symbol}
Fiyat: ${price:.4f}
Fiyat değişimi: %{price_change:.1f}
Hacim: normalin {volume_ratio:.1f} katı
Son haber: {news if news else 'Yok'}
Insider alım: {insider if insider else 'Yok'}
"""
        prompt = f"""
Sen bir finans asistanısın. Aşağıdaki veriye göre 3 seviyede açıkla:

{context}

===ACEMİ=== (1-2 cümle, hiç finans bilmeyene)
===USTA=== (teknik terimlerle)
===PRO=== (profesyonel analiz diliyle)

Sadece bu formatı kullan, başka bir şey yazma.
"""
        response = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "llama3-8b-8192",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 500
            },
            timeout=10
        )
        return response.json()["choices"][0]["message"]["content"]
    except:
        return ""

def parse_ai_levels(ai_text):
    """AI metnini seviyelere böl"""
    acemi, usta, pro = "", "", ""
    try:
        if "===ACEMİ===" in ai_text:
            acemi = ai_text.split("===ACEMİ===")[1].split("===USTA===")[0].strip()
        if "===USTA===" in ai_text:
            usta = ai_text.split("===USTA===")[1].split("===PRO===")[0].strip()
        if "===PRO===" in ai_text:
            pro = ai_text.split("===PRO===")[1].strip()
    except:
        pass
    return acemi, usta, pro

class VolumeTracker:
    def __init__(self):
        self.volumes = {}
        self.open_prices = {}

    def update(self, symbol, price, volume):
        if symbol not in self.volumes:
            self.volumes[symbol] = []
            self.open_prices[symbol] = price
        self.volumes[symbol].append(volume)
        if len(self.volumes[symbol]) > 20:
            self.volumes[symbol].pop(0)

    def check_signal(self, symbol, current_volume, current_price):
        volumes = self.volumes.get(symbol, [])
        if len(volumes) < 5:
            return None

        avg = sum(volumes[:-1]) / len(volumes[:-1])
        if avg == 0:
            return None

        ratio = current_volume / avg
        open_price = self.open_prices.get(symbol, current_price)
        price_change = ((current_price - open_price) / open_price * 100) if open_price else 0

        # Fiyat filtresi — WebSocket'te de kontrol et
        if not (0.001 <= current_price <= 20):
            return None

        # Spam kontrolü — aynı hisseden 30 dk içinde sinyal gelmesin
        now = time.time()
        if symbol in last_signal_time:
            if now - last_signal_time[symbol] < 1800:
                return None

        # Güçlü sinyal: hacim 5x + fiyat %10
        if ratio >= 5 and abs(price_change) >= 10:
            last_signal_time[symbol] = now
            return {
                "symbol": symbol,
                "signal_type": "momentum",
                "price": current_price,
                "price_change": round(price_change, 2),
                "volume_ratio": round(ratio, 2),
            }

        # Sadece hacim patlaması: 10x
        if ratio >= 10:
            last_signal_time[symbol] = now
            return {
                "symbol": symbol,
                "signal_type": "volume_spike",
                "price": current_price,
                "price_change": round(price_change, 2),
                "volume_ratio": round(ratio, 2),
            }

        return None

tracker = VolumeTracker()
active_symbols = []

def process_signal(raw_signal):
    """Sinyal geldi — haber, insider, AI çek ve kaydet"""
    symbol = raw_signal["symbol"]
    price = raw_signal["price"]
    price_change = raw_signal["price_change"]
    volume_ratio = raw_signal["volume_ratio"]

    print(f"🔍 {symbol} araştırılıyor...")

    # Paralel veri çek
    news = get_news(symbol)
    insider = get_insider(symbol)

    # AI açıklama
    ai_text = get_ai_explanation(symbol, price, price_change, volume_ratio, news, insider)
    acemi, usta, pro = parse_ai_levels(ai_text)

    description = f"🚀 {symbol} | ${price:.4f} | %{price_change:.1f} | Hacim: {volume_ratio:.1f}x"
    if news:
        description += f" | 📰 {news[:100]}"
    if insider:
        description += f" | 🐋 {insider}"

    signal = {
        "symbol": symbol,
        "signal_type": raw_signal["signal_type"],
        "value": price_change,
        "description": description,
        "acemi_explanation": acemi,
        "usta_explanation": usta,
        "pro_explanation": pro,
        "price": price,
        "volume_ratio": volume_ratio,
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
            price = trade.get("p", 0)
            volume = trade.get("v", 0)
            if not symbol or not price:
                continue
            tracker.update(symbol, price, volume)
            raw_signal = tracker.check_signal(symbol, volume, price)
            if raw_signal:
                process_signal(raw_signal)
    except Exception as e:
        print(f"Mesaj hatası: {e}")

def on_open(ws):
    global active_symbols
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
    global active_symbols
    print("🔍 Hisse listesi hazırlanıyor...")
    active_symbols = get_active_symbols()
    print(f"📡 {len(active_symbols)} hisse izlemeye alındı")
    ws = websocket.WebSocketApp(
        f"wss://ws.finnhub.io?token={FINNHUB_KEY}",
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )
    ws.run_forever()

if __name__ == "__main__":
    print("🚀 Atlas Sinyal Motoru başlatıldı...")
    start()
