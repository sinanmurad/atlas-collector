# price_collector.py - sadece BIST hisseleri (.IS ekle)
import requests
import os
from datetime import datetime

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

# BIST hisseleri için geçerli semboller (sadece BIST)
bist_symbols = ['THYAO', 'KUVVA', 'BJKAS', 'BRYAT', 'AKBNK', 'GARAN', 'SISE', 'KCHOL']

def get_price(symbol):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}.IS"
    try:
        response = requests.get(url, timeout=10)
        data = response.json()
        if 'chart' not in data or 'result' not in data['chart'] or not data['chart']['result']:
            return None
        result = data['chart']['result'][0]
        meta = result['meta']
        return {
            'symbol': symbol,
            'price': meta['regularMarketPrice'],
            'change': round(meta['regularMarketPrice'] - meta['previousClose'], 2),
            'change_percent': round((meta['regularMarketPrice'] - meta['previousClose']) / meta['previousClose'] * 100, 2),
            'high': meta['regularMarketDayHigh'],
            'low': meta['regularMarketDayLow'],
            'volume': meta['regularMarketVolume'],
            'currency': meta['currency'],
            'updated_at': datetime.now().isoformat()
        }
    except:
        return None

def save_price(data):
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json"}
    url = f"{SUPABASE_URL}/rest/v1/stock_prices"
    check = requests.get(f"{url}?symbol=eq.{data['symbol']}", headers=headers)
    if check.status_code == 200 and check.json():
        r = requests.patch(f"{url}?symbol=eq.{data['symbol']}", headers=headers, json=data)
    else:
        r = requests.post(url, headers=headers, json=data)
    return r.status_code in [200, 201, 204]

print("📡 BIST hisse fiyatları çekiliyor...")
basari = 0
for symbol in bist_symbols:
    print(f"  {symbol}...", end=" ")
    price_data = get_price(symbol)
    if price_data and save_price(price_data):
        print(f"✅ {price_data['price']} TL")
        basari += 1
    else:
        print("❌")

print(f"\n🎉 {basari}/{len(bist_symbols)} hisse güncellendi!")
