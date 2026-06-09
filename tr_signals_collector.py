import os
import time
import requests
from datetime import datetime, timezone, date
from supabase import create_client

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
GROQ_KEY = os.environ.get("GROQ_API_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
HEADERS = {'User-Agent': 'Mozilla/5.0'}

last_signal_time = {}

# BIST 30 — dev firmalar, bunlara bakma
EXCLUDED = [
    "THYAO", "AKBNK", "GARAN", "EREGL", "SISE", "KCHOL",
    "TUPRS", "BIMAS", "ASELS", "FROTO", "TOASO", "PGSUS",
    "SAHOL", "YKBNK", "HALKB", "VAKBN", "TTKOM", "TCELL",
    "ARCLK", "PETKM", "KOZAL", "KRDMD", "MGROS", "TAVHL",
    "EKGYO", "ENKAI", "ISCTR", "LOGO", "ODAS", "SOKM"
]

def get_bist_symbols():
    try:
        response = supabase.table("stock_prices").select("symbol").execute()
        symbols = [r["symbol"] for r in response.data if r["symbol"] not in EXCLUDED]
        print(f"✅ {len(symbols)} BIST hissesi yüklendi (dev firmalar elendi)")
        return symbols
    except Exception as e:
        print(f"⚠️ Hata: {e}")
        return []

def get_price_data(symbol):
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}.IS"
        r = requests.get(url, headers=HEADERS, timeout=8)
        result = r.json()["chart"]["result"][0]["meta"]
        return {
            "price": result.get("regularMarketPrice", 0),
            "prev_close": result.get("previousClose", 0),
        }
    except:
        return None

def get_kap_news_today(symbol):
    """Bugün KAP'ta bildirim var mı?"""
    try:
        today = date.today().isoformat()
        response = supabase.table("disclosures") \
            .select("title, publish_date") \
            .ilike("stock_codes", f"%{symbol}%") \
            .gte("publish_date", today) \
            .order("disclosure_index", ascending=False) \
            .limit(1) \
            .execute()
        if response.data:
            return response.data[0].get("title", "")
        return ""
    except:
        return ""

def get_ai_explanation(symbol, price, price_change, kap_news):
    try:
        prompt = f"""
Sen bir Türk finans asistanısın. Aşağıdaki veriye göre 3 seviyede Türkçe açıkla:

Hisse: {symbol} (Borsa İstanbul)
Fiyat: {price:.2f} TL
Fiyat değişimi: %{price_change:.1f}
Bugünkü KAP bildirimi: {kap_news if kap_news else 'Yok'}

===ACEMİ=== (1-2 cümle, hiç finans bilmeyene sade Türkçe)
===USTA=== (teknik terimlerle, orta düzey yatırımcıya)
===PRO=== (profesyonel analiz diliyle, tam teknik)

Sadece bu formatı kullan, başka bir şey yazma.
"""
        response = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "llama3-8b-8192",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 500
            },
            timeout=10
        )
        return response.json()["choices"][0]["message"]["content"]
    except:
        return ""

def parse_ai_levels(ai_text):
    acemi, usta, pro = "", "", ""
    try:
        if "===ACEMİ===" in ai_text:
            acemi = ai_text.split("===ACEMİ===")[1].split("===USTA===")[0].strip()
        if "===USTA===" in ai_text:
            usta = ai_text.split("===USTA===")[1].split("===PRO===")[0].strip()
        if "===PRO===" in ai_text:
            pro = ai_text.split("===PRO===")[1].strip()
    except:
        pass
    return acemi, usta, pro

def scan_once(symbols):
    signals_found = 0
    for symbol in symbols:
        try:
            data = get_price_data(symbol)
            if not data:
                time.sleep(0.1)
                continue

            price = data["price"]
            prev_close = data["prev_close"]

            if not price or not prev_close or prev_close == 0:
                time.sleep(0.1)
                continue

            price_change = ((price - prev_close) / prev_close) * 100

            # Bugün KAP bildirimi var mı?
            kap_news = get_kap_news_today(symbol)

            # Sinyal kriterleri:
            # 1. Güçlü hareket (KAP olsun olmasın)
            # 2. Orta hareket + KAP bildirimi (anlamlı sinyal)
            is_strong = abs(price_change) >= 7
            is_with_kap = abs(price_change) >= 3 and kap_news != ""

            if not is_strong and not is_with_kap:
                time.sleep(0.1)
                continue

            # Spam kontrolü
            now = time.time()
            if symbol in last_signal_time:
                if now - last_signal_time[symbol] < 1800:
                    continue

            signal_type = "momentum" if is_strong else "kap_momentum"
            print(f"🎯 {symbol} | %{price_change:.1f} | {'KAP var' if kap_news else 'KAP yok'} | {signal_type}")

            ai_text = get_ai_explanation(symbol, price, price_change, kap_news)
            acemi, usta, pro = parse_ai_levels(ai_text)

            description = f"{'🚀' if is_strong else '📰'} {symbol} | {price:.2f} TL | %{price_change:.1f}"
            if kap_news:
                description += f" | KAP: {kap_news[:80]}"

            signal = {
                "symbol": symbol,
                "signal_type": signal_type,
                "value": round(price_change, 2),
                "description": description,
                "acemi_explanation": acemi,
                "usta_explanation": usta,
                "pro_explanation": pro,
                "price": price,
                "volume_ratio": 0,
                "market": "BIST",
                "created_at": datetime.now(timezone.utc).isoformat()
            }

            supabase.table("tr_signals").insert(signal).execute()
            last_signal_time[symbol] = now
            print(f"✅ KAYDEDİLDİ: {description}")
            signals_found += 1
            time.sleep(0.5)

        except Exception as e:
            print(f"❌ {symbol}: {e}")
            continue

    return signals_found

def main():
    print("🚀 Atlas TR Gerçek Zamanlı Sinyal Motoru başlatıldı...")
    symbols = get_bist_symbols()

    while True:
        now = datetime.now()
        hour = now.hour

        # BIST: 10:00-18:00 Türkiye = 07:00-15:00 UTC
        if 7 <= hour < 15:
            print(f"📡 Tarama başlıyor... {now.strftime('%H:%M:%S')} UTC")
            found = scan_once(symbols)
            print(f"✅ Tarama bitti. {found} sinyal. 2 dk bekleniyor...")
            time.sleep(120)
        else:
            print(f"💤 Borsa kapalı. Bekleniyor... {now.strftime('%H:%M:%S')} UTC")
            time.sleep(300)

if __name__ == "__main__":
    main()
