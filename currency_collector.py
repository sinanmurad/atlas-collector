import requests
from datetime import datetime
import os

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

def get_currency_id(code):
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    r = requests.get(f"{SUPABASE_URL}/rest/v1/currencies?code=eq.{code}", headers=headers)
    if r.status_code == 200 and r.json():
        return r.json()[0]["id"]
    return None

def save_rate(from_code, to_code, rate):
    from_id = get_currency_id(from_code)
    to_id = get_currency_id(to_code)
    if not from_id or not to_id:
        print(f"❌ {from_code} veya {to_code} bulunamadı")
        return
    
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json"}
    data = {
        "from_currency_id": from_id,
        "to_currency_id": to_id,
        "rate": rate,
        "source": "frankfurter",
        "recorded_at": datetime.now().isoformat()
    }
    r = requests.post(f"{SUPABASE_URL}/rest/v1/exchange_rates", headers=headers, json=data)
    print(f"✅ {from_code}/{to_code}: {rate} (status: {r.status_code})")

print("📡 Döviz kurları çekiliyor...")

pairs = [("USD", "TRY"), ("EUR", "TRY"), ("GBP", "TRY"), ("USD", "EUR")]

for from_curr, to_curr in pairs:
    url = f"https://api.frankfurter.app/latest?from={from_curr}&to={to_curr}"
    try:
        response = requests.get(url)
        data = response.json()
        rate = data["rates"][to_curr]
        save_rate(from_curr, to_curr, rate)
    except Exception as e:
        print(f"❌ {from_curr}/{to_curr} hatası: {e}")

print("🎉 İşlem tamamlandı!")