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
        print("✅ Firebase Admin baslatildi")
except Exception as e:
    print(f"⚠️ Firebase baslatma hatası: {e}")


def send_push_notification(title, body, market="US", signal_id=None):
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


def is_market_open():
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5:
        return False
    open_t = now.replace(hour=13, minute=30, second=0, microsecond=0)
    close_t = now.replace(hour=20, minute=0, second=0, microsecond=0)
    return open_t <= now <= close_t


def load_watchlist_from_db():
    global active_symbols
    try:
        print("📋 Watchlist Supabase'den yukleniyor...")
        result = supabase.table("us_watchlist").select("symbol, avg_volume, last_price, name, sector").execute()
        if not result.data:
            return []
        symbols = []
        for row in result.data:
            sym = row["symbol"]
            vol = row.get("avg_volume", 0) or 0
            if vol > 0:
                avg_volumes[sym] = vol
                symbols.append(sym)
                company_cache[sym] = {
                    "name": row.get("name", ""),
                    "sector": row.get("sector", ""),
                    "market_cap": 0, "country": "", "exchange": ""
                }
        print(f"✅ {len(symbols)} hisse yuklendi")
        return symbols
    except Exception as e:
        print(f"❌ Watchlist yukleme hatası: {e}")
        return []


def refresh_watchlist_background():
    global active_symbols
    try:
        print("🔄 Watchlist yenileniyor...")
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

        all_symbols = list(set(nasdaq + nyse))
        candidates = []

        for i in range(0, len(all_symbols), 200):
            batch = all_symbols[i:i+200]
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
                            profile = {"name": "", "sector": "", "market_cap": 0, "country": "", "exchange": ""}
                            if sym not in company_cache:
                                try:
                                    rp = requests.get(f"https://finnhub.io/api/v1/stock/profile2?symbol={sym}&token={FINNHUB_KEY}", timeout=5)
                                    d = rp.json()
                                    profile = {
                                        "name": d.get("name", ""),
                                        "sector": d.get("finnhubIndustry", ""),
                                        "market_cap": int(d.get("marketCapitalization", 0) * 1_000_000) if d.get("marketCapitalization") else 0,
                                        "country": d.get("country", ""),
                                        "exchange": d.get("exchange", ""),
                                    }
                                    company_cache[sym] = profile
                                    time.sleep(0.1)
                                except:
                                    pass
                            else:
                                profile = company_cache[sym]
                            try:
                                supabase.table("us_watchlist").upsert({
                                    "symbol": sym, "name": profile["name"],
                                    "sector": profile["sector"], "market_cap": profile["market_cap"],
                                    "country": profile["country"], "exchange": profile["exchange"],
                                    "avg_volume": int(vol), "last_price": round(price, 2),
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


def get_news_catalyst(symbol, company_name=""):
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        two_days_ago = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d")
        r = requests.get(
            f"https://finnhub.io/api/v1/company-news?symbol={symbol}&from={two_days_ago}&to={today}&token={FINNHUB_KEY}",
            timeout=5
        )
        news_list = r.json()
        if not isinstance(news_list, list) or not news_list:
            return None

        negative_keywords = [
            "sell", "reasons to sell", "short", "downgrade",
            "sinks", "drops", "falls", "decline", "warning",
            "bearish", "avoid", "overvalued", "cut",
        ]

        catalyst_keywords = [
            "earnings", "revenue", "profit", "beat", "exceed", "guidance",
            "upgrade", "buy", "outperform", "target raised", "price target",
            "fda", "approval", "approved", "patent",
            "merger", "acquisition", "buyout", "deal",
            "contract", "partnership", "agreement",
            "dividend", "buyback", "record",
            "surging", "soaring", "jumps", "rally",
        ]

        symbol_lower = symbol.lower()
        company_lower = company_name.lower() if company_name else ""

        for news in news_list[:10]:
            headline = news.get("headline", "").lower()
            summary = news.get("summary", "").lower()

            # Haber bu hisseyle alakalı mı — sembol veya şirket adı geçmeli
            is_relevant = (
                symbol_lower in headline or
                symbol_lower in summary or
                (company_lower and len(company_lower) > 4 and company_lower.split()[0] in headline)
            )
            if not is_relevant:
                continue

            # Negatif haber = geç
            if any(kw in headline for kw in negative_keywords):
                continue

            # Pozitif kataliz
            for kw in catalyst_keywords:
                if kw in headline or kw in summary:
                    return {
                        "headline": news.get("headline", ""),
                        "keyword": kw,
                        "time": news.get("datetime", 0)
                    }

        return None
    except:
        return None


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


def get_insider_recent(symbol):
    try:
        r = requests.get(
            f"https://finnhub.io/api/v1/stock/insider-transactions?symbol={symbol}&token={FINNHUB_KEY}",
            timeout=5
        )
        data = r.json().get("data", [])
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
        recent_purchases = [
            t for t in data
            if t.get("transactionCode") == "P-Purchase"
            and t.get("transactionDate", "") >= cutoff
        ]
        if len(recent_purchases) >= 2:
            total = sum(p.get("share", 0) * p.get("transactionPrice", 0) for p in recent_purchases)
            return f"{len(recent_purchases)} insider alımı (${total:,.0f})"
        if recent_purchases:
            p = recent_purchases[0]
            return f"{p.get('name', 'Insider')} — {p.get('share', 0):,} hisse"
        return None
    except:
        return None


def get_earnings_today():
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        r = requests.get(
            f"https://finnhub.io/api/v1/calendar/earnings?from={today}&to={today}&token={FINNHUB_KEY}",
            timeout=10
        )
        data = r.json()
        earnings = data.get("earningsCalendar", [])
        symbols = []
        for e in earnings:
            sym = e.get("symbol", "")
            if sym:
                symbols.append({
                    "symbol": sym,
                    "eps_estimate": e.get("epsEstimate"),
                    "eps_actual": e.get("epsActual"),
                    "hour": e.get("hour", "")
                })
        print(f"📅 Bugün {len(symbols)} şirket earnings açıklıyor")
        return symbols
    except Exception as e:
        print(f"⚠️ Earnings hatası: {e}")
        return []


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


def premarket_conviction(trend_data, catalyst, earnings, analyst, insider):
    score = 0
    reasons = []

    if not trend_data:
        return "NORMAL", [], 0

    has_catalyst = bool(catalyst or earnings or analyst or insider)
    if not has_catalyst:
        return "NORMAL", [], 0

    if earnings:
        eps_est = earnings.get("eps_estimate")
        eps_act = earnings.get("eps_actual")
        if eps_est and eps_act and eps_act > eps_est:
            beat_pct = ((eps_act - eps_est) / abs(eps_est)) * 100 if eps_est != 0 else 0
            score += 5
            reasons.append(f"Earnings BEAT %{beat_pct:.0f} ({eps_act} vs {eps_est})")
        else:
            score += 2
            reasons.append("Earnings açıklaması bugün")

    if catalyst:
        score += 3
        reasons.append(f"Kataliz [{catalyst['keyword']}]: {catalyst['headline'][:60]}")

    if analyst:
        buy = analyst.get("buy", 0)
        sell = analyst.get("sell", 0)
        if buy >= 5 and sell == 0:
            score += 3
            reasons.append(f"Analist: {buy} AL 0 SAT")
        elif buy > sell * 2:
            score += 2
            reasons.append(f"Analist: {buy} AL {sell} SAT")

    if insider:
        score += 3
        reasons.append(f"Insider: {insider}")

    if trend_data["vol_ratio"] >= 2:
        score += 3
        reasons.append(f"Hacim {trend_data['vol_ratio']}x — kurumsal ilgi")
    elif trend_data["vol_ratio"] >= 1.5:
        score += 2
        reasons.append(f"Hacim {trend_data['vol_ratio']}x artışı")

    if trend_data["prev_change"] >= 4:
        score += 2
        reasons.append(f"Dün +{trend_data['prev_change']}% güçlü kapanış")
    elif trend_data["prev_change"] >= 2:
        score += 1
        reasons.append(f"Dün +{trend_data['prev_change']}% pozitif")

    if score >= 10:
        conviction = "CRITICAL"
    elif score >= 7:
        conviction = "HIGH"
    elif score >= 4:
        conviction = "MEDIUM"
    else:
        conviction = "NORMAL"

    return conviction, reasons, score


def get_ai_explanation(symbol, company_name, sector, trend_data, catalyst, earnings, analyst, insider, conviction, reasons, is_premarket_signal=False):
    try:
        analyst_str = f"Buy={analyst['buy']} Hold={analyst['hold']} Sell={analyst['sell']}" if analyst else "N/A"

        if is_premarket_signal and trend_data:
            prompt = f"""Financial analyst. Pre-market signal. 3 levels. Be specific.

Stock: {symbol} ({company_name}) | Sector: {sector}
Last Close: ${trend_data['last_close']} | Yesterday: {trend_data['prev_change']:+}% | 5d: {trend_data['5d_change']:+}%
Volume Ratio: {trend_data['vol_ratio']}x | Conviction: {conviction}
Reasons: {' | '.join(reasons)}
Catalyst: {catalyst['headline'] if catalyst else 'None'}
Insider: {insider if insider else 'None'}
Analyst: {analyst_str}

===BEGINNER===
[Max 2 sentences. What to expect at open. Plain language.]
===INTERMEDIATE===
[Max 3 sentences. Technical setup + catalyst + key level.]
===PRO===
[Max 4 sentences. Institutional probability, catalyst strength, risk/reward, entry/stop/target in dollars.]"""
        else:
            prompt = f"""Financial analyst. Live signal. 3 levels. Be specific.

Stock: {symbol} ({company_name}) | Sector: {sector}
Conviction: {conviction}
Catalyst: {catalyst['headline'] if catalyst else 'None'}
Insider: {insider if insider else 'None'}

===BEGINNER===
[Max 2 sentences. Plain language.]
===INTERMEDIATE===
[Max 3 sentences. Technical analysis.]
===PRO===
[Max 4 sentences. Risk/reward, entry/stop/target in dollars.]"""

        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"},
            json={
                "model": "llama-3.1-8b-instant",
                "messages": [
                    {"role": "system", "content": "Financial analyst. Use exact format. Never change ===BEGINNER===, ===INTERMEDIATE===, ===PRO=== headers. Be specific."},
                    {"role": "user", "content": prompt}
                ],
                "max_tokens": 500,
                "temperature": 0.2
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
        r = supabase.table("us_signals").select("created_at").eq("symbol", symbol).order("created_at", ascending=False).limit(1).execute()
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
                print(f"⚠️ {user_id} aylik limit doldu")
                return False
            open_trades = supabase.table("demo_trades").select("id").eq("user_id", user_id).eq("status", "open").execute()
            if len(open_trades.data) >= 1:
                print(f"⚠️ {user_id} acik pozisyon var")
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
        print(f"✅ BOT ALIM: {user_id} → {symbol} @ ${price:.2f}")
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
        print(f"✅ BOT SATIŞ: {trade['symbol']} | K/Z: ${profit_loss:.2f}")
    except Exception as e:
        print(f"❌ Bot satış hatası: {e}")


def bot_check_open_positions():
    try:
        trades = supabase.table("demo_trades").select("*").eq("status", "open").eq("market", "US").execute()
        if not trades.data:
            return
        print(f"🔍 {len(trades.data)} acik US pozisyon kontrol ediliyor...")
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
        print(f"❌ Bot sinyal hatası: {e}")


def run_premarket_scan():
    """Gece analizi — sinyalleri DB'ye kaydet, push GÖNDERME"""
    print("\n" + "="*50)
    print("🦅 KARTAL GÖZÜ — GECE ANALİZİ")
    print(f"⏰ {datetime.now(timezone.utc).strftime('%H:%M UTC')} — Sabah 15:30 TR'de sinyaller gönderilecek")
    print("="*50)

    print("\n📅 Earnings takvimi kontrol ediliyor...")
    earnings_today = get_earnings_today()
    earnings_map = {e["symbol"]: e for e in earnings_today}

    all_targets = set(list(avg_volumes.keys()) + list(earnings_map.keys()))
    print(f"🔍 {len(all_targets)} hisse taranıyor...")

    signals = []

    for symbol in all_targets:
        try:
            if symbol in premarket_signal_cache:
                continue

            company = company_cache.get(symbol, {"name": "", "sector": ""})
            company_name = company.get("name", "")
            sector = company.get("sector", "")

            earnings = earnings_map.get(symbol)
            catalyst = get_news_catalyst(symbol, company_name)
            time.sleep(0.05)

            if not catalyst and not earnings:
                continue

            trend = get_5day_trend(symbol)
            if not trend:
                continue

            if not earnings and not (1.0 <= trend["last_close"] <= 20.0):
                continue

            analyst = get_analyst_rating(symbol)
            insider = get_insider_recent(symbol)

            conviction, reasons, score = premarket_conviction(trend, catalyst, earnings, analyst, insider)

            if conviction == "NORMAL":
                continue

            signals.append({
                "symbol": symbol,
                "company_name": company_name,
                "sector": sector,
                "trend": trend,
                "catalyst": catalyst,
                "earnings": earnings,
                "analyst": analyst,
                "insider": insider,
                "conviction": conviction,
                "reasons": reasons,
                "score": score
            })

            print(f"  🎯 {symbol} ({company_name}) | {conviction} | Score: {score}")
            for r in reasons:
                print(f"     → {r}")

            time.sleep(0.3)

        except Exception as e:
            print(f"❌ {symbol}: {e}")
            continue

    signals.sort(key=lambda x: x["score"], reverse=True)
    top5 = signals[:5]

    print(f"\n✅ {len(signals)} aday — en iyi {len(top5)} sinyal DB'ye kaydediliyor...")

    for sig in top5:
        try:
            symbol = sig["symbol"]
            trend = sig["trend"]
            conviction = sig["conviction"]

            ai_text = get_ai_explanation(
                symbol, sig["company_name"], sig["sector"],
                trend, sig["catalyst"], sig["earnings"],
                sig["analyst"], sig["insider"],
                conviction, sig["reasons"], is_premarket_signal=True
            )
            acemi, usta, pro = parse_ai_levels(ai_text)

            emoji = "🔥" if conviction == "CRITICAL" else "⚡" if conviction == "HIGH" else "👁️"
            description = f"{emoji} PRE-MARKET | {symbol}"
            if sig["company_name"]:
                description += f" ({sig['company_name']})"
            description += f" | ${trend['last_close']} | Dun: {trend['prev_change']:+}%"
            if sig["catalyst"]:
                description += f" | 📰 {sig['catalyst']['headline'][:60]}"
            if sig["earnings"]:
                description += " | 📊 Earnings"

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
            print(f"✅ DB'ye kaydedildi: {description}")

        except Exception as e:
            print(f"❌ Sinyal kayıt hatası {sig['symbol']}: {e}")

    print(f"\n🌙 Gece analizi tamamlandı. {len(top5)} sinyal hazır. Sabah 15:30 TR'de gönderilecek.")


def send_morning_signals():
    """Gece hazırlanan US sinyallerini 15:30 TR'de push ile gönder"""
    try:
        since = (datetime.now(timezone.utc) - timedelta(hours=16)).isoformat()
        r = supabase.table("us_signals") \
            .select("*") \
            .gte("created_at", since) \
            .eq("signal_type", "premarket") \
            .order("created_at", ascending=False) \
            .limit(5) \
            .execute()

        if not r.data:
            print("⚠️ Sabah için US sinyali bulunamadı")
            return

        print(f"📱 {len(r.data)} US sabah sinyali gönderiliyor...")

        for signal in r.data:
            symbol = signal["symbol"]
            price = signal.get("price", 0)
            value = signal.get("value", 0)
            volume_ratio = signal.get("volume_ratio", 0)
            signal_id = signal["id"]
            description = signal.get("description", "")

            company_name = ""
            if "(" in description and ")" in description:
                try:
                    company_name = description.split("(")[1].split(")")[0]
                except:
                    pass

            emoji = "⚡"
            if "🔥" in description:
                emoji = "🔥"
            elif "👁️" in description:
                emoji = "👁️"

            push_body = f"${price:.2f} | {value:+.1f}% | Vol: {volume_ratio:.1f}x"
            if company_name:
                push_body = f"{company_name} — {push_body}"

            send_push_notification(
                title=f"{emoji} {symbol} — Açılış Öncesi Sinyal",
                body=push_body,
                market="US",
                signal_id=signal_id
            )
            time.sleep(0.5)

        print("✅ US sabah sinyalleri gönderildi.")
    except Exception as e:
        print(f"❌ US sabah sinyal gönderme hatası: {e}")


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

    company = company_cache.get(symbol, {"name": "", "sector": ""})
    company_name = company.get("name", "")

    catalyst = get_news_catalyst(symbol, company_name)
    insider = get_insider_recent(symbol)
    has_catalyst = bool(catalyst or insider)

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
    if catalyst:
        score += 3
    if insider:
        score += 2

    conviction = "HIGH" if score >= 7 else "MEDIUM" if score >= 4 else "NORMAL"

    if conviction == "NORMAL" and not has_catalyst:
        return

    print(f"📊 CANLI: {symbol} ({company_name}) | {conviction} | {price_change:+.1f}% | {volume_ratio:.1f}x")

    emoji = "🔥" if conviction == "HIGH" else "⚡"
    description = f"{emoji} {symbol}"
    if company_name:
        description += f" ({company_name})"
    description += f" | ${price:.2f} | {price_change:+.1f}% | Vol: {volume_ratio:.1f}x | {conviction}"
    if catalyst:
        description += f" | 📰 {catalyst['headline'][:60]}"
    if insider:
        description += f" | 🐋 {insider}"

    try:
        result = supabase.table("us_signals").insert({
            "symbol": symbol,
            "signal_type": signal_type,
            "value": round(price_change, 2),
            "description": description,
            "acemi_explanation": "",
            "usta_explanation": "",
            "pro_explanation": "",
            "price": price,
            "volume_ratio": round(volume_ratio, 2),
            "created_at": datetime.now(timezone.utc).isoformat()
        }).execute()

        signal_cache[symbol] = now
        signal_id = result.data[0].get("id") if result.data else None
        print(f"✅ KAYDEDİLDİ: {description}")

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
        print(f"WS mesaj hatası: {e}")


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

    print("🚀 Atlas US Kartal Gözü v2 baslatildi...")
    print(f"⏰ {datetime.now(timezone.utc).strftime('%H:%M UTC')}")

    active_symbols = load_watchlist_from_db()
    if not active_symbols:
        print("⚠️ DB bos, yfinance ile yukleniyor...")
        refresh_watchlist_background()

    print("🔄 Sinyal cache yukleniyor...")
    try:
        since = (datetime.now(timezone.utc) - timedelta(hours=16)).isoformat()
        r = supabase.table("us_signals").select("symbol, created_at").gte("created_at", since).execute()
        for row in r.data:
            sym = row["symbol"]
            dt = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
            signal_cache[sym] = dt.timestamp()
        print(f"✅ {len(signal_cache)} sinyal cache'e yuklendi")
    except Exception as e:
        print(f"⚠️ Cache yukleme hatası: {e}")

    last_watchlist_refresh = time.time()
    night_scan_done = False
    morning_signals_sent = False

    while True:
        try:
            now_utc = datetime.now(timezone.utc)
            hour = now_utc.hour
            minute = now_utc.minute

            # ============================================================
            # GECE WATCHLIST YENİLE: 02:00 UTC (05:00 TR)
            # ============================================================
            if hour == 2 and minute < 5 and time.time() - last_watchlist_refresh > 3600:
                print("🌙 Gece watchlist yenileniyor...")
                refresh_watchlist_background()
                last_watchlist_refresh = time.time()
                premarket_signal_cache.clear()
                night_scan_done = False
                morning_signals_sent = False

            # ============================================================
            # GECE MODU: 20:00-12:29 UTC (23:00-15:29 TR)
            # Borsa kapandıktan sonra analiz yap, push GÖNDERME
            # ============================================================
            if hour >= 20 or hour < 12:
                if not night_scan_done:
                    print(f"\n🌙 US GECE MOTORU başlıyor... {now_utc.strftime('%H:%M UTC')}")
                    premarket_signal_cache.clear()
                    run_premarket_scan()
                    night_scan_done = True
                    morning_signals_sent = False
                else:
                    print(f"💤 US gece bekleniyor... {now_utc.strftime('%H:%M UTC')}")
                time.sleep(600)

            # ============================================================
            # SABAH SİNYAL GÖNDERME: 12:30-12:59 UTC (15:30-15:59 TR)
            # Borsa açılmadan 1 saat önce push gönder
            # ============================================================
            elif hour == 12 and minute >= 30 and not morning_signals_sent:
                print(f"\n🦅 US SABAH SİNYALLERİ — {now_utc.strftime('%H:%M UTC')} (15:30 TR)")
                send_morning_signals()
                morning_signals_sent = True
                night_scan_done = False
                print("✅ US sabah sinyalleri gönderildi. Borsa açılışı bekleniyor...")
                time.sleep(120)

            # ============================================================
            # BORSA AÇIK: 13:30-20:00 UTC (16:30-23:00 TR)
            # WebSocket ile canlı takip
            # ============================================================
            elif is_market_open():
                night_scan_done = False
                morning_signals_sent = False
                print(f"📡 US WebSocket bağlanıyor... ({len(active_symbols)} hisse)")
                connect_websocket()

            # ============================================================
            # ARA DÖNEM: 13:00-13:29 UTC (16:00-16:29 TR)
            # ============================================================
            else:
                if hour == 20 and minute < 5:
                    bot_check_open_positions()
                print(f"💤 US bekleniyor. {now_utc.strftime('%H:%M UTC')}")
                time.sleep(300)

        except Exception as e:
            print(f"❌ Ana döngü hatası: {e}")
            time.sleep(60)


if __name__ == "__main__":
    start()
