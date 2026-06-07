import requests, os, json
from datetime import datetime

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

def translate_disclosure(raw_data, level):
    prompt = f"""Bu finansal bildirimi {level} seviyesinde açıkla. Sadece veriyi anlat, yorum yapma, tahmin etme:

{json.dumps(raw_data, ensure_ascii=False)[:3000]}

{level} seviyesi kuralları:
- acemi: çok basit, herkes anlar
- usta: detaylı, terimleri açıkla  
- pro: teknik terimlerle, ham veriyi koru"""

    r = requests.post("https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
        json={"model": "llama-3.3-70b-versatile", "messages": [{"role": "user", "content": prompt}], "temperature": 0.1})
    return r.json()["choices"][0]["message"]["content"]

# Supabase'den çevrilmemiş bildirimleri al
headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
r = requests.get(f"{SUPABASE_URL}/rest/v1/disclosures?select=disclosure_index,raw_data&ai_explanation=is.null&limit=10", headers=headers)

for item in r.json():
    did = item["disclosure_index"]
    raw = item["raw_data"]
    
    for level in ["acemi", "usta", "pro"]:
        explanation = translate_disclosure(raw, level)
        
        # Kaydet
        data = {"disclosure_index": did, "level": level, "explanation": explanation, "created_at": datetime.now().isoformat()}
        requests.post(f"{SUPABASE_URL}/rest/v1/ai_explanations", headers=headers, json=data)
        print(f"✅ {did} - {level}")

print("🎉 AI çeviriler tamamlandı!")