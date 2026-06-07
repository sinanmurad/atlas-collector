import requests, os, json, time
from datetime import datetime

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json"}

def translate_all_levels(raw_data):
    prompt = f"""
Bu finansal bildirimi 3 FARKLI seviyede açıkla. Her seviyenin sonuna "📚 Bugün öğrendiklerin" bölümü ekle.

VERI: {json.dumps(raw_data, ensure_ascii=False)[:2000]}

CEVAP FORMATI (sadece bu formatta çıktı ver):
===ACEMI===
[Hiç finans bilmeyen birine anlatır gibi. Çok basit cümleler. Teknik terim yok.]

📚 Bugün öğrendiklerin:
- [Bu bildirimde geçen terimlerin basit açıklaması]
- [Örnek: "Bağlı ortaklık = Şirketin kontrol ettiği başka bir şirket"]

===USTA===
[Biraz bilgili için. Terimleri açıkla. Neden olduğunu anlat.]

📚 Bugün öğrendiklerin:
- [Terimlerin detaylı açıklaması]
- [Örnek: "Devralma = Bir şirketin diğerini satın alması, kontrol artar"]

===PRO===
[Teknik terimlerle. Ham veriyi koru. Bunun ne anlama geldiğini anlat.]

📚 Bugün öğrendiklerin:
- [Teknik terimlerin profesyonel tanımı]
- [Örnek: "Konsolidasyon = Finansal tabloların birleştirilmesi"]

KURALLAR:
- ASLA yatırım tavsiyesi verme ("al", "sat", "tut" yasak)
- ASLA tahmin yapma ("yükselebilir", "düşebilir" yasak)
- Sadece VERİYİ anlat
- Her seviye için "📚 Bugün öğrendiklerin" bölümü ZORUNLU
"""

    resp = requests.post("https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
        json={"model": "llama-3.3-70b-versatile", "messages": [{"role": "user", "content": prompt}], "temperature": 0.1},
        timeout=60)
    
    if resp.status_code != 200:
        print(f"   API hatası: {resp.status_code}")
        return None, None, None
    
    content = resp.json()["choices"][0]["message"]["content"]
    
    acemi = content.split("===ACEMI===")[1].split("===USTA===")[0].strip() if "===ACEMI===" in content else ""
    usta = content.split("===USTA===")[1].split("===PRO===")[0].strip() if "===USTA===" in content else ""
    pro = content.split("===PRO===")[1].strip() if "===PRO===" in content else ""
    
    return acemi, usta, pro

print("📡 Çevirisi yapılmamış bildirimler aranıyor...")
r = requests.get(f"{SUPABASE_URL}/rest/v1/disclosures?select=disclosure_index,raw_data&order=disclosure_index.desc&limit=20", headers=headers)

disclosures = r.json()
print(f"✅ {len(disclosures)} bildirim kontrol ediliyor.")

for item in disclosures:
    did = item["disclosure_index"]
    
    check = requests.get(f"{SUPABASE_URL}/rest/v1/ai_explanations?disclosure_index=eq.{did}&limit=1", headers=headers)
    if check.status_code == 200 and check.json():
        print(f"⏭️ {did} zaten çevrili")
        continue
    
    print(f"🔄 {did} çevriliyor...")
    acemi, usta, pro = translate_all_levels(item.get("raw_data", {}))
    
    if acemi:
        for level, explanation in [("acemi", acemi), ("usta", usta), ("pro", pro)]:
            if explanation:
                data = {"disclosure_index": did, "level": level, "explanation": explanation, "model_name": "llama-3.3-70b"}
                save_resp = requests.post(f"{SUPABASE_URL}/rest/v1/ai_explanations", headers=headers, json=data)
                if save_resp.status_code in [200, 201]:
                    print(f"   ✅ {level} kaydedildi")
                else:
                    print(f"   ❌ {level} kayıt hatası: {save_resp.status_code}")
        print(f"   ✅ {did} TAMAMLANDI")
    else:
        print(f"   ❌ {did} başarısız")
    
    time.sleep(0.5)

print("🎉 AI çeviriler tamamlandı!")
