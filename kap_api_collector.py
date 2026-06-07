import requests
from datetime import datetime

SUPABASE_URL = "https://ogiooilwfeowymgdphuk.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im9naW9vaWx3ZmVvd3ltZ2RwaHVrIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODA2NjczNDgsImV4cCI6MjA5NjI0MzM0OH0.cSo83jEk6JdEfxnPmf7HwGbRr--tEu2WFH7H1n6Aanc"

def get_last_index_from_db():
    """Supabase'deki son bildirim index'ini al"""
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    r = requests.get(f"{SUPABASE_URL}/rest/v1/disclosures?select=disclosure_index&order=disclosure_index.desc&limit=1", headers=headers)
    if r.status_code == 200 and r.json():
        return r.json()[0]["disclosure_index"]
    return 0

def save_disclosure(item):
    """Yeni bildirimi kaydet"""
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json"}
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
    try:
        r = requests.post(f"{SUPABASE_URL}/rest/v1/disclosures", headers=headers, json=data)
        return r.status_code in [200, 201, 204]
    except:
        return False

# 1. Son index'i bul
last_index = get_last_index_from_db()
print(f"📌 Veritabanındaki son bildirim: {last_index}")

# 2. KAP'ten son index'i al
url = "https://www.kap.org.tr/tr/api/disclosure/members/byCriteria"
payload = {
    "fromDate": "2026-06-01",
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
if not isinstance(data, list):
    print(f"❌ Beklenmeyen veri formatı")
    exit()

# 3. Yeni bildirimleri bul (index'i büyük olanlar)
yeni_bildirimler = [item for item in data if item.get("disclosureIndex", 0) > last_index]
print(f"✅ Toplam {len(data)} bildirim, {len(yeni_bildirimler)} yeni bildirim bulundu.")

# 4. Sadece yenileri kaydet
kaydedilen = 0
for i, item in enumerate(yeni_bildirimler):
    print(f"   [{i+1}] {item.get('disclosureIndex')} - {(item.get('kapTitle') or '')[:40]}...")
    if save_disclosure(item):
        kaydedilen += 1
        print(f"       ✅ KAYDEDİLDİ")
    else:
        print(f"       ❌ HATA")

print(f"\n🎉 {kaydedilen}/{len(yeni_bildirimler)} yeni bildirim kaydedildi!")