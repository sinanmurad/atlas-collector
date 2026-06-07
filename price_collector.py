# price_collector.py - TÜM HİSSELER İÇİN
import requests
import os
import time
from datetime import datetime

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

def get_all_symbols():
    """Supabase'den tüm hisse kodlarını al"""
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    url = f"{SUPABASE_URL}/rest/v1/assets?select=symbol&asset_type=eq.STOCK&is_active=eq.true"
    response = requests.get(url, headers=headers)
    return [item['symbol'] for item in response.json()]

def get_price(symbol):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}.IS"
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 429:
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

print("📡 Tüm hisse fiyatları çekiliyor...")
symbols = get_all_symbols()
print(f"✅ {len(symbols)} hisse bulundu.")

success = 0
for i, symbol in enumerate(symbols):
    print(f"  [{i+1}/{len(symbols)}] {symbol}...", end=" ")
    price_data = get_price(symbol)
    if price_data and save_price(price_data):
        print(f"✅ {price_data['price']} TL")
        success += 1
    else:
        print("❌")
    time.sleep(1)  # Rate limit koruması

print(f"\n🎉 {success}/{len(symbols)} hisse güncellendi!")
