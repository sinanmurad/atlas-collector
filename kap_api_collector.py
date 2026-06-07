import requests
from datetime import datetime

SUPABASE_URL = "https://ogiooilwfeowymgdphuk.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im9naW9vaWx3ZmVvd3ltZ2RwaHVrIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODA2NjczNDgsImV4cCI6MjA5NjI0MzM0OH0.cSo83jEk6JdEfxnPmf7HwGbRr--tEu2WFH7H1n6Aanc"

def save_disclosure(item):
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json"
    }

    data = {
        "disclosure_index": item.get("disclosureIndex"),
        "publish_date": item.get("publishDate"),
        "title": (item.get("kapTitle") or "")[:255],
        "summary": (item.get("summary") or "")[:500],
        "subject": (item.get("subject") or "")[:255],
        "disclosure_class": item.get("disclosureClass"),
        "disclosure_type": item.get("disclosureType"),
        "stock_codes": item.get("stockCodes") or "",
        "attachment_count": item.get("attachmentCount", 0),
        "raw_data": item,
        "collected_at": datetime.now().isoformat()
    }

    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/disclosures",
        headers=headers,
        json=data
    )
    print(f"   Supabase cevabı: {r.status_code} - {r.text[:200]}")
    return r.status_code in [200, 201, 204]

url = "https://www.kap.org.tr/tr/api/disclosure/members/byCriteria"
payload = {
    "fromDate": "2026-06-06",
    "toDate": "2026-06-07",
    "memberType": "IGS",
    "mkkMemberOidList": [],
    "inactiveMkkMemberOidList": [],
    "disclosureClass": "",
    "subjectList": [],
    "isLate": "",
    "mainSector": "",
    "sector": "",
    "subSector": "",
    "marketOid": "",
    "index": "",
    "bdkReview": "",
    "bdkMemberOidList": [],
    "year": "",
    "term": "",
    "ruleType": "",
    "period": "",
    "fromSrc": False,
    "srcCategory": "",
    "disclosureIndexList": []
}

print("📡 KAP API'den veri çekiliyor...")
response = requests.post(url, json=payload)

if response.status_code != 200:
    print(f"❌ API Hatası: {response.status_code}")
    exit()

data = response.json()
if isinstance(data, list):
    print(f"✅ {len(data)} bildirim bulundu.")
    for i, item in enumerate(data[:3]):  # İlk 3 bildirimi test et
        print(f"   [{i+1}] {item.get('disclosureIndex')}")
        save_disclosure(item)
else:
    print(f"❌ Beklenmeyen veri formatı")