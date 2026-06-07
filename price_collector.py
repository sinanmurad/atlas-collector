import requests
import os
from datetime import datetime

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

bist_symbols = ['THYAO', 'KUVVA']

def get_price(symbol):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}.IS"
    print(f"   URL: {url}")
    try:
        response = requests.get(url, timeout=10)
        print(f"   Status: {response.status_code}")
        if response.status_code != 200:
            print(f"   Hata: status {response.status_code}")
            return None
        data = response.json()
        print(f"   JSON alındı, keys: {data.keys() if data else 'yok'}")
        if 'chart' not in data or 'result' not in data['chart'] or not data['chart']['result']:
            print(f"   Veri yapısı hatalı: {data}")
            return None
        result = data['chart']['result'][0]
        meta = result['meta']
        print(f"   Meta: {meta.keys()}")
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
    except Exception as e:
        print(f"   Exception: {e}")
        return None

def save_price(data):
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json"}
    url = f"{SUPABASE_URL}/rest/v1/stock_prices"
    print(f"   Saving to: {url}")
    check = requests.get(f"{url}?symbol=eq.{data['symbol']}", headers=headers)
    print(f"   Check status: {check.status_code}")
    if check.status_code == 200 and check.json():
        r = requests.patch(f"{url}?symbol=eq.{data['symbol']}", headers=headers, json=data)
        print(f"   PATCH status: {r.status_code}")
    else:
        r = requests.post(url, headers=headers, json=data)
        print(f"   POST status: {r.status_code}")
        if r.status_code not in [200, 201, 204]:
            print(f"   Hata mesajı: {r.text[:200]}")
    return r.status_code in [200, 201, 204]

print(f"📡 {len(bist_symbols)} BIST hissesi çekiliyor...")
print(f"Supabase URL: {SUPABASE_URL[:50]}...")

for symbol in bist_symbols:
    print(f"\n🔍 {symbol}...")
    price_data = get_price(symbol)
    if price_data:
        print(f"   Fiyat: {price_data['price']}")
        if save_price(price_data):
            print(f"   ✅ {price_data['price']} TL")
        else:
            print(f"   ❌ Kayıt hatası")
    else:
        print(f"   ❌ Veri alınamadı")

print("\n🎉 İşlem tamamlandı!")
