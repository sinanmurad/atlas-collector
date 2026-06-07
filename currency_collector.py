import requests
from datetime import datetime
import os

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

# Currency ID'leri (manuel)
CURRENCY_IDS = {
    "TRY": "15b5ffc5-7a00-45c2-9f67-db19b15c41ed",
    "USD": "d5fb9bb6-cc98-4e04-8783-db5b9b595a4b",
    "EUR": "edfd835c-2217-492f-98ae-2e545bd06f82",
    "GBP": "4d39b3b4-16d8-4ab8-92e5-4e5031fb7e37",
    "JPY": "125fe17d-0381-4be4-afa8-90f12fd1353b",
}

def save_rate(from_code, to_code, rate):
    from_id = CURRENCY_IDS.get(from_code)
    to_id = CURRENCY_IDS.get(to_code)
    if not from_id or not to_id:
        print(f"❌ {from_code} veya {to_code} ID'si bulunamadı")
        return False
    
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json"}
    data = {
        "from_currency_id": from_id,
        "to_currency_id": to_id,
        "rate": rate,
        "source": "frankfurter",
        "recorded_at": datetime.now().isoformat()
    }
    r = requests.post(f"{SUPABASE_URL}/rest/v1/exchange_rates", headers=headers, json=data)
    return r.status_code in [200, 201, 204]

print("📡 Döviz kurları çekiliyor...")

pairs = [("USD", "TRY"), ("EUR", "TRY"), ("GBP", "TRY"), ("USD", "EUR")]

for from_curr, to_curr in pairs:
    url = f"https://api.frankfurter.app/latest?from={from_curr}&to={to_curr}"
    try:
        response = requests.get(url)
        data = response.json()
        rate = data["rates"][to_curr]
        if save_rate(from_curr, to_curr, rate):
            print(f"✅ {from_curr}/{to_curr}: {rate}")
        else:
            print(f"❌ {from_curr}/{to_curr}: kayıt hatası")
    except Exception as e:
        print(f"❌ {from_curr}/{to_curr}: {e}")

print("🎉 İşlem tamamlandı!")
