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
signal_cache = {}


def get_last_signal_time(symbol):
    try:
        r = supabase.table("tr_signals") \
            .select("created_at") \
            .eq("symbol", symbol) \
            .order("created_at", ascending=False) \
            .limit(1) \
            .execute()
        if r.data:
            dt = datetime.fromisoformat(r.data[0]["created_at"].replace("Z", "+00:00"))
            return dt.timestamp()
        return 0
    except:
        return 0


def load_all_avg_volumes():
    try:
        print("📊 Tüm hacim verileri yükleniyor...")
        all_data = []
        offset = 0
        while True:
            r = supabase.table("stock_prices") \
                .select("symbol, volume, updated_at") \
                .range(offset, offset + 999) \
                .execute()
            if not r.data:
                break
            all_data.extend(r.data)
            if len(r.data) < 1000:
                break
            offset += 1000

        symbol_daily = {}
        for row in all_data:
            sym = row["symbol"]
            day = row["updated_at"][:10]
            vol = row.get("volume", 0) or 0
            if sym not in symbol_daily:
                symbol_daily[sym] = {}
            if day not in symbol_daily[sym] or vol > symbol_daily[sym][day]:
                symbol_daily[sym][day] = vol

        avg_volumes = {}
        for sym, days in symbol_daily.items():
            volumes = list(days.values())
            avg_volumes[sym] = sum(volumes) / len(volumes) if volumes else 0

        loaded = sum(1 for v in avg_volumes.values() if v > 0)
        print(f"✅ {loaded}/{len(avg_volumes)} hisse için hacim verisi yüklendi")
        return avg_volumes
    except Exception as e:
        print(f"⚠️ Hacim yükleme hatası: {e}")
        return {}


def get_bist_symbols():
    try:
        all_symbols = set()
        offset = 0
        while True:
            r = supabase.table("stock_prices") \
                .select("symbol") \
                .range(offset, offset + 999) \
                .execute()
            if not r.data:
                break
            for row in r.data:
                all_symbols.add(row["symbol"])
            if len(r.data) < 1000:
                break
            offset += 1000
        symbols = list(all_symbols)
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

        open_price = meta.get("regularMarketOpen", 0)
        if not open_price:
            opens = data.get("indicators", {}).get("quote", [{}])[0].get("open", [])
            open_price = next((x for x in opens if x), 0)

        prev_close = meta.get("previousClose", 0)
        if not open_price or open_price == 0:
            open_price = prev_close

        return {
            "price": meta.get("regularMarketPrice", 0),
            "open_price": open_price,
            "prev_close": prev_close,
            "volume": meta.get("regularMarketVolume", 0),
            "day_high": meta.get("regularMarketDayHigh", 0),
            "day_low": meta.get("regularMarketDayLow", 0),
        }
    except:
        return None


def get_kap_news(symbol):
    try:
        r = supabase.table("disclosures") \
            .select("title") \
            .ilike("stock_codes", f"%{symbol}%") \
            .order("disclosure_index", ascending=False) \
            .limit(1) \
            .execute()
        if r.data:
            return r.data[0].get("title", "")
        return ""
    except:
        return ""


def get_ai_explanation(symbol, price, price_change, volume_ratio, kap_news, day_high, day_low):
    try:
        prompt = f"""Türk finans asistanısın. Kısa ve net yaz.

{symbol} | {price:.2f} TL | %{price_change:.1f} | Hacim: {volume_ratio:.1f}x
KAP: {kap_news if kap_news else 'Yok'} | Yüksek: {day_high:.2f} | Düşük: {day_low:.2f}

===ACEMİ===
[1 cümle, çok sade Türkçe]
===USTA===
[2 cümle, teknik analiz]
===PRO===
[3 cümle, profesyonel analiz]"""

        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "llama-3.1-8b-instant",
                "messages": [
                    {
                        "role": "system",
                        "content": "Türk finans asistanısın. Sadece verilen formatı kullan. ===ACEMİ===, ===USTA===, ===PRO=== başlıklarını değiştirme. Her seviye çok kısa olsun."
                    },
                    {"role": "user", "content": prompt}
                ],
                "max_tokens": 300,
                "temperature": 0.3
            },
            timeout=15
        )
        resp = r.json()
        if "choices" not in resp:
            print(f"⚠️ Groq: {resp.get('error', {}).get('message', str(resp))}")
            return ""
        result = resp["choices"][0]["message"]["content"]
        print(f"✅ AI: {result[:60]}...")
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


def save_signal_result(symbol, signal_id, buy_price):
    """24 saat sonra fiyatı kontrol et ve kaydet"""
    try:
        data = get_price_data(symbol)
        if not data or not data["price"]:
            return
        current_price = data["price"]
        change_pct = ((current_price - buy_price) / buy_price) * 100
        supabase.table("tr_signals").update({
            "result_price": current_price,
            "result_change": round(change_pct, 2),
            "result_checked_at": datetime.now(timezone.utc).isoformat()
        }).eq("id", signal_id).execute()
        print(f"📈 Sonuç kaydedildi: {symbol} %{change_pct:.1f}")
    except:
        pass


def check_signal_results():
    """24 saat önce verilen sinyallerin sonucunu kontrol et"""
    try:
        from datetime import timedelta
        yesterday = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        day_before = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()

        r = supabase.table("tr_signals") \
            .select("id, symbol, price") \
            .gte("created_at", day_before) \
            .lte("created_at", yesterday) \
            .is_("result_price", "null") \
            .execute()

        if r.data:
            print(f"📊 {len(r.data)} sinyal sonucu kontrol ediliyor...")
            for signal in r.data:
                save_signal_result(signal["symbol"], signal["id"], signal["price"])
                time.sleep(0.2)
    except:
        pass


def scan_once(symbols, avg_volumes):
    signals_found = 0
    now = time.time()

    for symbol in symbols:
        try:
            if symbol in signal_cache:
                if now - signal_cache[symbol] < 3600:
                    continue

            data = get_price_data(symbol)
            if not data:
                time.sleep(0.05)
                continue

            price = data["price"]
            open_price = data["open_price"]
            volume = data["volume"]
            day_high = data["day_high"]
            day_low = data["day_low"]

            if not price or price == 0 or not open_price or open_price == 0:
                time.sleep(0.05)
                continue

            avg_volume = avg_volumes.get(symbol, 0)
            if avg_volume == 0:
                time.sleep(0.05)
                continue

            price_change = ((price - open_price) / open_price) * 100
            volume_ratio = volume / avg_volume

            if volume_ratio > 500:
                time.sleep(0.05)
                continue

            is_momentum = abs(price_change) >= 5 and volume_ratio >= 2
            is_volume_spike = volume_ratio >= 5

            if not is_momentum and not is_volume_spike:
                time.sleep(0.05)
                continue

            last_time = get_last_signal_time(symbol)
            if now - last_time < 3600:
                signal_cache[symbol] = last_time
                continue

            signal_type = "momentum" if is_momentum else "volume_spike"
            print(f"🎯 {symbol} | Açılış:{open_price:.2f} Şimdi:{price:.2f} | %{price_change:.1f} | {volume_ratio:.1f}x")

            kap_news = get_kap_news(symbol)
            ai_text = get_ai_explanation(symbol, price, price_change, volume_ratio, kap_news, day_high, day_low)
            acemi, usta, pro = parse_ai_levels(ai_text)

            description = f"{'🚀' if is_momentum else '📊'} {symbol} | {price:.2f} TL | %{price_change:.1f} | Hacim: {volume_ratio:.1f}x"
            if kap_news:
                description += f" | 📰 {kap_news[:80]}"

            result = supabase.table("tr_signals").insert({
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
            }).execute()

            signal_cache[symbol] = now
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
    avg_volumes = load_all_avg_volumes()

    print("🔄 Mevcut sinyaller cache'e yükleniyor...")
    for symbol in symbols:
        last_time = get_last_signal_time(symbol)
        if last_time > 0:
            signal_cache[symbol] = last_time
    print(f"✅ {len(signal_cache)} sembol cache'e yüklendi")

    scan_count = 0

    while True:
        now = datetime.now()
        hour = now.hour

        if 7 <= hour < 15:
            print(f"📡 Tarama başlıyor... {now.strftime('%H:%M:%S')} UTC")
            found = scan_once(symbols, avg_volumes)
            scan_count += 1

            # Her 12 taramada bir (~24 dk) sinyal sonuçlarını kontrol et
            if scan_count % 12 == 0:
                check_signal_results()

            print(f"✅ Tarama bitti. {found} sinyal. 2 dk bekleniyor...")
            time.sleep(120)
        else:
            print(f"💤 Borsa kapalı. Bekleniyor... {now.strftime('%H:%M:%S')} UTC")
            time.sleep(300)


if __name__ == "__main__":
    main()
