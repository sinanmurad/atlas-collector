import requests, os, json
from datetime import datetime

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json"}

# Daha önce çevrilmemiş bildirimleri bulmak için:
# ai_explanations tablosunda kaydı olmayan disclosure_index'leri al
r = requests.get(f"{SUPABASE_URL}/rest/v1/disclosures?select=disclosure_index,raw_data", headers=headers)
all_disclosures = r.json()

# Çevrilenleri bul
r2 = requests.get(f"{SUPABASE_URL}/rest/v1/ai_explanations?select=disclosure_index", headers=headers)
translated_ids = {item["disclosure_index"] for item in r2.json()}

# Çevrilmemişleri filtrele
to_translate = [d for d in all_disclosures if d["disclosure_index"] not in translated_ids]
print(f"📡 {len(to_translate)} bildirim çevrilecek.")

for item in to_translate[:10]:  # İlk 10'u test et
    did = item["disclosure_index"]
    raw = item["raw_data"]
    
    for level in ["acemi", "usta", "pro"]:
        prompt = f"""Bu finansal bildirimi {level} seviyesinde açıkla. Sadece veriyi anlat, yorum yapma:

{json.dumps(raw, ensure_ascii=False)[:2000]}"""

        resp = requests.post("https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            json={"model": "llama-3.3-70b-versatile", "messages": [{"role": "user", "content": prompt}], "temperature": 0.1})
        
        if resp.status_code == 200:
            explanation = resp.json()["choices"][0]["message"]["content"]
            data = {"disclosure_index": did, "level": level, "explanation": explanation, "model_name": "llama-3.3-70b"}
            requests.post(f"{SUPABASE_URL}/rest/v1/ai_explanations", headers=headers, json=data)
            print(f"✅ {did} - {level}")
        else:
            print(f"❌ {did} - {level} API hatası: {resp.status_code}")

print("🎉 AI çeviriler tamamlandı!")
