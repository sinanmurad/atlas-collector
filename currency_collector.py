import requests
from datetime import datetime
import os

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

CURRENCY_IDS = {
    "TRY": "15b5ffc5-7a00-45c2-9f67-db19b15c41ed",
    "USD": "d5fb9bb6-cc98-4e04-8783-db5b9b595a4b",
    "EUR": "edfd835c-2217-492f-98ae-2e545bd06f82",
    "GBP": "4d39b3b4-16d8-4ab8-92e5-4e5031fb7e37",
}

def save_rate(from_code, to_code, rate):
    from_id = CURRENCY_IDS.get(from_code)
    to_id = CURRENCY_IDS.get(to_code)
    if not from_id or not to_id:
        print(f"   ID bulunamadı: {from_code}->{to_code}")
        return False
    
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json"}
    data = {
        "from_currency_id": from_id,
        "to_currency_id": to_id,
        "rate": rate,
        "source": "frankfurter",
        "recorded_at": datetime.now().isoformat()
    }
    
    try:
        r = requests.post(f"{SUPABASE_URL}/rest/v1/exchange_rates", headers=headers, json=data)
        print(f"   status: {r.status_code}, response: {r.text[:100]}")
        return r.status_code in [200, 201, 204]
    except Exception as e:
        print(f"   hata: {e}")
        return False

print("📡 Döviz kurları çekiliyor...")
print(f"Supabase URL: {SUPABASE_URL}")

pairs = [("USD", "TRY"), ("EUR", "TRY"), ("GBP", "TRY"), ("USD", "EUR")]

for from_curr, to_curr in pairs:
    url = f"https://api.frankfurter.app/latest?from={from_curr}&to={to_curr}"
    print(f"\n{from_curr}/{to_curr}...")
    try:
        response = requests.get(url)
        data = response.json()
        print(f"   API yanıtı: {data}")
        rate = data["rates"][to_curr]
        print(f"   Kur: {rate}")
        if save_rate(from_curr, to_curr, rate):
            print(f"   ✅ {from_curr}/{to_curr}: {rate} kaydedildi")
        else:
            print(f"   ❌ {from_curr}/{to_curr}: kayıt hatası")
    except Exception as e:
        print(f"   ❌ {from_curr}/{to_curr}: {e}")

print("\n🎉 İşlem tamamlandı!")
