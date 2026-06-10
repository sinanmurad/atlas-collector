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

# Spam kontrolü — Supabase'den oku, Railway restart'ta sıfırlanmasın
def get_last_signal_time(symbol):
    try:
        r = supabase.table("tr_signals") \
            .select("created_at") \
            .eq("symbol", symbol) \
            .order("created_at", ascending=False) \
            .limit(1) \
            .execute()
        if r.data:
            last = r.data[0]["created_at"]
            dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
            return dt.timestamp()
        return 0
    except:
        return 0

def get_bist_symbols():
    try:
        response = supabase.table("stock_prices").select("symbol").execute()
        symbols = list(set([r["symbol"] for r in response.data]))
        print(f"✅ {len(symbols)} BIST hissesi yüklendi")
        return symbols
    except Exception as e:
        print(f"⚠️ Hata: {e}")
        return ["THYAO", "AKBNK", "GARAN", "ASELS", "SISE", "EREGL", "BIMAS", "TUPRS", "PGSUS", "KCHOL"]

def get_price_data(symbol):
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}.IS"
        r = requests.get(url, headers=HEADERS, timeout=8)
        data = r.json()["chart"]["result"][0]
        meta = data["meta"]

        # Açılış fiyatını al — gün içi hareket için doğru referans
        open_price = meta.get("regularMarketOpen", 0)
        if not open_price:
            # Fallback: ilk işlem fiyatı
            opens = data.get("indicators", {}).get("quote", [{}])[0].get("open", [])
            open_price = next((x for x in opens if x), 0)

        return {
            "price": meta.get("regularMarketPrice", 0),
            "open_price": open_price,
            "prev_close": meta.get("previousClose", 0),
            "volume": meta.get("regularMarketVolume", 0),
            "avg_volume": meta.get("averageDailyVolume3Month", 0),
            "day_high": meta.get("regularMarketDayHigh", 0),
            "day_low": meta.get("regularMarketDayLow", 0),
        }
    except:
        return None

def get_kap_news(symbol):
    try:
        response = supabase.table("disclosures") \
            .select("title") \
            .ilike("stock_codes", f"%{symbol}%") \
            .order("disclosure_index", ascending=False) \
            .limit(1) \
            .execute()
        if response.data:
            return response.data[0].get("title", "")
        return ""
    except:
        return ""

def get_ai_explanation(symbol, price, price_change, volume_ratio, kap_news, day_high, day_low):
    try:
        prompt = f"""Sen bir Türk finans asistanısın. Aşağıdaki hisse için 3 seviyede açıklama yaz.

Hisse: {symbol}
Fiyat: {price:.2f} TL
Değişim: %{price_change:.1f}
Hacim: {volume_ratio:.1f}x
KAP: {kap_news if kap_news else 'Yok'}
Yüksek: {day_high:.2f} TL
Düşük: {day_low:.2f} TL

===ACEMİ===
[1-2 cümle, sade Türkçe]
===USTA===
[teknik analiz, orta düzey]
===PRO===
[profesyonel analiz]"""

        response = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "llama3-8b-8192",
                "messages": [
                    {
                        "role": "system",
                        "content": "Sen bir finans asistanısın. Sadece verilen formatı kullan. ===ACEMİ===, ===USTA===, ===PRO=== başlıklarını değiştirme."
                    },
                    {"role": "user", "content": prompt}
                ],
                "max_tokens": 600,
                "temperature": 0.3
            },
            timeout=15
        )
        result = response.json()["choices"][0]["message"]["content"]
        print(f"✅ AI açıklama: {result[:50]}...")
        return result
    except Exception as e:
        print(f"⚠️ AI hatası: {e}")
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
                time.sleep(0.05)
                continue

            price = data["price"]
            open_price = data["open_price"]
            prev_close = data["prev_close"]
            volume = data["volume"]
            avg_volume = data["avg_volume"]
            day_high = data["day_high"]
            day_low = data["day_low"]

            # Geçersiz veri filtresi
            # Geçersiz veri filtresi
if not price or price == 0:
    time.sleep(0.05)
    continue

if not open_price or open_price == 0:
    open_price = prev_close if prev_close else 0

if open_price == 0:
    time.sleep(0.05)
    continue


            if avg_volume < 10000:
                time.sleep(0.05)
                continue

            # Açılış fiyatından değişim — kapanış değil!
            price_change = ((price - open_price) / open_price) * 100

            # Hacim oranı
            volume_ratio = volume / avg_volume if avg_volume > 0 else 0

            # Sahte sinyal filtresi
            if volume_ratio > 500:
                time.sleep(0.05)
                continue

            is_momentum = abs(price_change) >= 5 and volume_ratio >= 2
            is_volume_spike = volume_ratio >= 5

            if not is_momentum and not is_volume_spike:
                time.sleep(0.05)
                continue

            # Spam kontrolü — Supabase'den oku
            last_time = get_last_signal_time(symbol)
            now = time.time()
            if now - last_time < 3600:  # 1 saat
                continue

            signal_type = "momentum" if is_momentum else "volume_spike"
            print(f"🎯 {symbol} | Açılış: {open_price:.2f} | Şimdi: {price:.2f} | %{price_change:.1f} | {volume_ratio:.1f}x")

            kap_news = get_kap_news(symbol)
            ai_text = get_ai_explanation(symbol, price, price_change, volume_ratio, kap_news, day_high, day_low)
            acemi, usta, pro = parse_ai_levels(ai_text)

            description = f"{'🚀' if is_momentum else '📊'} {symbol} | {price:.2f} TL | %{price_change:.1f} (açılıştan) | Hacim: {volume_ratio:.1f}x"
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
            print(f"❌ {symbol}: {e}")
            continue

    return signals_found

def main():
    print("🚀 Atlas TR Gerçek Zamanlı Sinyal Motoru başlatıldı...")
    symbols = get_bist_symbols()

    while True:
        now = datetime.now()
        hour = now.hour

        # BIST saatleri: 10:00-18:00 Türkiye (UTC+3 = 07:00-15:00 UTC)
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
