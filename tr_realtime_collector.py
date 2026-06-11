# -*- coding: utf-8 -*-
import os
import time
import requests
import json
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

try:
    if FIREBASE_SERVICE_ACCOUNT:
        cred = credentials.Certificate(json.loads(FIREBASE_SERVICE_ACCOUNT))
        firebase_admin.initialize_app(cred)
        print("✅ Firebase Admin başlatıldı")
except Exception as e:
    print(f"⚠️ Firebase başlatma hatası: {e}")


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
        yesterday = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d")
        r = supabase.table("disclosures") \
            .select("title, publish_date, disclosure_index") \
            .ilike("stock_codes", f"%{symbol}%") \
            .gte("publish_date", yesterday) \
            .order("disclosure_index", ascending=False) \
            .limit(5) \
            .execute()
        if not r.data:
            return None

        titles = [row.get("title", "") for row in r.data]
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
            return "NORMAL", [], 0
        reasons.append(f"%{price_change:.1f} düşüş — KAP'a rağmen satış")

    if not kap:
        if volume_ratio < 3 or price_change < 3:
            return "NORMAL", [], 0

    if score >= 10:
        conviction = "CRITICAL"
    elif score >= 7:
        conviction = "HIGH"
    elif score >= 4:
        conviction = "MEDIUM"
    else:
        conviction = "NORMAL"

    return conviction, reasons, score


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


def bot_should_buy(price_change, volume_ratio, conviction):
    if price_change < 0:
        return False
    if conviction in ["CRITICAL", "HIGH"]:
        return True
    if price_change >= 3 and volume_ratio >= 3:
        return True
    if conviction == "MEDIUM" and price_change >= 2 and volume_ratio >= 2:
        return True
    if volume_ratio >= 5 and price_change > 0:
        return True
    return False


def bot_should_sell(buy_price, current_price):
    change = ((current_price - buy_price) / buy_price) * 100
    if change >= 10:
        print(f"  💰 %{change:.1f} kar — SAT")
        return True
    if change <= -5:
        print(f"  🛑 %{change:.1f} zarar — STOP LOSS")
        return True
    return False


def bot_buy(user_id, symbol, price, signal_id, is_pro, balance):
    try:
        if not is_pro:
            month_start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0).isoformat()
            month_trades = supabase.table("demo_trades").select("id").eq("user_id", user_id).gte("created_at", month_start).execute()
            if len(month_trades.data) >= 3:
                print(f"⚠️ {user_id} aylık limit doldu")
                return False
            open_trades = supabase.table("demo_trades").select("id").eq("user_id", user_id).eq("status", "open").execute()
            if len(open_trades.data) >= 1:
                print(f"⚠️ {user_id} açık pozisyon var")
                return False
        invest = min(balance * 0.10, 100)
        if invest < 10:
            return False
        quantity = invest / price
        supabase.table("demo_trades").insert({
            "user_id": user_id, "symbol": symbol, "market": "BIST",
            "signal_id": signal_id, "buy_price": price,
            "buy_date": datetime.now(timezone.utc).isoformat(),
            "quantity": round(quantity, 4), "status": "open",
            "created_at": datetime.now(timezone.utc).isoformat()
        }).execute()
        supabase.table("demo_portfolios").update({
            "balance": round(balance - invest, 2)
        }).eq("user_id", user_id).execute()
        print(f"✅ BOT ALIM: {user_id} → {symbol} @ {price:.2f} TL")
        return True
    except Exception as e:
        print(f"❌ Bot alım hatası: {e}")
        return False


def bot_sell(trade, current_price):
    try:
        profit_loss = (current_price - trade["buy_price"]) * trade["quantity"]
        supabase.table("demo_trades").update({
            "sell_price": current_price,
            "sell_date": datetime.now(timezone.utc).isoformat(),
            "status": "closed",
            "profit_loss": round(profit_loss, 2)
        }).eq("id", trade["id"]).execute()
        portfolio = supabase.table("demo_portfolios").select("balance").eq("user_id", trade["user_id"]).maybeSingle().execute()
        if portfolio.data:
            new_balance = portfolio.data["balance"] + (trade["quantity"] * current_price)
            supabase.table("demo_portfolios").update({"balance": round(new_balance, 2)}).eq("user_id", trade["user_id"]).execute()
        print(f"✅ BOT SATIŞ: {trade['symbol']} | K/Z: {profit_loss:.2f} TL")
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
            if bot_should_sell(trade["buy_price"], data["price"]):
                bot_sell(trade, data["price"])
            time.sleep(0.3)
    except Exception as e:
        print(f"❌ Pozisyon kontrol hatası: {e}")


def bot_process_signal(symbol, price, price_change, volume_ratio, conviction, signal_id):
    try:
        if not bot_should_buy(price_change, volume_ratio, conviction):
            return
        print(f"🤖 Bot {symbol}: AL")
        portfolios = supabase.table("demo_portfolios").select("user_id, balance").execute()
        if not portfolios.data:
            return
        for portfolio in portfolios.data:
            user_id = portfolio["user_id"]
            balance = portfolio["balance"]
            if balance < 10:
                continue
            profile = supabase.table("profiles").select("is_pro").eq("id", user_id).maybeSingle().execute()
            is_pro = profile.data.get("is_pro", False) if profile.data else False
            bot_buy(user_id, symbol, price, signal_id, is_pro, balance)
            time.sleep(0.2)
    except Exception as e:
        print(f"❌ Bot sinyal işleme hatası: {e}")


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


def scan_once(symbols, avg_volumes):
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

            if volume_ratio < 1.5 and abs(price_change) < 2:
                continue

            candidates.append({
                "symbol": symbol,
                "price": price,
                "open_price": open_price,
                "prev_close": prev_close or open_price,
                "price_change": price_change,
                "volume_ratio": volume_ratio,
                "day_high": data["day_high"],
                "day_low": data["day_low"],
            })

        except:
            continue

    print(f"  📋 {len(candidates)} aday — KAP analizi başlıyor...")

    scored = []
    for c in candidates:
        try:
            symbol = c["symbol"]
            kap = get_kap_disclosures(symbol)
            conviction, reasons, score = calculate_signal_score(c["price_change"], c["volume_ratio"], kap)

            if conviction == "NORMAL":
                continue

            last_time = get_last_signal_time(symbol)
            if now - last_time < 3600:
                signal_cache[symbol] = last_time
                continue

            scored.append({**c, "conviction": conviction, "reasons": reasons, "score": score, "kap": kap})

        except:
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

            print(f"\n🎯 {symbol} | {conviction} | Score: {s['score']}")
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

            bot_process_signal(symbol, price, price_change, volume_ratio, conviction, signal_id)

            # Canlı modda anlık push, gece modunda push gönderilmez
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
    """Gece hazırlanan sinyalleri 09:30 TR'de push ile gönder"""
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

        print(f"📱 {len(r.data)} sabah sinyali gönderiliyor...")
        for signal in r.data:
            symbol = signal["symbol"]
            price = signal.get("price", 0)
            value = signal.get("value", 0)
            volume_ratio = signal.get("volume_ratio", 0)
            signal_id = signal["id"]
            signal_type = signal.get("signal_type", "")

            if signal_type == "critical":
                emoji = "🔥"
            elif signal_type == "kap_momentum":
                emoji = "📰"
            elif signal_type in ["HIGH", "momentum"]:
                emoji = "⚡"
            else:
                emoji = "🚀"

            send_push_notification(
                title=f"{emoji} {symbol} — Sabah Sinyali",
                body=f"{price:.2f} TL | %{value:.1f} | Hacim: {volume_ratio:.1f}x",
                market="BIST",
                signal_id=signal_id
            )
            time.sleep(0.5)

        print("✅ Sabah sinyalleri gönderildi.")
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
            # GECE MODU: 14:00-06:29 UTC (17:00-09:29 TR)
            # Borsa kapandıktan sonra KAP + trend analizi yap
            # ============================================================
            if hour >= 14 or hour < 6:
                if not night_scan_done:
                    print(f"\n🌙 GECE MOTORU başlıyor... {now_utc.strftime('%H:%M UTC')}")
                    signal_cache.clear()  # Eski cache temizle
                    found = scan_once(symbols, avg_volumes)
                    night_scan_done = True
                    morning_signals_sent = False
                    print(f"✅ Gece taraması bitti. {found} sinyal hazırlandı. Sabah 09:30'da gönderilecek.")
                else:
                    print(f"💤 Gece bekleniyor... {now_utc.strftime('%H:%M UTC')}")
                time.sleep(600)

            # ============================================================
            # SABAH SİNYAL GÖNDERME: 06:30-06:59 UTC (09:30-09:59 TR)
            # Gece hazırlanan sinyalleri push ile gönder
            # ============================================================
            elif hour == 6 and minute >= 30 and not morning_signals_sent:
                print(f"\n🦅 SABAH SİNYALLERİ — {now_utc.strftime('%H:%M UTC')} (09:30 TR)")
                send_morning_signals()
                morning_signals_sent = True
                night_scan_done = False
                time.sleep(120)

            # ============================================================
            # CANLI TARAMA: 07:00-13:59 UTC (10:00-16:59 TR)
            # Borsa açık — 2 dk'da bir tarama
            # ============================================================
            elif 7 <= hour < 14:
                print(f"\n📡 Canlı tarama... {now_utc.strftime('%H:%M:%S')} UTC")
                found = scan_once(symbols, avg_volumes)
                scan_count += 1
                if scan_count % 6 == 0:
                    bot_check_open_positions()
                if scan_count % 12 == 0:
                    check_signal_results()
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
