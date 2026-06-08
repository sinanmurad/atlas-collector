import os
import time
import requests
from datetime import datetime, timezone
from supabase import create_client

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
GROQ_KEY = os.environ.get("GROQ_API_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

HEADERS = {'User-Agent': 'Mozilla/5.0'}

# BIST hisse listesi — price_collector'dan geliyor zaten
def get_bist_symbols():
    try:
        response = supabase.table("stock_prices").select("symbol").execute()
        symbols = [r["symbol"] for r in response.data]
        print(f"✅ {len(symbols)} BIST hissesi yüklendi")
        return symbols
    except Exception as e:
        print(f"⚠️ Hisse listesi hatası: {e}")
        return ["THYAO", "AKBNK", "GARAN", "ASELS", "KCHOL", "SISE", "EREGL", "BIMAS", "TUPRS", "PGSUS"]

def get_price_data(symbol):
    """Yahoo Finance'den fiyat + hacim çek"""
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}.IS"
        r = requests.get(url, headers=HEADERS, timeout=10)
        data = r.json()["chart"]["result"][0]["meta"]
        return {
            "price": data.get("regularMarketPrice", 0),
            "prev_close": data.get("previousClose", 0),
            "volume": data.get("regularMarketVolume", 0),
            "avg_volume": data.get("averageDailyVolume3Month", 0),
        }
    except:
        return None

def get_kap_news(symbol):
    """KAP'tan son bildirimi çek"""
    try:
        response = supabase.table("disclosures") \
            .select("title, publish_date") \
            .ilike("stock_codes", f"%{symbol}%") \
            .order("disclosure_index", ascending=False) \
            .limit(1) \
            .execute()
        if response.data:
            return response.data[0].get("title", "")
        return ""
    except:
        return ""

def get_ai_explanation(symbol, price, price_change, volume_ratio, kap_news):
    """Groq AI ile Acemi/Usta/Pro açıklama"""
    try:
        context = f"""
Hisse: {symbol} (Borsa İstanbul)
Fiyat: {price:.2f} TL
Fiyat değişimi: %{price_change:.1f}
Hacim: normalin {volume_ratio:.1f} katı
Son KAP bildirimi: {kap_news if kap_news else 'Yok'}
"""
        prompt = f"""
Sen bir Türk finans asistanısın. Aşağıdaki veriye göre 3 seviyede Türkçe açıkla:

{context}

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

def process_symbols():
    symbols = get_bist_symbols()
    signals_found = 0

    for symbol in symbols:
        try:
            data = get_price_data(symbol)
            if not data:
                continue

            price = data["price"]
            prev_close = data["prev_close"]
            volume = data["volume"]
            avg_volume = data["avg_volume"]

            if not price or not prev_close or prev_close == 0:
                continue

            # Fiyat değişimi
            price_change = ((price - prev_close) / prev_close) * 100

            # Hacim oranı
            volume_ratio = (volume / avg_volume) if avg_volume > 0 else 0

            # Sinyal kriterleri
            is_momentum = abs(price_change) >= 10 and volume_ratio >= 3
            is_volume_spike = volume_ratio >= 5

            if not is_momentum and not is_volume_spike:
                time.sleep(0.2)
                continue

            signal_type = "momentum" if is_momentum else "volume_spike"

            print(f"🎯 SİNYAL: {symbol} | %{price_change:.1f} | Hacim: {volume_ratio:.1f}x")

            # KAP haberi çek
            kap_news = get_kap_news(symbol)

            # AI açıklama
            ai_text = get_ai_explanation(symbol, price, price_change, volume_ratio, kap_news)
            acemi, usta, pro = parse_ai_levels(ai_text)

            description = f"{'🚀' if is_momentum else '📊'} {symbol} | {price:.2f} TL | %{price_change:.1f} | Hacim: {volume_ratio:.1f}x"
            if kap_news:
                description += f" | 📰 {kap_news[:80]}"

            signal = {
                "symbol": symbol,
                "signal_type": signal_type,
                "value": round(price_change, 2),
                "description": description,
                "acemi_explanation": acemi,
                "usta_explanation": usta,
                "pro_explanation": pro,
                "price": price,
                "volume_ratio": round(volume_ratio, 2),
                "market": "BIST",
                "created_at": datetime.now(timezone.utc).isoformat()
            }

            supabase.table("tr_signals").insert(signal).execute()
            print(f"✅ KAYDEDİLDİ: {description}")
            signals_found += 1

            time.sleep(0.5)

        except Exception as e:
            print(f"❌ {symbol} hatası: {e}")
            continue

    print(f"✅ Tamamlandı. {signals_found} sinyal kaydedildi.")

if __name__ == "__main__":
    print("🚀 Atlas TR Sinyal Motoru başlatıldı...")
    process_symbols()
