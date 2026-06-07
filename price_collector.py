# price_collector.py - Akıllı Yahoo Finance
import requests
import os
import time
from datetime import datetime

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

bist_symbols = ['THYAO', 'KUVVA', 'BJKAS', 'BRYAT', 'AKBNK', 'GARAN', 'SISE', 'KCHOL']

def get_price(symbol):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}.IS"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 429:
            print(f"   Rate limit, 5 saniye bekleniyor...")
            time.sleep(5)
            response = requests.get(url, headers=headers, timeout=10)
        if response.status_code != 200:
            return None
        data = response.json()
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
    r = requests.post(url, headers=headers, json=data)
    return r.status_code in [200, 201, 204]

print(f"📡 {len(bist_symbols)} hisse çekiliyor (Yahoo Finance)...")
success = 0
for i, symbol in enumerate(bist_symbols):
    print(f"  [{i+1}/{len(bist_symbols)}] {symbol}...", end=" ")
    price_data = get_price(symbol)
    if price_data and save_price(price_data):
        print(f"✅ {price_data['price']} TL")
        success += 1
    else:
        print("❌")
    time.sleep(2)  # 2 saniye bekle, rate limit'i aşmak için

print(f"\n🎉 {success}/{len(bist_symbols)} hisse güncellendi!")
