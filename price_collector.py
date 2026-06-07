# price_collector.py - FİNNUB VERSİYONU
import requests
import os
import time

API_KEY = "d8iocdhr01qmfrvi51k0d8iocdhr01qmfrvi51kg"
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

bist_symbols = ['THYAO', 'KUVVA', 'BJKAS', 'BRYAT', 'AKBNK', 'GARAN', 'SISE', 'KCHOL']

def get_price(symbol):
    url = f"https://finnhub.io/api/v1/quote?symbol={symbol}.IS&token={API_KEY}"
    try:
        r = requests.get(url, timeout=10)
        data = r.json()
        if 'c' in data and data['c'] > 0:
            return {
                'symbol': symbol,
                'price': data['c'],
                'change': round(data['d'] or 0, 2),
                'change_percent': round(data['dp'] or 0, 2),
                'high': data['h'],
                'low': data['l'],
                'volume': data['v'],
                'currency': 'TRY',
                'updated_at': datetime.now().isoformat()
            }
    except:
        pass
    return None

def save_price(data):
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json"}
    url = f"{SUPABASE_URL}/rest/v1/stock_prices"
    r = requests.post(url, headers=headers, json=data)
    return r.status_code in [200, 201, 204]

print(f"📡 {len(bist_symbols)} hisse çekiliyor (Finnhub)...")
success = 0
for symbol in bist_symbols:
    price_data = get_price(symbol)
    if price_data and save_price(price_data):
        print(f"✅ {symbol}: {price_data['price']} TL")
        success += 1
    else:
        print(f"❌ {symbol}")
    time.sleep(1)  # 1 saniye bekle, 60 istek/dakika yetecek

print(f"🎉 {success}/{len(bist_symbols)} hisse güncellendi!")
