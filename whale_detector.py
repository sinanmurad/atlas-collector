# -*- coding: utf-8 -*-
import os
import json
import time
import requests
import websocket
import yfinance as yf
from datetime import datetime, timezone, timedelta
from supabase import create_client
import firebase_admin
from firebase_admin import credentials, messaging

FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
GROQ_KEY = os.environ.get("GROQ_API_KEY")
FIREBASE_SERVICE_ACCOUNT = os.environ.get("FIREBASE_SERVICE_ACCOUNT")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

signal_cache = {}
avg_volumes = {}
active_symbols = []
news_cache = {}
last_price_update = {}
company_cache = {}
analyst_cache = {}
premarket_signal_cache = {}

try:
    if FIREBASE_SERVICE_ACCOUNT and not firebase_admin._apps:
        cred = credentials.Certificate(json.loads(FIREBASE_SERVICE_ACCOUNT))
        firebase_admin.initialize_app(cred)
        print("✅ Firebase Admin başlatıldı")
except Exception as e:
    print(f"⚠️ Firebase başlatma hatası: {e}")


def send_push_notification(title, body, market="US", signal_id=None):
    try:
        profiles = supabase.table("profiles") \
            .select("fcm_token") \
            .not_.is_("fcm_token", "null") \
            .execute()
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
                        notification=messaging.AndroidNotification(
                            channel_id="atlas_signals",
                        ),
                    ),
                    apns=messaging.APNSConfig(
                        payload=messaging.APNSPayload(
                            aps=messaging.Aps(sound="default", badge=1)
                        )
                    ),
                    token=token,
                )
                messaging.send(message)
            except Exception as e:
                print(f"⚠️ Push hatası: {e}")
        print(f"📱 Push gönderildi: {len(tokens)} kullanıcı")
    except Exception as e:
        print(f"❌ Push notification hatası: {e}")


def is_market_open():
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5:
        return False
    open_t = now.replace(hour=13, minute=30, second=0, microsecond=0)
    close_t = now.replace(hour=20, minute=0, second=0, microsecond=0)
    return open_t <= now <= close_t


def is_premarket():
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5:
        return False
    premarket_start = now.replace(hour=9, minute=30, second=0, microsecond=0)
    market_open = now.replace(hour=13, minute=30, second=0, microsecond=0)
    return premarket_start <= now < market_open


def load_watchlist_from_db():
    """Supabase'den mevcut watchlist'i yükle — hızlı başlangıç"""
    global active_symbols
    try:
        print("📋 Watchlist Supabase'den yükleniyor...")
        result = supabase.table("us_watchlist") \
            .select("symbol, avg_volume, last_price, name, sector") \
            .execute()
        if not result.data:
            print("⚠️ Watchlist boş")
            return []
        symbols = []
        for row in result.data:
            sym = row["symbol"]
            vol = row.get("avg_volume", 0) or 0
            name = row.get("name", "")
            sector = row.get("sector", "")
            if vol > 0:
                avg_volumes[sym] = vol
                symbols.append(sym)
                if name:
                    company_cache[sym] = {"name": name, "sector": sector, "market_cap": 0, "country": "", "exchange": ""}
        print(f"✅ {len(symbols)} hisse yüklendi")
        return symbols
    except Exception as e:
        print(f"❌ Watchlist yükleme hatası: {e}")
        return []


def refresh_watchlist_background():
    """Gece watchlist'i yenile — yfinance batch"""
    global active_symbols
    try:
        print("🔄 Watchlist yenileniyor (arka plan)...")

        def get_symbols():
            nasdaq, nyse = [], []
            try:
                r = requests.get('https://raw.githubusercontent.com/datasets/nasdaq-listings/main/data/nasdaq-listed.csv', timeout=10)
                nasdaq = [l.split(',')[0] for l in r.text.strip().split('\n')[1:] if l.split(',')[0].isalpha() and 2 <= len(l.split(',')[0]) <= 5]
            except:
                pass
            try:
                r = requests.get('https://raw.githubusercontent.com/datasets/nyse-listings/main/data/nyse-listed.csv', timeout=10)
                nyse = [l.split(',')[0] for l in r.text.strip().split('\n')[1:] if l.split(',')[0].isalpha() and 2 <= len(l.split(',')[0]) <= 5]
            except:
                pass
            return list(set(nasdaq + nyse))

        all_symbols = get_symbols()
        candidates = []
        batch_size = 200

        for i in range(0, len(all_symbols), batch_size):
            batch = all_symbols[i:i + batch_size]
            try:
                data = yf.download(' '.join(batch), period='5d', interval='1d', progress=False, threads=True)
                if data.empty:
                    continue
                closes = data['Close'].iloc[-1]
                volumes = data['Volume'].mean()
                for sym in batch:
                    try:
                        price = float(closes[sym])
                        vol = float(volumes[sym])
                        if 1.0 <= price <= 20.0 and vol >= 500_000:
                            candidates.append(sym)
                            avg_volumes[sym] = vol
                            try:
                                profile = {"name": "", "sector": "", "market_cap": 0, "country": "", "exchange": ""}
                                if sym not in company_cache:
                                    r = requests.get(f"https://finnhub.io/api/v1/stock/profile2?symbol={sym}&token={FINNHUB_KEY}", timeout=5)
                                    d = r.json()
                                    profile = {
                                        "name": d.get("name", ""),
                                        "sector": d.get("finnhubIndustry", ""),
                                        "market_cap": int(d.get("marketCapitalization", 0) * 1_000_000) if d.get("marketCapitalization") else 0,
                                        "country": d.get("country", ""),
                                        "exchange": d.get("exchange", ""),
                                    }
                                    company_cache[sym] = profile
                                    time.sleep(0.1)
                                else:
                                    profile = company_cache[sym]
                                supabase.table("us_watchlist").upsert({
                                    "symbol": sym,
                                    "name": profile["name"],
                                    "sector": profile["sector"],
                                    "market_cap": profile["market_cap"],
                                    "country": profile["country"],
                                    "exchange": profile["exchange"],
                                    "avg_volume": int(vol),
                                    "last_price": round(price, 2),
                                    "updated_at": datetime.now(timezone.utc).isoformat()
                                }).execute()
                            except:
                                pass
                    except:
                        continue
            except:
                continue
            time.sleep(1)

        active_symbols = candidates
        print(f"✅ Watchlist yenilendi: {len(candidates)} hisse")
    except Exception as e:
        print(f"❌ Watchlist yenileme hatası: {e}")


def get_news(symbol, days=1):
    cache_key = f"{symbol}_{days}"
    if cache_key in news_cache:
        cached_time, cached_news = news_cache[cache_key]
        if time.time() - cached_time < 1800:
            return cached_news
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        from_date = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        r = requests.get(
            f"https://finnhub.io/api/v1/company-news?symbol={symbol}&from={from_date}&to={today}&token={FINNHUB_KEY}",
            timeout=5
        )
        news_list = r.json()
        headlines = [n.get("headline", "") for n in news_list[:3]] if isinstance(news_list, list) else []
        result = " | ".join(headlines) if headlines else ""
        news_cache[cache_key] = (time.time(), result)
        return result
    except:
        return ""


def get_analyst_rating(symbol):
    if symbol in analyst_cache:
        cached_time, cached_data = analyst_cache[symbol]
        if time.time() - cached_time < 3600:
            return cached_data
    try:
        r = requests.get(
            f"https://finnhub.io/api/v1/stock/recommendation?symbol={symbol}&token={FINNHUB_KEY}",
            timeout=5
        )
        data = r.json()
        if data and isinstance(data, list):
            latest = data[0]
            result = {
                "buy": latest.get("buy", 0) + latest.get("strongBuy", 0),
                "hold": latest.get("hold", 0),
                "sell": latest.get("sell", 0)
            }
            analyst_cache[symbol] = (time.time(), result)
            return result
        return None
    except:
        return None


def get_insider(symbol):
    try:
        r = requests.get(
            f"https://finnhub.io/api/v1/stock/insider-transactions?symbol={symbol}&token={FINNHUB_KEY}",
            timeout=5
        )
        data = r.json().get("data", [])
        purchases = [t for t in data[:10] if t.get("transactionCode") == "P-Purchase"]
        if len(purchases) >= 2:
            return f"{len(purchases)} insider alımı"
        if purchases:
            return f"{purchases[0].get('name', '')} bought {purchases[0].get('share', 0):,} shares"
        return ""
    except:
        return ""


def get_5day_trend(symbol):
    try:
        t = yf.Ticker(symbol)
        hist = t.history(period="10d").dropna()
        if len(hist) < 3:
            return None
        closes = hist['Close'].tolist()
        volumes = hist['Volume'].tolist()
        last5 = closes[-5:] if len(closes) >= 5 else closes
        trend = "up" if last5[-1] > last5[0] else "down"
        prev_change = ((closes[-1] - closes[-2]) / closes[-2]) * 100
        avg_vol = sum(volumes[:-1]) / len(volumes[:-1]) if volumes[:-1] else 0
        vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 0
        return {
            "trend": trend,
            "prev_change": round(prev_change, 2),
            "vol_ratio": round(vol_ratio, 2),
            "last_close": round(closes[-1], 2),
            "5d_change": round(((closes[-1] - closes[-5]) / closes[-5]) * 100, 2) if len(closes) >= 5 else 0
        }
    except:
        return None


def premarket_conviction(trend_data, news, analyst, insider):
    score = 0
    reasons = []

    if not trend_data:
        return "NORMAL", []

    if trend_data["trend"] == "down" and trend_data["5d_change"] <= -5:
        score += 2
        reasons.append(f"5g düşüş {trend_data['5d_change']}% — bounce adayı")

    if trend_data["prev_change"] >= 3:
        score += 2
        reasons.append(f"Dün +{trend_data['prev_change']}% güçlü kapanış")
    elif trend_data["prev_change"] >= 1:
        score += 1
        reasons.append(f"Dün +{trend_data['prev_change']}% pozitif kapanış")

    if trend_data["vol_ratio"] >= 2:
        score += 3
        reasons.append(f"Hacim {trend_data['vol_ratio']}x — kurumsal ilgi")
    elif trend_data["vol_ratio"] >= 1.5:
        score += 2
        reasons.append(f"Hacim {trend_data['vol_ratio']}x artışı")

    if news:
        score += 3
        reasons.append(f"Haber: {news[:60]}")

    if analyst:
        buy = analyst.get("buy", 0)
        sell = analyst.get("sell", 0)
        if buy >= 5 and sell == 0:
            score += 3
            reasons.append(f"Analist: {buy} AL 0 SAT")
        elif buy > sell:
            score += 1
            reasons.append(f"Analist: {buy} AL {sell} SAT")

    if insider:
        score += 3
        reasons.append(f"Insider: {insider}")

    if score >= 8:
        return "CRITICAL", reasons
    elif score >= 6:
        return "HIGH", reasons
    elif score >= 4:
        return "MEDIUM", reasons
    return "NORMAL", reasons


def get_ai_explanation(symbol, company_name, sector, trend_data, news, insider, analyst, conviction, reasons, is_premarket_signal=False):
    try:
        analyst_str = f"Buy={analyst['buy']} Hold={analyst['hold']} Sell={analyst['sell']}" if analyst else "N/A"

        if is_premarket_signal and trend_data:
            prompt = f"""Financial analyst. Pre-market signal. 3 levels.

Stock: {symbol} ({company_name}) | Sector: {sector}
Last Close: ${trend_data['last_close']} | Yesterday: {trend_data['prev_change']:+}% | 5d: {trend_data['5d_change']:+}%
Volume Ratio: {trend_data['vol_ratio']}x | Conviction: {conviction}
Reasons: {', '.join(reasons)}
News: {news if news else 'None'}
Insider: {insider if insider else 'None'}
Analyst: {analyst_str}

===BEGINNER===
[1-2 sentences, what to expect at open]
===INTERMEDIATE===
[technical setup, key levels]
===PRO===
[catalyst, risk/reward, entry strategy]"""
        else:
            prompt = f"""Financial analyst. Live market signal. 3 levels.

Stock: {symbol} ({company_name}) | Sector: {sector}
Conviction: {conviction}
News: {news if news else 'None'}
Insider: {insider if insider else 'None'}

===BEGINNER===
[1-2 sentences plain language]
===INTERMEDIATE===
[technical analysis]
===PRO===
[professional analysis, risk/reward]"""

        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"},
            json={
                "model": "llama-3.1-8b-instant",
                "messages": [
                    {"role": "system", "content": "Financial analyst. Use exact format. Never change ===BEGINNER===, ===INTERMEDIATE===, ===PRO=== headers."},
                    {"role": "user", "content": prompt}
                ],
                "max_tokens": 500,
                "temperature": 0.3
            },
            timeout=15
        )
        resp = r.json()
        if "choices" not in resp:
            return ""
        return resp["choices"][0]["message"]["content"]
    except:
        return ""


def parse_ai_levels(ai_text):
    acemi, usta, pro = "", "", ""
    try:
        if "===BEGINNER===" in ai_text:
            acemi = ai_text.split("===BEGINNER===")[1].split("===INTERMEDIATE===")[0].strip()
        if "===INTERMEDIATE===" in ai_text:
            usta = ai_text.split("===INTERMEDIATE===")[1].split("===PRO===")[0].strip()
        if "===PRO===" in ai_text:
            pro = ai_text.split("===PRO===")[1].strip()
    except:
        pass
    return acemi, usta, pro


def get_last_signal_time(symbol):
    try:
        r = supabase.table("us_signals") \
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


def update_live_price(symbol, price):
    now = time.time()
    if symbol in last_price_update and now - last_price_update[symbol] < 60:
        return
    try:
        supabase.table("us_watchlist").update({
            "last_price": round(price, 2),
            "updated_at": datetime.now(timezone.utc).isoformat()
        }).eq("symbol", symbol).execute()
        last_price_update[symbol] = now
    except:
        pass


class VolumeTracker:
    def __init__(self):
        self.trades = {}
        self.daily_opens = {}

    def update(self, symbol, price, volume):
        now = time.time()
        if symbol not in self.trades:
            self.trades[symbol] = []
            self.daily_opens[symbol] = price
        self.trades[symbol].append((price, volume, now))
        cutoff = now - 300
        self.trades[symbol] = [(p, v, t) for p, v, t in self.trades[symbol] if t > cutoff]

    def get_volume_ratio(self, symbol, avg_volume):
        if avg_volume <= 0:
            return 0
        recent_volume = sum(v for _, v, _ in self.trades.get(symbol, []))
        expected_5min = avg_volume / 78
        return recent_volume / expected_5min if expected_5min > 0 else 0

    def get_price_change(self, symbol, current_price):
        open_price = self.daily_opens.get(symbol, current_price)
        if not open_price:
            return 0
        return ((current_price - open_price) / open_price) * 100


tracker = VolumeTracker()


def bot_should_buy(price_change, volume_ratio, conviction):
    if price_change < 0:
        return False
    if conviction in ["CRITICAL", "HIGH"]:
        return True
    if price_change >= 3 and volume_ratio >= 3:
        return True
    if conviction == "MEDIUM" and price_change >= 1 and volume_ratio >= 2:
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
            "user_id": user_id, "symbol": symbol, "market": "US",
            "signal_id": signal_id, "buy_price": price,
            "buy_date": datetime.now(timezone.utc).isoformat(),
            "quantity": round(quantity, 4), "status": "open",
            "created_at": datetime.now(timezone.utc).isoformat()
        }).execute()

        supabase.table("demo_portfolios").update({
            "balance": round(balance - invest, 2)
        }).eq("user_id", user_id).execute()

        print(f"✅ BOT ALIM: {user_id} → {symbol} {quantity:.4f} lot @ ${price:.2f}")
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

        print(f"✅ BOT SATIŞ: {trade['user_id']} → {trade['symbol']} | K/Z: ${profit_loss:.2f}")
    except Exception as e:
        print(f"❌ Bot satış hatası: {e}")


def bot_check_open_positions():
    try:
        trades = supabase.table("demo_trades").select("*").eq("status", "open").eq("market", "US").execute()
        if not trades.data:
            return
        print(f"🔍 {len(trades.data)} açık US pozisyon kontrol ediliyor...")
        for trade in trades.data:
            try:
                t = yf.Ticker(trade["symbol"])
                hist = t.history(period="1d", interval="1m").dropna()
                if hist.empty:
                    continue
                current_price = float(hist["Close"].iloc[-1])
                if bot_should_sell(trade["buy_price"], current_price):
                    bot_sell(trade, current_price)
            except:
                continue
            time.sleep(0.3)
    except Exception as e:
        print(f"❌ Pozisyon kontrol hatası: {e}")


def bot_process_signal(symbol, price, price_change, volume_ratio, conviction, signal_id):
    try:
        if not bot_should_buy(price_change, volume_ratio, conviction):
            print(f"🤖 Bot {symbol}: ALMA")
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


def run_premarket_scan():
    print("\n🦅 KARTAL GÖZÜ — PRE-MARKET TARAMA BASLIYOR")
    now_utc = datetime.now(timezone.utc)
    minutes_to_open = int((13.5 - now_utc.hour - now_utc.minute / 60) * 60)
    print(f"⏰ {datetime.now().strftime('%H:%M')} — Açılışa {minutes_to_open} dakika var")

    candidates = list(avg_volumes.keys())
    if not candidates:
        print("⚠️ Watchlist boş")
        return

    signals_found = 0
    print(f"🔍 {len(candidates)} hisse taranıyor...")

    for symbol in candidates:
        try:
            if symbol in premarket_signal_cache:
                continue

            trend = get_5day_trend(symbol)
            if not trend:
                continue

            if not (1.0 <= trend["last_close"] <= 20.0):
                continue

            news = get_news(symbol, days=2)
            analyst = get_analyst_rating(symbol)
            insider = get_insider(symbol)

            conviction, reasons = premarket_conviction(trend, news, analyst, insider)

            if conviction == "NORMAL":
                continue

            company = company_cache.get(symbol, {"name": "", "sector": ""})
            company_name = company.get("name", "")
            sector = company.get("sector", "")

            print(f"\n🎯 {symbol} ({company_name}) | {conviction}")
            for r in reasons:
                print(f"   → {r}")

            ai_text = get_ai_explanation(symbol, company_name, sector, trend, news, insider, analyst, conviction, reasons, is_premarket_signal=True)
            acemi, usta, pro = parse_ai_levels(ai_text)

            emoji = "🔥" if conviction == "CRITICAL" else "⚡" if conviction == "HIGH" else "👁️"
            description = f"{emoji} PRE-MARKET | {symbol}"
            if company_name:
                description += f" ({company_name})"
            description += f" | ${trend['last_close']} | Dün: {trend['prev_change']:+}% | 5g: {trend['5d_change']:+}%"
            if news:
                description += f" | 📰 {news[:80]}"

            result = supabase.table("us_signals").insert({
                "symbol": symbol,
                "signal_type": "premarket",
                "value": trend["prev_change"],
                "description": description,
                "acemi_explanation": acemi,
                "usta_explanation": usta,
                "pro_explanation": pro,
                "price": trend["last_close"],
                "volume_ratio": trend["vol_ratio"],
                "created_at": datetime.now(timezone.utc).isoformat()
            }).execute()

            signal_id = result.data[0].get("id") if result.data else None
            premarket_signal_cache[symbol] = time.time()
            signals_found += 1

            push_body = f"${trend['last_close']} | Dün {trend['prev_change']:+}% | {conviction}"
            if company_name:
                push_body = f"{company_name} — {push_body}"

            send_push_notification(
                title=f"{emoji} {symbol} — Acilis Oncesi Sinyal",
                body=push_body,
                market="US",
                signal_id=signal_id
            )

            print(f"✅ KAYDEDİLDİ [{conviction}]: {description}")
            time.sleep(0.5)

        except Exception as e:
            print(f"❌ {symbol}: {e}")
            continue

    print(f"\n✅ Pre-market tarama bitti. {signals_found} sinyal.")


def process_live_signal(symbol, signal_type, price, price_change, volume_ratio):
    if not is_market_open():
        return
    if price < 1.0 or price > 20.0:
        return
    if price_change < 0:
        return

    now = time.time()
    if symbol in signal_cache and now - signal_cache[symbol] < 3600:
        return
    last_time = get_last_signal_time(symbol)
    if now - last_time < 3600:
        signal_cache[symbol] = last_time
        return

    news = get_news(symbol)
    insider = get_insider(symbol)
    has_news = bool(news)

    score = 0
    if abs(price_change) >= 10:
        score += 3
    elif abs(price_change) >= 5:
        score += 2
    elif abs(price_change) >= 3:
        score += 1
    if volume_ratio >= 10:
        score += 3
    elif volume_ratio >= 5:
        score += 2
    elif volume_ratio >= 2:
        score += 1
    if has_news:
        score += 3
    if insider:
        score += 2

    conviction = "HIGH" if score >= 7 else "MEDIUM" if score >= 4 else "NORMAL"

    if conviction == "NORMAL" and not has_news:
        return

    company = company_cache.get(symbol, {"name": "", "sector": ""})
    company_name = company.get("name", "")
    sector = company.get("sector", "")

    print(f"📊 {symbol} ({company_name}) | {conviction} | {price_change:+.1f}% | {volume_ratio:.1f}x")

    ai_text = get_ai_explanation(symbol, company_name, sector, None, news, insider, None, conviction, [], is_premarket_signal=False)
    acemi, usta, pro = parse_ai_levels(ai_text)

    emoji = "🔥" if conviction == "HIGH" else "⚡"
    description = f"{emoji} {symbol}"
    if company_name:
        description += f" ({company_name})"
    description += f" | ${price:.2f} | {price_change:+.1f}% | Vol: {volume_ratio:.1f}x | {conviction}"
    if news:
        description += f" | 📰 {news[:80]}"
    if insider:
        description += f" | 🐋 {insider}"

    try:
        result = supabase.table("us_signals").insert({
            "symbol": symbol,
            "signal_type": signal_type,
            "value": round(price_change, 2),
            "description": description,
            "acemi_explanation": acemi,
            "usta_explanation": usta,
            "pro_explanation": pro,
            "price": price,
            "volume_ratio": round(volume_ratio, 2),
            "created_at": datetime.now(timezone.utc).isoformat()
        }).execute()

        signal_cache[symbol] = now
        signal_id = result.data[0].get("id") if result.data else None
        print(f"✅ KAYDEDİLDİ [{conviction}]: {description}")

        bot_process_signal(symbol, price, price_change, volume_ratio, conviction, signal_id)

        push_body = f"${price:.2f} | {price_change:+.1f}% | Vol: {volume_ratio:.1f}x"
        if company_name:
            push_body = f"{company_name} — {push_body}"
        send_push_notification(
            title=f"{emoji} {symbol} — {conviction}",
            body=push_body,
            market="US",
            signal_id=signal_id
        )
    except Exception as e:
        print(f"❌ Kayıt hatası: {e}")


def on_message(ws, message):
    try:
        data = json.loads(message)
        if data.get("type") != "trade":
            return
        for trade in data.get("data", []):
            symbol = trade.get("s")
            price = float(trade.get("p", 0))
            volume = float(trade.get("v", 0))
            if not symbol or not price or price < 1.0 or price > 20.0:
                continue
            tracker.update(symbol, price, volume)
            if symbol in avg_volumes:
                update_live_price(symbol, price)
            avg_vol = avg_volumes.get(symbol, 0)
            if avg_vol == 0:
                continue
            volume_ratio = tracker.get_volume_ratio(symbol, avg_vol)
            price_change = tracker.get_price_change(symbol, price)
            if volume_ratio >= 2 and price_change >= 3:
                process_live_signal(symbol, "momentum", price, price_change, volume_ratio)
            elif volume_ratio >= 5 and price_change > 0:
                process_live_signal(symbol, "volume_spike", price, price_change, volume_ratio)
    except Exception as e:
        print(f"Mesaj hatası: {e}")


def on_open(ws):
    print(f"✅ WebSocket bağlandı. {len(active_symbols)} hisse izleniyor...")
    for symbol in active_symbols:
        ws.send(json.dumps({"type": "subscribe", "symbol": symbol}))


def on_error(ws, error):
    print(f"❌ WebSocket hatası: {error}")


def on_close(ws, close_status_code, close_msg):
    print("🔌 Bağlantı kapandı, 5sn sonra yeniden...")
    time.sleep(5)
    connect_websocket()


def connect_websocket():
    ws = websocket.WebSocketApp(
        f"wss://ws.finnhub.io?token={FINNHUB_KEY}",
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )
    ws.run_forever()


def start():
    global active_symbols

    print("🚀 Atlas US Kartal Gözü baslatildi...")
    print(f"⏰ {datetime.now(timezone.utc).strftime('%H:%M UTC')}")

    # Supabase'den hızlı yükle
    active_symbols = load_watchlist_from_db()

    if not active_symbols:
        print("⚠️ DB boş, yfinance ile yükleniyor...")
        refresh_watchlist_background()

    # Cache yükle
    print("🔄 Sinyal cache yükleniyor...")
    try:
        since = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        r = supabase.table("us_signals").select("symbol, created_at").gte("created_at", since).execute()
        for row in r.data:
            sym = row["symbol"]
            dt = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
            signal_cache[sym] = dt.timestamp()
        print(f"✅ {len(signal_cache)} sinyal cache'e yüklendi")
    except Exception as e:
        print(f"⚠️ Cache yükleme hatası: {e}")

    premarket_done = False
    last_watchlist_refresh = time.time()

    while True:
        try:
            now_utc = datetime.now(timezone.utc)
            hour = now_utc.hour
            minute = now_utc.minute

            # Gece 02:00 UTC'de watchlist yenile
            if hour == 2 and minute < 5 and time.time() - last_watchlist_refresh > 3600:
                print("🌙 Gece watchlist yenileniyor...")
                refresh_watchlist_background()
                last_watchlist_refresh = time.time()
                premarket_signal_cache.clear()
                premarket_done = False

            if is_premarket() and not premarket_done:
                run_premarket_scan()
                premarket_done = True
                print("💤 Pre-market bitti. Açılış bekleniyor...")
                time.sleep(300)

            elif is_market_open():
                premarket_done = False
                print(f"📡 WebSocket bağlanıyor... ({len(active_symbols)} hisse)")
                connect_websocket()

            else:
                if hour == 21 and minute < 5:
                    bot_check_open_positions()
                    premarket_done = False
                print(f"💤 Borsa kapalı. {now_utc.strftime('%H:%M UTC')}")
                time.sleep(300)

        except Exception as e:
            print(f"❌ Ana döngü hatası: {e}")
            time.sleep(60)


if __name__ == "__main__":
    start()
