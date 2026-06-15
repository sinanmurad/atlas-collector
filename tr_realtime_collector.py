# -*- coding: utf-8 -*-
import os
import time
import math
import requests
import json
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from supabase import create_client
import firebase_admin
from firebase_admin import credentials, messaging

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
GROQ_KEY = os.environ.get("GROQ_API_KEY")
FIREBASE_SERVICE_ACCOUNT = os.environ.get("FIREBASE_SERVICE_ACCOUNT")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
HEADERS = {'User-Agent': 'Mozilla/5.0'}
signal_cache = {}

# ── OTOMATİK ÖĞRENME SİSTEMİ — sabitler (crypto_collector.py ile aynı) ──
LEARNING_MIN_SAMPLES = 30        # Grup başına min örnek (akademik standart: 30+)
LEARNING_MIN_ABS_Z = 1.96         # ~p<0.05 için z-skor eşiği
LEARNING_MAX_BONUS = 3            # skora uygulanacak max ek/eksi puan
LEARNING_BASELINE_WINRATE = 0.45  # "Başarı" referansı: 24s içinde >%2 kâr oranı
OUTCOME_CHECK_HOURS = [24, 72, 168]   # 24s, 72s, 7g sonuç ölçümü
ZOMBIE_HOLD_HOURS = 24            # Bu süre geçti + kâr yok ise slot temizliği yapılır

try:
    if FIREBASE_SERVICE_ACCOUNT:
        cred = credentials.Certificate(json.loads(FIREBASE_SERVICE_ACCOUNT))
        firebase_admin.initialize_app(cred)
        print("✅ Firebase Admin başlatıldı")
except Exception as e:
    print(f"⚠️ Firebase başlatma hatası: {e}")


def log_activity(event_type, symbol=None, price=None, pnl=None, pnl_pct=None,
                  detail=None, conviction=None, layer=None, market="BIST"):
    """ALIM/SATIM/SİNYAL olaylarını bot_activity_log'a yazar."""
    try:
        supabase.table("bot_activity_log").insert({
            "event_type": event_type,
            "symbol": symbol,
            "market": market,
            "price": price,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "detail": detail,
            "conviction": conviction,
            "layer": layer,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as e:
        print(f"⚠️ log_activity: {e}")


# ============================================================
# OTOMATİK ÖĞRENME SİSTEMİ (crypto_collector.py V12 ile aynı mantık)
# ============================================================
# Akış:
# 1. Her bot alımında signal_outcomes'a kayıt atılır (record_signal_outcome)
# 2. Her taramada check_signal_outcomes() 24s/72s/7g dolan kayıtların
#    gerçek sonucunu ölçer (fiyat şu an ne oldu, kâr/zarar %)
# 3. update_learning_weights() yeterli örnek (>=30) + istatistiksel
#    anlamlılık (|z|>=1.96) varsa learning_weights tablosuna katsayı yazar
# 4. score_coin/calculate_signal_score bu katsayıyı okuyup ±LEARNING_MAX_BONUS uygular
# Hiçbir adım manuel müdahale gerektirmez — sistem kendi kendine evrilir.
# TR/US, signal_outcomes/learning_weights tablolarını market='BIST'/'US'
# filtresiyle kripto ile aynı tablodan paylaşır — karışmazlar.

def score_bucket(score):
    """RSI bucket'ının TR/US karşılığı — entry_score bandı."""
    if score is None:
        return "SCORE_YOK"
    if score < 7:
        return "SCORE<7"
    if score < 10:
        return "SCORE7-10"
    if score < 14:
        return "SCORE10-14"
    return "SCORE14+"


def record_signal_outcome(signal_id, symbol, layer, conviction, score, entry_price, market="BIST"):
    """Bot alımı yapıldığında çağrılır — 24s/72s/7g sonuç takibi için kayıt at."""
    try:
        supabase.table("signal_outcomes").insert({
            "signal_id": signal_id,
            "symbol": symbol,
            "layer": layer,
            "conviction": conviction,
            "entry_score": score,
            "entry_price": entry_price,
            "market": market,
            "checked_24h": False,
            "checked_72h": False,
            "checked_7d": False,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as e:
        print(f"⚠️ signal_outcomes kayıt: {e}")


def check_signal_outcomes(market="BIST", price_fetcher=None):
    """
    24s/72s/7g eşiklerini dolduran kayıtların gerçek sonucunu ölçer.
    price_fetcher(symbol) -> float|None şeklinde bir fonksiyon alır
    (TR için get_price_data, US için yfinance tabanlı bir fetcher).
    """
    try:
        now = datetime.now(timezone.utc)
        field_map = {24: "24h", 72: "72h", 168: "7d"}

        for hours in OUTCOME_CHECK_HOURS:
            field = field_map[hours]
            cutoff = (now - timedelta(hours=hours)).isoformat()

            rows = supabase.table("signal_outcomes") \
                .select("*") \
                .eq(f"checked_{field}", False) \
                .eq("market", market) \
                .lte("created_at", cutoff) \
                .limit(20).execute()

            if not rows.data:
                continue

            for row in rows.data:
                try:
                    current = price_fetcher(row["symbol"]) if price_fetcher else None
                    if current is None:
                        supabase.table("signal_outcomes").update({
                            f"checked_{field}": True
                        }).eq("id", row["id"]).execute()
                        continue

                    entry = float(row["entry_price"])
                    pct = ((current - entry) / entry) * 100 if entry > 0 else 0

                    # Aşırı/imkansız değişim koruması (veri hatası ihtimali)
                    if abs(pct) > 95:
                        print(f"⚠️ {row['symbol']} {field} sonucu şüpheli "
                              f"(%{pct:.1f}, {entry}→{current}) — atlanıyor")
                        continue

                    supabase.table("signal_outcomes").update({
                        f"price_{field}": current,
                        f"pct_{field}": round(pct, 2),
                        f"checked_{field}": True,
                    }).eq("id", row["id"]).execute()

                except Exception:
                    continue
                time.sleep(0.2)

    except Exception as e:
        print(f"❌ check_signal_outcomes ({market}): {e}")


def update_learning_weights(market="BIST"):
    """
    layer + score bucket kombinasyonu başına 24s sonuçlarını
    analiz eder. >=30 örnek VE |z|>=1.96 (yaklaşık p<0.05) ise
    learning_weights'e küçük bir katsayı yazar.
    """
    try:
        rows = supabase.table("signal_outcomes") \
            .select("layer, entry_score, pct_24h") \
            .eq("checked_24h", True) \
            .eq("market", market) \
            .not_.is_("pct_24h", "null") \
            .execute()

        if not rows.data or len(rows.data) < LEARNING_MIN_SAMPLES:
            return

        groups = defaultdict(list)
        for r in rows.data:
            bucket = score_bucket(r.get("entry_score"))
            key = (r.get("layer") or "?", bucket)
            groups[key].append(r["pct_24h"])

        updated = 0
        for (layer, bucket), pcts in groups.items():
            n = len(pcts)
            if n < LEARNING_MIN_SAMPLES:
                continue

            wins = sum(1 for p in pcts if p > 2)
            win_rate = wins / n

            p0 = LEARNING_BASELINE_WINRATE
            se = math.sqrt(p0 * (1 - p0) / n)
            z = (win_rate - p0) / se if se > 0 else 0

            if abs(z) < LEARNING_MIN_ABS_Z:
                continue  # istatistiksel olarak anlamsız, dokunma

            magnitude = min(abs(z) / 3.0, 1.0) * LEARNING_MAX_BONUS
            bonus = round(magnitude) if z > 0 else -round(magnitude)
            bonus = max(-LEARNING_MAX_BONUS, min(LEARNING_MAX_BONUS, bonus))

            if bonus == 0:
                continue

            key_str = f"{layer}|{bucket}"
            supabase.table("learning_weights").upsert({
                "pattern_key": key_str,
                "layer": layer,
                "rsi_bucket": bucket,  # aynı kolon, TR/US için "score bucket" anlamında
                "obv_trend": None,
                "sample_size": n,
                "win_rate": round(win_rate * 100, 1),
                "z_score": round(z, 2),
                "bonus": bonus,
                "market": market,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }, on_conflict="market,pattern_key").execute()

            updated += 1
            print(f"  🧠 ÖĞRENME ({market}): {key_str} → bonus:{bonus:+d} "
                  f"(n={n}, win_rate=%{win_rate*100:.1f}, z={z:.2f})")

            log_activity("OGRENME", detail=f"{key_str} → bonus:{bonus:+d} "
                          f"(n={n}, win_rate=%{win_rate*100:.1f}, z={z:.2f})",
                          layer=layer, market=market)

        if updated:
            print(f"  🧠 {updated} patern güncellendi ({market})")

    except Exception as e:
        print(f"❌ update_learning_weights ({market}): {e}")


_learning_cache_tr = {"data": {}, "ts": 0}


def get_learning_bonus(layer, score, market="BIST"):
    """
    calculate_signal_score/premarket_conviction tarafından çağrılır.
    learning_weights'ten önceden hesaplanmış katsayıyı okur.
    10 dakika cache'lenir.
    """
    global _learning_cache_tr
    now = time.time()
    if now - _learning_cache_tr["ts"] > 600:
        try:
            rows = supabase.table("learning_weights").select("*").eq("market", market).execute()
            _learning_cache_tr["data"] = {r["pattern_key"]: r["bonus"] for r in (rows.data or [])}
            _learning_cache_tr["ts"] = now
        except Exception:
            pass

    bucket = score_bucket(score)
    key = f"{layer}|{bucket}"
    return _learning_cache_tr["data"].get(key, 0)


def log_nightly_learning_summary(market="BIST"):
    """
    Gece taraması bitince çağrılır — learning_weights tablosundaki
    güncel durumu tek bir özet olarak bot_activity_log'a yazar.
    Otomatik, manuel script gerektirmez.
    """
    try:
        rows = supabase.table("learning_weights").select("*").eq("market", market).execute()
        if not rows.data:
            log_activity("OGRENME_OZET", detail="Henüz öğrenme paterni yok (yeterli örnek birikmedi)", market=market)
            return

        parts = []
        for r in sorted(rows.data, key=lambda x: abs(x.get("bonus", 0)), reverse=True)[:5]:
            parts.append(f"{r['pattern_key']}→{r['bonus']:+d}(n={r['sample_size']},wr=%{r['win_rate']})")

        summary = " | ".join(parts)
        print(f"  🧠 GECE ÖZET ({market}): {summary}")
        log_activity("OGRENME_OZET", detail=summary, market=market)
    except Exception as e:
        print(f"❌ log_nightly_learning_summary ({market}): {e}")


def send_push_notification(title, body, market="BIST", signal_id=None):
    try:
        profiles = supabase.table("profiles").select("fcm_token").not_.is_("fcm_token", "null").execute()
        if not profiles.data:
            return
        tokens = [p["fcm_token"] for p in profiles.data if p.get("fcm_token")]
        if not tokens:
            return
        for token in tokens:
            try:
                message = messaging.Message(
                    notification=messaging.Notification(title=title, body=body),
                    data={
                        "market": market,
                        "signal_id": str(signal_id) if signal_id else "",
                        "route": "signals",
                        "click_action": "FLUTTER_NOTIFICATION_CLICK",
                    },
                    android=messaging.AndroidConfig(
                        priority="high",
                        notification=messaging.AndroidNotification(channel_id="atlas_signals"),
                    ),
                    apns=messaging.APNSConfig(
                        payload=messaging.APNSPayload(aps=messaging.Aps(sound="default", badge=1))
                    ),
                    token=token,
                )
                messaging.send(message)
            except Exception as e:
                print(f"⚠️ Push hatası: {e}")
        print(f"📱 Push gönderildi: {len(tokens)} kullanıcı")
    except Exception as e:
        print(f"❌ Push hatası: {e}")


def get_bist_symbols():
    try:
        all_symbols = set()
        offset = 0
        while True:
            r = supabase.table("stock_prices").select("symbol").range(offset, offset + 999).execute()
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
        return []


def load_all_avg_volumes():
    try:
        print("📊 Hacim verileri yükleniyor...")
        all_data = []
        offset = 0
        while True:
            r = supabase.table("stock_prices").select("symbol, volume, updated_at").range(offset, offset + 999).execute()
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


def get_kap_disclosures(symbol):
    try:
        # publish_date kolonu TEXT formatında "DD.MM.YYYY HH:MM:SS" olarak
        # saklanıyor (örn. "15.06.2026 10:14:37"). Bu format ISO ile
        # string karşılaştırması yapıldığında YANLIŞ sonuç verir —
        # "2026-06-13" >= "15.06.2026..." string olarak HER ZAMAN
        # büyük çıkar, yani .gte() filtresi tüm satırları eler ve
        # bu fonksiyon hep None döner. Bu yüzden tarih filtresi
        # Supabase sorgusunda DEĞİL, Python tarafında
        # datetime.strptime ile yapılıyor.
        r = supabase.table("disclosures") \
            .select("title, publish_date, disclosure_index") \
            .ilike("stock_codes", f"%{symbol}%") \
            .order("disclosure_index", ascending=False) \
            .limit(20) \
            .execute()
        if not r.data:
            return None

        cutoff = datetime.now(timezone.utc) - timedelta(days=2)
        recent_rows = []
        for row in r.data:
            pd_str = row.get("publish_date")
            if not pd_str:
                continue
            try:
                dt = datetime.strptime(pd_str, "%d.%m.%Y %H:%M:%S")
                dt = dt.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            if dt >= cutoff:
                recent_rows.append((dt, row))

        if not recent_rows:
            return None

        # En yeniden en eskiye sırala (gerçek tarihe göre, disclosure_index'e değil)
        recent_rows.sort(key=lambda x: x[0], reverse=True)
        titles = [row.get("title", "") for _, row in recent_rows]
        full_text = " ".join(titles).lower()

        tier1_keywords = [
            "finansal sonuç", "kar açıklama", "temettü", "kar dağıtım",
            "birleşme", "devralma", "satın alma", "satış",
            "sermaye artırım", "bedelsiz", "bedelli",
            "özel durum", "esasa ilişkin",
        ]
        tier2_keywords = [
            "sözleşme", "anlaşma", "ihale", "sipariş",
            "kapasite", "üretim", "yatırım", "proje",
            "ortaklık", "işbirliği", "ihracat",
            "genel kurul", "olağan", "olağanüstü",
            "pay alım", "geri alım",
        ]
        tier3_keywords = [
            "atama", "istifa", "değişiklik", "açıklama",
            "bilgi", "düzeltme", "güncelleme",
        ]

        for kw in tier1_keywords:
            if kw in full_text:
                return {"tier": 1, "text": titles[0], "score": 5}
        for kw in tier2_keywords:
            if kw in full_text:
                return {"tier": 2, "text": titles[0], "score": 3}
        for kw in tier3_keywords:
            if kw in full_text:
                return {"tier": 3, "text": titles[0], "score": 1}

        return {"tier": 3, "text": titles[0], "score": 1}
    except:
        return None


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


def calculate_signal_score(price_change, volume_ratio, kap):
    score = 0
    reasons = []

    if kap:
        score += kap["score"]
        tier_label = "🔴 Kritik" if kap["tier"] == 1 else "🟡 Orta" if kap["tier"] == 2 else "🟢 Bilgi"
        reasons.append(f"KAP [{tier_label}]: {kap['text'][:60]}")

    if volume_ratio >= 10:
        score += 5
        reasons.append(f"Hacim {volume_ratio:.1f}x — olağandışı kurumsal ilgi")
    elif volume_ratio >= 5:
        score += 4
        reasons.append(f"Hacim {volume_ratio:.1f}x — güçlü kurumsal ilgi")
    elif volume_ratio >= 3:
        score += 3
        reasons.append(f"Hacim {volume_ratio:.1f}x — kurumsal ilgi")
    elif volume_ratio >= 2:
        score += 2
        reasons.append(f"Hacim {volume_ratio:.1f}x artışı")
    elif volume_ratio >= 1.5:
        score += 1
        reasons.append(f"Hacim {volume_ratio:.1f}x hafif artış")

    if price_change >= 7:
        score += 4
        reasons.append(f"%{price_change:.1f} çok güçlü yükseliş")
    elif price_change >= 5:
        score += 3
        reasons.append(f"%{price_change:.1f} güçlü yükseliş")
    elif price_change >= 3:
        score += 2
        reasons.append(f"%{price_change:.1f} yükseliş")
    elif price_change >= 1:
        score += 1
        reasons.append(f"%{price_change:.1f} hafif yükseliş")
    elif price_change < -3:
        if not kap or kap["tier"] > 2:
            return "NORMAL", [], 0, None
        reasons.append(f"%{price_change:.1f} düşüş — KAP'a rağmen satış")

    if not kap:
        if volume_ratio < 3 or price_change < 3:
            return "NORMAL", [], 0, None

    # Layer ayrımı: KAP kaynaklı mı hacim/fiyat kaynaklı mı
    layer = "KAP" if kap else "HACIM"

    # Öğrenme bonusu — geçmiş 24s sonuçlarına göre ±LEARNING_MAX_BONUS.
    # Bucket sadece (layer, ham_skor) ile belirlenir — conviction henüz
    # hesaplanmadığı için referans olamaz (dönüşümlü bağımlılık olur).
    bonus = get_learning_bonus(layer, score, market="BIST")
    if bonus != 0:
        score += bonus
        reasons.append(f"🧠 Öğrenme katsayısı: {bonus:+d}")

    if score >= 10:
        conviction = "CRITICAL"
    elif score >= 7:
        conviction = "HIGH"
    elif score >= 4:
        conviction = "MEDIUM"
    else:
        conviction = "NORMAL"

    return conviction, reasons, score, layer


def get_ai_explanation(symbol, price, price_change, volume_ratio, kap, day_high, day_low, conviction, reasons, prev_close):
    try:
        kap_text = kap["text"] if kap else "Yok"
        kap_tier = f"Tier {kap['tier']}" if kap else "Yok"

        prompt = f"""Sen profesyonel bir Türk sermaye piyasası analistisin. Aşağıdaki veriye dayanarak 3 seviyeli analiz yaz. Somut ve spesifik ol.

=== VERİ ===
Hisse: {symbol}
Fiyat: {price:.2f} TL
Açılıştan değişim: %{price_change:.1f}
Önceki kapanış: {prev_close:.2f} TL
Gün yüksek/düşük: {day_high:.2f} / {day_low:.2f} TL
Hacim: normalin {volume_ratio:.1f}x
Güven: {conviction}
KAP ({kap_tier}): {kap_text}
Sinyaller: {' | '.join(reasons)}

=== FORMAT ===
===ACEMİ===
[Maks 2 cümle. Yatırımcıya sade Türkçe ile ne olduğunu ve ne yapması gerektiğini anlat.]
===USTA===
[Maks 3 cümle. Teknik analiz: destek/direnç seviyeleri, hacim yorumu, KAP etkisi.]
===PRO===
[Maks 4 cümle. Kurumsal para akışı olasılığı, kataliz gücü (1-10), risk/ödül oranı, TL cinsinden giriş/stop/hedef seviyeleri.]"""

        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"},
            json={
                "model": "llama-3.1-8b-instant",
                "messages": [
                    {"role": "system", "content": "Profesyonel Türk sermaye piyasası analistisin. Sadece verilen formatı kullan. Başlıkları değiştirme. Somut verilerle analiz yap, genel konuşma."},
                    {"role": "user", "content": prompt}
                ],
                "max_tokens": 600,
                "temperature": 0.2
            },
            timeout=15
        )
        resp = r.json()
        if "choices" not in resp:
            print(f"⚠️ Groq: {resp.get('error', {}).get('message', '')}")
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


def get_open_bist_trades(user_id):
    r = supabase.table("demo_trades").select("*") \
        .eq("user_id", user_id).eq("status", "open").eq("market", "BIST").execute()
    return r.data or []


def get_bist_hist_rate(user_id):
    r = supabase.table("demo_trades").select("profit_loss") \
        .eq("user_id", user_id).eq("market", "BIST").eq("status", "closed").execute()
    if not r.data:
        return 0
    wins = sum(1 for t in r.data if (t.get("profit_loss") or 0) > 0)
    return (wins / len(r.data)) * 100


def calc_bist_levels(price, price_change):
    """Günlük hareketten volatilite tahmini.
    Stop = volatilite x1.5, Hedef = volatilite x8 (V12 oranı)"""
    daily_vol = max(abs(price_change), 1.5)
    stop = price * (1 - daily_vol * 1.5 / 100)
    target = price * (1 + daily_vol * 8 / 100)
    return round(stop, 2), round(target, 2)


def bot_should_buy(price_change, volume_ratio, conviction):
    # Savaş mimarisi — sadece en yüksek güven + pozitif hareket
    return conviction == "CRITICAL" and price_change > 0


def bist_bot_should_sell(trade, current_price):
    buy_price = trade["buy_price"]
    stop = trade.get("stop_price") or buy_price * 0.925
    target = trade.get("target_price") or buy_price * 1.12
    peak = max(trade.get("peak_price") or buy_price, current_price)
    change = ((current_price - buy_price) / buy_price) * 100

    if current_price <= stop:
        print(f"  🛑 STOP: %{change:.1f}")
        return True, "stop_loss", peak

    if current_price >= target:
        print(f"  🎯 HEDEF: %{change:.1f}")
        return True, "target_hit", peak

    # Reversal — kâr varken tepeden geri çekiliş
    if peak > buy_price:
        drawdown = (peak - current_price) / peak * 100
        if change >= 1.5 and drawdown >= 2.0:
            print(f"  ⚠️ REVERSAL: tepe {peak:.2f}'den {current_price:.2f}'ye, kâr hâlâ %{change:.1f}")
            return True, "reversal", peak

    # ── ZOMBİ TEMİZLİĞİ ──────────────────────────────────────
    # 24+ saat açık, hâlâ KÂRDA DEĞİL (current <= buy_price) ise
    # slot işgal ediyor — zorla kapat. Kârda olan pozisyonlara
    # DOKUNULMAZ.
    try:
        buy_date = trade.get("buy_date")
        if buy_date:
            buy_dt = datetime.fromisoformat(str(buy_date).replace("Z", "+00:00"))
            if buy_dt.tzinfo is None:
                buy_dt = buy_dt.replace(tzinfo=timezone.utc)
            hold_hours = (datetime.now(timezone.utc) - buy_dt).total_seconds() / 3600
            if hold_hours >= ZOMBIE_HOLD_HOURS and current_price <= buy_price:
                print(f"  ⏰ ZOMBİ: {hold_hours:.1f}s açık, kâr yok (%{change:.1f}) — slot temizliği")
                return True, "zombie_cleanup", peak
    except Exception:
        pass

    return False, "", peak


def bot_buy(user_id, symbol, price, signal_id, is_pro, balance, conviction=None, score=0,
            reasons=None, price_change=0, layer=None):
    try:
        open_trades = get_open_bist_trades(user_id)
        open_count = len(open_trades)
        stop, target = calc_bist_levels(price, price_change)
        entry_reason = " | ".join((reasons or [])[:3]) if reasons else (conviction or "")
        is_exceptional = False

        if not is_pro:
            month_start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0).isoformat()
            month_trades = supabase.table("demo_trades").select("id").eq("user_id", user_id).eq("market", "BIST").gte("created_at", month_start).execute()
            if len(month_trades.data) >= 3:
                print(f"⚠️ {user_id} aylık limit doldu")
                return False
            if open_count >= 1:
                print(f"⚠️ {user_id} açık pozisyon var (Free)")
                return False
        else:
            BASE_CAP = 3
            EXCEPTIONAL_CAP = 2
            MAX_TOTAL = BASE_CAP + EXCEPTIONAL_CAP

            if open_count >= MAX_TOTAL:
                print(f"⚠️ {user_id} max {MAX_TOTAL} pozisyon dolu (Pro)")
                return False

            if open_count >= BASE_CAP:
                hist_rate = get_bist_hist_rate(user_id)
                if not (score >= 10 and hist_rate >= 90):
                    print(f"⚠️ {user_id} istisna kriteri karşılanmadı (score={score}, hist={hist_rate:.0f}%)")
                    return False
                exceptional_open = sum(1 for t in open_trades if t.get("is_exceptional"))
                if exceptional_open >= EXCEPTIONAL_CAP:
                    print(f"⚠️ {user_id} istisna slotları dolu")
                    return False
                is_exceptional = True

        invest = min(balance * 0.10, 100)
        if invest < 10:
            return False
        quantity = invest / price
        supabase.table("demo_trades").insert({
            "user_id": user_id, "symbol": symbol, "market": "BIST",
            "signal_id": signal_id, "buy_price": price,
            "buy_date": datetime.now(timezone.utc).isoformat(),
            "quantity": round(quantity, 4), "status": "open",
            "stop_price": stop, "target_price": target, "peak_price": price,
            "entry_reason": entry_reason, "entry_conviction": conviction,
            "entry_score": score, "is_exceptional": is_exceptional,
            "created_at": datetime.now(timezone.utc).isoformat()
        }).execute()
        supabase.table("demo_portfolios").update({
            "bist_balance": round(balance - invest, 2)
        }).eq("user_id", user_id).execute()
        tag = " 🌟İSTİSNA" if is_exceptional else ""
        print(f"✅ BOT ALIM{tag}: {user_id} → {symbol} @ {price:.2f} TL | Stop:{stop} Hedef:{target}")
        log_activity("ALIM", symbol=symbol, price=price,
                      detail=f"${invest:.0f} yatırım | Stop:{stop} | Hedef:{target}" + (" | İSTİSNAİ" if is_exceptional else ""),
                      conviction=conviction, market="BIST")
        record_signal_outcome(signal_id, symbol, layer, conviction, score, price, market="BIST")
        return True
    except Exception as e:
        print(f"❌ Bot alım hatası: {e}")
        return False


def bot_sell(trade, current_price, exit_reason=""):
    try:
        profit_loss = (current_price - trade["buy_price"]) * trade["quantity"]
        pct = ((current_price - trade["buy_price"]) / trade["buy_price"]) * 100
        supabase.table("demo_trades").update({
            "sell_price": current_price,
            "sell_date": datetime.now(timezone.utc).isoformat(),
            "status": "closed",
            "profit_loss": round(profit_loss, 2),
            "exit_reason": exit_reason
        }).eq("id", trade["id"]).execute()
        portfolio = supabase.table("demo_portfolios").select("bist_balance").eq("user_id", trade["user_id"]).maybeSingle().execute()
        if portfolio.data:
            new_balance = portfolio.data["bist_balance"] + (trade["quantity"] * current_price)
            supabase.table("demo_portfolios").update({"bist_balance": round(new_balance, 2)}).eq("user_id", trade["user_id"]).execute()
        print(f"✅ BOT SATIŞ ({exit_reason}): {trade['symbol']} | K/Z: {profit_loss:.2f} TL")
        log_activity("SATIM", symbol=trade["symbol"], price=current_price,
                      pnl=round(profit_loss, 2), pnl_pct=round(pct, 2),
                      detail=exit_reason,
                      conviction=trade.get("entry_conviction"), market="BIST")
    except Exception as e:
        print(f"❌ Bot satış hatası: {e}")


def bot_check_open_positions():
    try:
        trades = supabase.table("demo_trades").select("*").eq("status", "open").eq("market", "BIST").execute()
        if not trades.data:
            return
        print(f"🔍 {len(trades.data)} açık BIST pozisyon kontrol ediliyor...")
        for trade in trades.data:
            data = get_price_data(trade["symbol"])
            if not data or not data["price"]:
                continue
            current_price = data["price"]
            should_sell, exit_reason, new_peak = bist_bot_should_sell(trade, current_price)
            if should_sell:
                bot_sell(trade, current_price, exit_reason)
            else:
                update_data = {"current_price": current_price}
                if new_peak != (trade.get("peak_price") or trade["buy_price"]):
                    update_data["peak_price"] = new_peak
                supabase.table("demo_trades").update(update_data).eq("id", trade["id"]).execute()
            time.sleep(0.3)
    except Exception as e:
        print(f"❌ Pozisyon kontrol hatası: {e}")


def bot_process_signal(symbol, price, price_change, volume_ratio, conviction, signal_id, score=0, reasons=None, layer=None):
    try:
        if not bot_should_buy(price_change, volume_ratio, conviction):
            return
        print(f"🤖 Bot {symbol}: AL")
        portfolios = supabase.table("demo_portfolios").select("user_id, bist_balance").execute()
        if not portfolios.data:
            return
        for portfolio in portfolios.data:
            user_id = portfolio["user_id"]
            balance = portfolio.get("bist_balance", 0) or 0
            if balance < 10:
                continue
            profile = supabase.table("profiles").select("is_pro").eq("id", user_id).limit(1).execute()
            is_pro = profile.data[0].get("is_pro", False) if profile.data else False
            bot_buy(user_id, symbol, price, signal_id, is_pro, balance, conviction, score, reasons, price_change, layer)
            time.sleep(0.2)
    except Exception as e:
        print(f"❌ Bot sinyal işleme hatası: {e}")


def bot_process_morning_signal(symbol, price, price_change, conviction, signal_id, score=0, reasons=None, layer=None):
    """Gece taramasının sıkı denetimini (KAP+skor) geçmiş sinyaller için
    sabah 09:00-09:30 TR'de çağrılır. bot_should_buy'daki canlı-tarama
    CRITICAL-only kısıtı burada uygulanmaz — denetim zaten gece tamamlandı."""
    try:
        print(f"🤖 Bot {symbol}: SABAH ALIM ({conviction})")
        portfolios = supabase.table("demo_portfolios").select("user_id, bist_balance").execute()
        if not portfolios.data:
            return
        for portfolio in portfolios.data:
            user_id = portfolio["user_id"]
            balance = portfolio.get("bist_balance", 0) or 0
            if balance < 10:
                continue
            profile = supabase.table("profiles").select("is_pro").eq("id", user_id).limit(1).execute()
            is_pro = profile.data[0].get("is_pro", False) if profile.data else False
            bot_buy(user_id, symbol, price, signal_id, is_pro, balance, conviction, score, reasons, price_change, layer)
            time.sleep(0.2)
    except Exception as e:
        print(f"❌ Sabah bot işleme hatası: {e}")


def check_signal_results():
    try:
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
                try:
                    data = get_price_data(signal["symbol"])
                    if not data or not data["price"]:
                        continue
                    current_price = data["price"]
                    change_pct = ((current_price - signal["price"]) / signal["price"]) * 100
                    supabase.table("tr_signals").update({
                        "result_price": current_price,
                        "result_change": round(change_pct, 2),
                        "result_checked_at": datetime.now(timezone.utc).isoformat()
                    }).eq("id", signal["id"]).execute()
                    print(f"📈 Sonuç: {signal['symbol']} %{change_pct:.1f}")
                    time.sleep(0.2)
                except:
                    continue
    except:
        pass


def scan_once(symbols, avg_volumes, send_push=True, is_night=False):
    candidates = []
    now = time.time()

    for symbol in symbols:
        try:
            if symbol in signal_cache and now - signal_cache[symbol] < 3600:
                continue

            data = get_price_data(symbol)
            if not data:
                continue

            price = data["price"]
            open_price = data["open_price"]
            prev_close = data["prev_close"]
            volume = data["volume"]

            if not price or price == 0 or not open_price or open_price == 0:
                continue

            avg_volume = avg_volumes.get(symbol, 0)
            if avg_volume == 0:
                continue

            price_change = ((price - open_price) / open_price) * 100
            volume_ratio = volume / avg_volume

            if volume_ratio > 500 or volume_ratio < 0:
                continue

            if is_night:
                # GECE MODU: borsa kapalı → price≈open, volume_ratio≈0
                # olduğundan hacim/fiyat eşiği anlamsız. Adaylık sadece
                # KAP varlığına bakılarak belirlenir — sıkı skor denetimi
                # (calculate_signal_score) DEĞİŞMEDEN aşağıda uygulanır.
                kap = get_kap_disclosures(symbol)
                if not kap:
                    continue
            else:
                if volume_ratio < 1.5 and abs(price_change) < 2:
                    continue
                kap = None  # ikinci döngüde tekrar çekilecek

            candidates.append({
                "symbol": symbol,
                "price": price,
                "open_price": open_price,
                "prev_close": prev_close or open_price,
                "price_change": price_change,
                "volume_ratio": volume_ratio,
                "day_high": data["day_high"],
                "day_low": data["day_low"],
                "kap_prefetched": kap,
            })

        except Exception:
            continue

    print(f"  📋 {len(candidates)} aday ({'gece/KAP' if is_night else 'canlı'}) — analiz başlıyor...")

    scored = []
    for c in candidates:
        try:
            symbol = c["symbol"]
            kap = c["kap_prefetched"] if c["kap_prefetched"] is not None else get_kap_disclosures(symbol)
            conviction, reasons, score, layer = calculate_signal_score(c["price_change"], c["volume_ratio"], kap)

            if conviction == "NORMAL":
                continue

            last_time = get_last_signal_time(symbol)
            if now - last_time < 3600:
                signal_cache[symbol] = last_time
                continue

            scored.append({**c, "conviction": conviction, "reasons": reasons, "score": score, "kap": kap, "layer": layer})

        except Exception:
            continue

    scored.sort(key=lambda x: x["score"], reverse=True)
    top5 = scored[:5]

    print(f"  🎯 {len(scored)} sinyal adayı → en iyi {len(top5)} seçildi")

    signals_found = 0
    for s in top5:
        try:
            symbol = s["symbol"]
            conviction = s["conviction"]
            price = s["price"]
            price_change = s["price_change"]
            volume_ratio = s["volume_ratio"]
            kap = s["kap"]
            reasons = s["reasons"]
            prev_close = s["prev_close"]
            score = s["score"]

            print(f"\n🎯 {symbol} | {conviction} | Score: {score}")
            for r in reasons:
                print(f"   → {r}")

            ai_text = get_ai_explanation(
                symbol, price, price_change, volume_ratio,
                kap, s["day_high"], s["day_low"],
                conviction, reasons, prev_close
            )
            acemi, usta, pro = parse_ai_levels(ai_text)

            if conviction == "CRITICAL":
                emoji, signal_type = "🔥", "critical"
            elif conviction == "HIGH":
                emoji, signal_type = "⚡", "momentum"
            elif kap and kap["tier"] <= 2:
                emoji, signal_type = "📰", "kap_momentum"
            else:
                emoji, signal_type = "🚀", "momentum"

            description = f"{emoji} {symbol} | {price:.2f} TL | %{price_change:.1f} | Hacim: {volume_ratio:.1f}x | {conviction}"
            if kap:
                description += f" | 📰 {kap['text'][:60]}"

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
            signals_found += 1
            signal_id = result.data[0].get("id") if result.data else None

            log_activity("SINYAL", symbol=symbol, price=price,
                          detail=f"Score:{score} | %{price_change:.1f} | Hacim:{volume_ratio:.1f}x",
                          conviction=conviction, market="BIST")

            if send_push:
                bot_process_signal(symbol, price, price_change, volume_ratio, conviction, signal_id, score, reasons, layer)

            # Canlı modda anlık push, gece modunda push gönderilmez
            if send_push:
                send_push_notification(
                    title=f"{emoji} {symbol} — {conviction}",
                    body=f"{price:.2f} TL | %{price_change:.1f} | Hacim: {volume_ratio:.1f}x",
                    market="BIST",
                    signal_id=signal_id
                )

            print(f"✅ KAYDEDİLDİ [{conviction}]: {description}")
            time.sleep(0.5)

        except Exception as e:
            print(f"❌ {s.get('symbol', '?')}: {e}")
            continue

    return signals_found


def send_morning_signals():
    """Gece hazırlanan sinyalleri 09:00-09:30 TR'de push ile gönder
    VE sabah anlık fiyatıyla bot alım/stop/hedef işlemini başlatır."""
    try:
        since = (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()
        r = supabase.table("tr_signals") \
            .select("*") \
            .gte("created_at", since) \
            .order("created_at", ascending=False) \
            .limit(5) \
            .execute()

        if not r.data:
            print("⚠️ Sabah için sinyal bulunamadı")
            return

        print(f"📱 {len(r.data)} sabah sinyali işleniyor...")
        for signal in r.data:
            symbol = signal["symbol"]
            night_price = signal.get("price", 0)
            value = signal.get("value", 0)
            volume_ratio = signal.get("volume_ratio", 0)
            signal_id = signal["id"]
            signal_type = signal.get("signal_type", "")

            if signal_type == "critical":
                emoji, conviction = "🔥", "CRITICAL"
            elif signal_type == "kap_momentum":
                emoji, conviction = "📰", "HIGH"
            elif signal_type in ["HIGH", "momentum"]:
                emoji, conviction = "⚡", "HIGH"
            else:
                emoji, conviction = "🚀", "MEDIUM"

            # Sabah anlık fiyatı çek — bot alım ve stop/hedef bu fiyat üzerinden
            morning_data = get_price_data(symbol)
            morning_price = morning_data["price"] if morning_data and morning_data.get("price") else night_price

            send_push_notification(
                title=f"{emoji} {symbol} — Sabah Sinyali",
                body=f"{morning_price:.2f} TL | %{value:.1f} | Hacim: {volume_ratio:.1f}x",
                market="BIST",
                signal_id=signal_id
            )

            # Gece sıkı denetimi geçen sinyal için sabah fiyatıyla
            # bot alım + stop/hedef + öğrenme kaydı başlat.
            score = signal.get("score") or (10 if conviction == "CRITICAL" else 7 if conviction == "HIGH" else 4)
            reasons = [signal.get("description", "")]
            layer = "KAP" if signal_type == "kap_momentum" else "HACIM"
            price_change_morning = ((morning_price - night_price) / night_price * 100) if night_price else 0

            bot_process_morning_signal(symbol, morning_price, price_change_morning,
                                        conviction, signal_id, score, reasons, layer)

            time.sleep(0.5)

        print("✅ Sabah sinyalleri işlendi (push + bot alım).")
    except Exception as e:
        print(f"❌ Sabah sinyal gönderme hatası: {e}")


def main():
    print("🚀 Atlas TR Kartal Gözü başlatıldı...")
    symbols = get_bist_symbols()
    avg_volumes = load_all_avg_volumes()

    print("🔄 Sinyal cache yükleniyor...")
    try:
        since = (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()
        r = supabase.table("tr_signals").select("symbol, created_at").gte("created_at", since).execute()
        for row in r.data:
            sym = row["symbol"]
            dt = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
            signal_cache[sym] = dt.timestamp()
        print(f"✅ {len(signal_cache)} sembol cache'e yüklendi")
    except Exception as e:
        print(f"⚠️ Cache yükleme hatası: {e}")

    scan_count = 0
    night_scan_done = False
    morning_signals_sent = False

    while True:
        try:
            now_utc = datetime.now(timezone.utc)
            hour = now_utc.hour
            minute = now_utc.minute

            # ============================================================
            # GECE MODU: 14:00-05:59 UTC (17:00-08:59 TR)
            # Borsa kapandıktan sonra KAP-bazlı analiz yap
            # ============================================================
            if hour >= 14 or hour < 6:
                if not night_scan_done:
                    print(f"\n🌙 GECE MOTORU başlıyor... {now_utc.strftime('%H:%M UTC')}")
                    signal_cache.clear()  # Eski cache temizle
                    found = scan_once(symbols, avg_volumes, send_push=False, is_night=True)
                    check_signal_outcomes(market="BIST", price_fetcher=lambda s: (get_price_data(s) or {}).get("price"))
                    update_learning_weights(market="BIST")
                    log_nightly_learning_summary(market="BIST")
                    night_scan_done = True
                    morning_signals_sent = False
                    print(f"✅ Gece taraması bitti. {found} sinyal hazırlandı. Sabah 09:00-09:30 TR'de gönderilecek.")
                else:
                    print(f"💤 Gece bekleniyor... {now_utc.strftime('%H:%M UTC')}")
                time.sleep(600)

            # ============================================================
            # SABAH SİNYAL GÖNDERME: 06:00-06:29 UTC (09:00-09:29 TR)
            # Gece hazırlanan sinyalleri push et + sabah fiyatıyla bot alım
            # ============================================================
            elif hour == 6 and minute < 30 and not morning_signals_sent:
                print(f"\n🦅 SABAH SİNYALLERİ — {now_utc.strftime('%H:%M UTC')} (09:00-09:30 TR)")
                send_morning_signals()
                morning_signals_sent = True
                night_scan_done = False
                time.sleep(120)

            # ============================================================
            # CANLI TARAMA: 06:30-13:59 UTC (09:30-16:59 TR)
            # Borsa açık — 2 dk'da bir tarama
            # ============================================================
            elif (hour == 6 and minute >= 30) or (7 <= hour < 14):
                print(f"\n📡 Canlı tarama... {now_utc.strftime('%H:%M:%S')} UTC")
                found = scan_once(symbols, avg_volumes)
                scan_count += 1
                if scan_count % 6 == 0:
                    bot_check_open_positions()
                if scan_count % 12 == 0:
                    check_signal_results()
                if scan_count % 3 == 0:
                    check_signal_outcomes(market="BIST", price_fetcher=lambda s: (get_price_data(s) or {}).get("price"))
                    update_learning_weights(market="BIST")
                print(f"✅ Tarama bitti. {found} sinyal. 2 dk bekleniyor...")
                time.sleep(120)

            else:
                print(f"💤 Bekleniyor... {now_utc.strftime('%H:%M UTC')}")
                time.sleep(300)

        except Exception as e:
            print(f"❌ Ana döngü hatası: {e}")
            time.sleep(60)


if __name__ == "__main__":
    main()
