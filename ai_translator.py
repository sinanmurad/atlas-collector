import requests, os, json, time
from datetime import datetime

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

headers = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json"
}

def translate_all_levels(raw_data):
    # raw_data string ise parse et
    if isinstance(raw_data, str):
        try:
            raw_data = json.loads(raw_data)
        except:
            pass

    # Sadece anlamlı alanları al
    company = ""
    title = ""
    subject = ""
    summary = ""

    if isinstance(raw_data, dict):
        company = raw_data.get("kapTitle", "") or raw_data.get("companyName", "") or ""
        title = raw_data.get("title", "") or ""
        subject = raw_data.get("subject", "") or ""
        summary = raw_data.get("summary", "") or raw_data.get("description", "") or ""

    if not any([company, title, subject, summary]):
        return None, None, None

    prompt = f"""Bu finansal bildirimi 3 FARKLI seviyede Türkçe açıkla.

ŞİRKET: {company}
BAŞLIK: {title}
KONU: {subject}
ÖZET: {summary}

CEVAP FORMATI (sadece bu formatta çıktı ver, başka hiçbir şey yazma):

===ACEMI===
[Hiç finans bilmeyen birine anlatır gibi. 2-3 basit cümle. Teknik terim yok.]
📚 Bugün öğrendiklerin:
- [Bu bildirimde geçen bir terimin basit açıklaması]
- [Bir terim daha]

===USTA===
[Biraz finans bilen için. Terimleri açıkla. 3-4 cümle.]
📚 Bugün öğrendiklerin:
- [Terimin detaylı açıklaması]
- [Bir terim daha]

===PRO===
[Profesyonel yatırımcı için. Teknik terimlerle. 4-5 cümle.]
📚 Bugün öğrendiklerin:
- [Teknik terimin profesyonel tanımı]
- [Bir terim daha]

KURALLAR:
- ASLA "al", "sat", "tut" gibi tavsiye verme
- ASLA "yükselebilir", "düşebilir" gibi tahmin yapma
- Sadece bildirimi açıkla
- JSON veya ham veri gösterme"""

    resp = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
        json={
            "model": "llama-3.3-70b-versatile",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1
        },
        timeout=60
    )

    if resp.status_code != 200:
        print(f"   API hatası: {resp.status_code}")
        return None, None, None

    content = resp.json()["choices"][0]["message"]["content"]

    acemi = content.split("===ACEMI===")[1].split("===USTA===")[0].strip() if "===ACEMI===" in content else ""
    usta = content.split("===USTA===")[1].split("===PRO===")[0].strip() if "===USTA===" in content else ""
    pro = content.split("===PRO===")[1].strip() if "===PRO===" in content else ""

    return acemi, usta, pro


print("📡 Çevirisi yapılmamış bildirimler aranıyor...")
r = requests.get(
    f"{SUPABASE_URL}/rest/v1/disclosures?select=disclosure_index,raw_data&order=disclosure_index.desc&limit=20",
    headers=headers
)
disclosures = r.json()
print(f"✅ {len(disclosures)} bildirim kontrol ediliyor.")

for item in disclosures:
    did = item["disclosure_index"]

    check = requests.get(
        f"{SUPABASE_URL}/rest/v1/ai_explanations?disclosure_index=eq.{did}&limit=1",
        headers=headers
    )
    if check.status_code == 200 and check.json():
        print(f"⏭️ {did} zaten çevrili")
        continue

    print(f"🔄 {did} çevriliyor...")
    acemi, usta, pro = translate_all_levels(item.get("raw_data", {}))

    if acemi:
        for level, explanation in [("acemi", acemi), ("usta", usta), ("pro", pro)]:
            if explanation:
                data = {
                    "disclosure_index": did,
                    "level": level,
                    "explanation": explanation,
                    "model_name": "llama-3.3-70b"
                }
                save_resp = requests.post(
                    f"{SUPABASE_URL}/rest/v1/ai_explanations",
                    headers=headers,
                    json=data
                )
                if save_resp.status_code in [200, 201]:
                    print(f"   ✅ {level} kaydedildi")
                else:
                    print(f"   ❌ {level} kayıt hatası: {save_resp.status_code}")
        print(f"   ✅ {did} TAMAMLANDI")
    else:
        print(f"   ❌ {did} — anlamlı veri yok, atlandı")

    time.sleep(0.5)

print("🎉 AI çeviriler tamamlandı!")
