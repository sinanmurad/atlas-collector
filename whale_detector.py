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

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# API koparsa devreye giren fallback - $0.001-$20 arası small cap
FALLBACK = [
    "SOUN", "BBAI", "RCAT", "ONDS", "POET", "RZLV",
    "LUNR", "RDW", "ASTS", "ACHR", "JOBY",
    "IONQ", "RGTI", "QUBT", "ARQQ",
    "LCID", "NKLA", "BLNK", "CHPT", "QS", "MVST",
    "SOFI", "HOOD", "AFRM", "UPST", "DAVE",
    "SIGA", "KALA", "NKTR", "OCGN", "NVAX",
    "FFIE", "MULN", "MVIS", "ILUS", "IDEX",
    "AEHR", "CAMT", "ONTO", "FORM"
]

def get_active_symbols():
    """
    Finnhub'dan tüm ABD hisselerini çek.
    $0.001 - $20 arası olanları filtrele (dev firmalar elensin).
    API koparsa FALLBACK devreye girer.
    """
    try:
        url = f"https://finnhub.io/api/v1/stock/symbol?exchange=US&token={FINNHUB_KEY}"
        r = requests.get(url, timeout=10)
        all_symbols = [s["symbol"] for s in r.json() if "." not in s["symbol"]]

        filtered = []
        for symbol in all_symbols[:500]:  # İlk 500'ü tara
            try:
                quote = requests.get(
                    f"https://finnhub.io/api/v1/quote?symbol={symbol}&token={FINNHUB_KEY}",
                    timeout=5
                ).json()
                price = quote.get("c", 0)

                # $0.001 - $20 arası → dev firmalar ($20 üzeri) elensin
                if 0.001 <= price <= 20:
                    filtered.append(symbol)

                if len(filtered) >= 100:  # Max 100 hisse izle
                    break

                time.sleep(0.1)  # Rate limit
            except:
                continue

        print(f"✅ {len(filtered)} hisse filtrelendi (dev firmalar elendi)")
        return filtered if filtered else FALLBACK

    except Exception as e:
        print(f"⚠️ API hatası, fallback devreye girdi: {e}")
        return FALLBACK


class VolumeTracker:
    def __init__(self):
        self.volumes = {}
        self.prices = {}

    def update(self, symbol, price, volume):
        if symbol not in self.volumes:
            self.volumes[symbol] = []
            self.prices[symbol] = price

        self.volumes[symbol].append(volume)
        if len(self.volumes[symbol]) > 20:
            self.volumes[symbol].pop(0)

        self.prices[symbol] = price

    def check_signal(self, symbol, current_volume, current_price):
        volumes = self.volumes.get(symbol, [])
        if len(volumes) < 5:
            return None

        avg = sum(volumes[:-1]) / len(volumes[:-1])
        if avg == 0:
            return None

        ratio = current_volume / avg

        # Fiyat değişimi — başlangıç fiyatına göre
        old_price = self.prices.get(symbol, current_price)
        price_change = ((current_price - old_price) / old_price * 100) if old_price else 0

        # 🚀 Güçlü sinyal: hacim 5x + fiyat %10 hareket
        if ratio >= 5 and abs(price_change) >= 10:
            return {
                "symbol": symbol,
                "signal_type": "momentum",
                "value": round(price_change, 2),
                "description": (
                    f"🚀 {symbol} | Fiyat: ${current_price:.4f} | "
                    f"%{price_change:.1f} hareket | Hacim: {ratio:.1f}x normal"
                ),
                "created_at": datetime.now(timezone.utc).isoformat()
            }

        # 📊 Hacim patlaması: 10x ve üzeri
        if ratio >= 10:
            return {
                "symbol": symbol,
                "signal_type": "volume_spike",
                "value": round(ratio, 2),
                "description": (
                    f"📊 {symbol} | Hacim patlaması: normalin {ratio:.1f}x | "
                    f"Fiyat: ${current_price:.4f}"
                ),
                "created_at": datetime.now(timezone.utc).isoformat()
            }

        return None


tracker = VolumeTracker()
active_symbols = []


def save_signal(signal):
    try:
        supabase.table("us_signals").insert(signal).execute()
        print(f"✅ SİNYAL: {signal['description']}")
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
            signal = tracker.check_signal(symbol, volume, price)

            if signal:
                save_signal(signal)

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
    print("🚀 Atlas Whale Detector başlatıldı...")
    start()
