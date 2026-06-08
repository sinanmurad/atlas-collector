import os
import time
import requests
from datetime import datetime, timezone
from supabase import create_client

# Config
FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# Small/Mid cap watchlist - kimsenin bakmadığı hisseler
WATCHLIST = [
    "IONQ", "SOUN", "BBAI", "RKLB", "ASTS", "LUNR", "RDW",
    "ACHR", "JOBY", "QS", "MVST", "NKLA", "BLNK", "CHPT",
    "SMCI", "PLTR", "SOFI", "HOOD", "AFRM", "UPST"
]

def get_quote(symbol):
    url = f"https://finnhub.io/api/v1/quote?symbol={symbol}&token={FINNHUB_KEY}"
    r = requests.get(url)
    return r.json()

def get_insider(symbol):
    url = f"https://finnhub.io/api/v1/stock/insider-transactions?symbol={symbol}&token={FINNHUB_KEY}"
    r = requests.get(url)
    return r.json()

def get_earnings(symbol):
    url = f"https://finnhub.io/api/v1/stock/earnings?symbol={symbol}&token={FINNHUB_KEY}"
    r = requests.get(url)
    return r.json()

def check_volume_spike(symbol, current_volume, avg_volume):
    if avg_volume and avg_volume > 0:
        ratio = current_volume / avg_volume
        if ratio >= 3:
            return f"Hacim patlaması: normalin {ratio:.1f} katı"
    return None

def process_signals():
    now = datetime.now(timezone.utc).isoformat()
    signals = []

    for symbol in WATCHLIST:
        try:
            quote = get_quote(symbol)
            
            current_price = quote.get("c", 0)
            prev_close = quote.get("pc", 0)
            
            if not current_price:
                continue

            change_pct = ((current_price - prev_close) / prev_close * 100) if prev_close else 0

            # Sinyal 1: Büyük fiyat hareketi
            if abs(change_pct) >= 5:
                signals.append({
                    "symbol": symbol,
                    "signal_type": "price_move",
                    "value": round(change_pct, 2),
                    "description": f"%{change_pct:.1f} fiyat hareketi",
                    "created_at": now
                })

            # Sinyal 2: Insider işlem
            insider = get_insider(symbol)
            transactions = insider.get("data", [])
            if transactions:
                latest = transactions[0]
                if latest.get("transactionType") == "P-Purchase":
                    signals.append({
                        "symbol": symbol,
                        "signal_type": "insider_buy",
                        "value": latest.get("share", 0),
                        "description": f"İçeriden alım: {latest.get('name')} {latest.get('share'):,} hisse aldı",
                        "created_at": now
                    })

            # Sinyal 3: Earnings surprise
            earnings = get_earnings(symbol)
            if earnings and len(earnings) > 0:
                latest = earnings[0]
                actual = latest.get("actual")
                estimate = latest.get("estimate")
                if actual and estimate and estimate != 0:
                    surprise = ((actual - estimate) / abs(estimate)) * 100
                    if abs(surprise) >= 10:
                        signals.append({
                            "symbol": symbol,
                            "signal_type": "earnings_surprise",
                            "value": round(surprise, 2),
                            "description": f"Kazanç sürprizi: beklentinin %{surprise:.1f} {'üzerinde' if surprise > 0 else 'altında'}",
                            "created_at": now
                        })

            time.sleep(0.5)  # Rate limit

        except Exception as e:
            print(f"Hata {symbol}: {e}")
            continue

    # Supabase'e kaydet
    if signals:
        supabase.table("us_signals").insert(signals).execute()
        print(f"{len(signals)} sinyal kaydedildi")
    else:
        print("Sinyal yok")

if __name__ == "__main__":
    process_signals()
