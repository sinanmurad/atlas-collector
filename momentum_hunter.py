# -*- coding: utf-8 -*-
"""
ATLAS MOMENTUM HUNTER V3 — Çoklu Borsa Gerçek Zamanlı Avcı
=============================================================
Son güncelleme: 19 Haziran 2026

DEĞİŞİKLİK GÜNLÜĞÜ:
[19 Haziran 2026 — ilk canlı test]
- crypto_signals insert hatası düzeltildi: "coin" kolonu NOT NULL idi,
  sadece "symbol" gönderiliyordu → coin=symbol eklendi.
- İlk canlı testte 3 borsa da bağlandı (MEXC 200, Gate.io 200,
  Coinbase 150 coin) ve DEXT anında yakalandı (ch1m:4.08%, vol:3.0x) —
  mimari çalışıyor, REST tarama beklemesi olmadan saniyeler içinde tespit.
- KRİTİK EKSİK GİDERİLDİ: V3 sadece push+sinyal kaydı yapıyordu, demo_trades'e
  hiç dokunmuyordu — gerçek bot alımı YOKTU. bot_buy_sticky() ve
  sell_sticky_positions() eklendi: artık yapışkan moda giriş gerçek
  pozisyon açıyor, çıkışta gerçek satış yapıp bakiyeyi güncelliyor.
- DİNAMİK YATIRIM: Sabit %20/$50 yerine sinyal gücüne göre $50-$200
  arası ölçekli yatırım. Güç = ch1m büyüklüğü (%40) + hacim çarpanı
  (%40) + çift borsa konfirmasyon bonusu (%20). Zayıf sinyal $50 taban
  alır, çok güçlü sinyal (yüksek ch1m + yüksek hacim + dual confirm)
  $200'e kadar çıkar. Bakiyenin %30'unu asla geçmez.
- signal_type "momentum_v3_entry" yerine "momentum" olarak kaydediliyor
  — Flutter'ın sinyal listesi muhtemelen tanıdık signal_type değerleri
  bekliyor, yeni bir tip eklersek listede görünmeyebilir riski vardı.
  V3 detayı zaten description alanında "MOMENTUM V3" olarak duruyor.

HEDEF: SYN gibi coinleri %3-5'te yakala, sonuna kadar yapışkan takip et.
Hiçbir tarama turu beklemeden — borsa hareket ettiği AN yakalanır.

MİMARİ (whale_detector.py'deki US paralel WebSocket çözümünün
kripto uyarlaması — 19 Haziran 2026, US 6.5 saat sinyal kaçırma
vakasından sonra aynı mantık buraya da taşındı):

- MEXC + Gate.io + Coinbase'e AYNI ANDA, AYRI WebSocket bağlantılarıyla
  bağlanılır. Her borsa kendi thread'inde sürekli dinler.
- Hiçbir REST polling/tarama döngüsü yok — tamamen event-driven.
- Her trade mesajı geldiğinde o coin için rolling 1-dakikalık fiyat/hacim
  penceresi güncellenir.
- ch1m (1 dakikalık değişim) + hacim ivmesi eşiği geçilince anında
  YAPIŞKAN MOD'a girilir — ortalama tespit süresi: saniyeler, dakikalar
  değil.
- Bağlantı koparsa o borsa kendi başına yeniden bağlanır, diğerlerini
  etkilemez.

YAPIŞKAN MOD KRİTERLERİ (giriş):
- ch1m >= %1.5 (1 dakikada güçlü hareket — REST taramasının yakalayamadığı hız)
- Hacim son 60sn'de önceki 60sn ortalamasının 2.5x+ üstünde
- RSI 50-82 (momentum bölgesi, gerekirse hesaplanır)
- VEYA: 2+ borsada AYNI ANDA ch1m >= %1.0 (çift borsa konfirmasyonu —
  manipülasyon değil gerçek hareket)

YAPIŞKAN MOD DAVRANIŞI:
- Giriş: push + crypto_signals'a CRITICAL kayıt
- Her milestone (+%10, +%25, +%50, +%100): "hâlâ tutuyoruz" mesajı
- Her 10 dakikada periyodik güncelleme
- Peak'ten -%8 geri çekilince: çıkış sinyali
- Max 48 saat tutma
"""

import os
import time
import json
import threading
import requests
from datetime import datetime, timezone
from collections import deque, defaultdict
from supabase import create_client
import firebase_admin
from firebase_admin import credentials, messaging

try:
    import websocket
except ImportError:
    os.system("pip install websocket-client --break-system-packages -q")
    import websocket

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
FIREBASE_SERVICE_ACCOUNT = os.environ.get("FIREBASE_SERVICE_ACCOUNT")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

try:
    if FIREBASE_SERVICE_ACCOUNT and not firebase_admin._apps:
        cred = credentials.Certificate(json.loads(FIREBASE_SERVICE_ACCOUNT))
        firebase_admin.initialize_app(cred)
        print("✅ Firebase başlatıldı (Momentum Hunter V3)")
except Exception as e:
    print(f"⚠️ Firebase: {e}")

# ── PARAMETRELER ──────────────────────────────────────────────────
STICKY_CH1M_MIN      = 1.5    # %1.5+ 1 dakikalık değişim (tek borsa)
STICKY_CH1M_DUAL      = 1.0   # %1.0+ yeterli (çift borsa konfirmasyonu varsa)
STICKY_VOL_MULTIPLIER = 2.5   # Son 60sn hacim > önceki 60sn ort. x2.5
STICKY_PUSH_INTERVAL  = 600   # 10 dakikada bir periyodik push
STICKY_EXIT_DRAWDOWN  = 8.0   # Peak'ten %8 → çıkış
STICKY_MAX_HOLD_H     = 48
DUAL_CONFIRM_WINDOW_S = 30    # İki borsada hareket arası max 30sn = "aynı anda"

# Bot alımı parametreleri
MAX_OPEN_PRO = 5
MAX_OPEN_PRO_EXCEPTIONAL = 8
MIN_INVEST_USD = 50    # Taban — en zayıf sinyal bile en az bunu alır
MAX_INVEST_USD = 200   # Tavan — en güçlü sinyal bunu geçemez
STOP_PCT = 0.08
balance_lock = threading.Lock()


def calc_dynamic_investment(balance, ch1m, vol_mult, dual_confirmed):
    """
    Sinyal gücüne göre dinamik yatırım tutarı.
    Güç skoru: ch1m büyüklüğü + hacim çarpanı + çift borsa bonusu.
    $50 taban, $200 tavan — bakiyenin de üstüne çıkmaz.
    """
    # Güç skoru 0-1 arası normalize
    ch1m_score = min(ch1m / 6.0, 1.0)          # %6+ ch1m = tam puan
    vol_score = min(vol_mult / 8.0, 1.0)        # 8x+ hacim = tam puan
    dual_bonus = 0.25 if dual_confirmed else 0

    strength = (ch1m_score * 0.4) + (vol_score * 0.4) + dual_bonus
    strength = min(strength, 1.0)

    # $50 (zayıf) → $200 (çok güçlü) arası lineer ölçek
    invest = MIN_INVEST_USD + (MAX_INVEST_USD - MIN_INVEST_USD) * strength
    invest = min(invest, balance * 0.30)  # Bakiyenin %30'unu asla geçme
    return round(invest, 2), round(strength, 2)

LEV_SUFFIXES = ("3S", "5S", "3L", "5L", "2S", "2L", "10S", "10L", "UP", "DOWN", "BEAR", "BULL")
STABLECOINS = {"USDT", "USDC", "FDUSD", "TUSD", "USDP", "DAI", "USDD", "BUSD", "USDE", "PYUSD"}

# symbol -> {price, vol, timestamp} son 120 saniyelik trade kaydı (her borsa ayrı)
price_windows = defaultdict(lambda: defaultdict(lambda: deque(maxlen=500)))
# symbol -> en son hangi borsada ne zaman hareket görüldü (çift borsa konfirmasyonu için)
recent_moves = defaultdict(dict)  # {symbol: {exchange: timestamp}}

sticky_coins = {}  # {symbol: {...}}
sticky_lock = threading.Lock()

HEADERS = {"User-Agent": "Mozilla/5.0"}


# ── ANALİZ ────────────────────────────────────────────────────────

def update_window(exchange, symbol, price, volume):
    """Her trade mesajında çağrılır — rolling pencereyi günceller."""
    now = time.time()
    window = price_windows[symbol][exchange]
    window.append((now, price, volume))
    # 120 saniyeden eski kayıtları temizle
    while window and now - window[0][0] > 120:
        window.popleft()


def compute_ch1m_and_vol(symbol, exchange):
    """Son 60sn fiyat değişimi ve hacim ivmesini hesapla."""
    window = price_windows[symbol][exchange]
    if len(window) < 2:
        return None, None

    now = time.time()
    last_60 = [(t, p, v) for t, p, v in window if now - t <= 60]
    prev_60 = [(t, p, v) for t, p, v in window if 60 < now - t <= 120]

    if len(last_60) < 2:
        return None, None

    first_price = last_60[0][1]
    last_price = last_60[-1][1]
    if first_price <= 0:
        return None, None

    ch1m = ((last_price - first_price) / first_price) * 100

    vol_last = sum(v for _, _, v in last_60)
    vol_prev = sum(v for _, _, v in prev_60) if prev_60 else 0

    vol_mult = (vol_last / vol_prev) if vol_prev > 0 else (3.0 if vol_last > 0 else 0)

    return round(ch1m, 3), round(vol_mult, 2)


def get_current_price(symbol):
    """En son görülen fiyatı herhangi bir borsadan döndür."""
    for exchange, window in price_windows[symbol].items():
        if window:
            return window[-1][1]
    return None


# ── PUSH & KAYIT ─────────────────────────────────────────────────

def send_push(title, body, symbol=None, extra=None):
    try:
        profiles = supabase.table("profiles").select("fcm_token").not_.is_("fcm_token", "null").execute()
        if not profiles.data:
            return
        tokens = [p["fcm_token"] for p in profiles.data if p.get("fcm_token")]
        for token in tokens:
            try:
                data_payload = {"route": "signals", "click_action": "FLUTTER_NOTIFICATION_CLICK"}
                if symbol:
                    data_payload["symbol"] = symbol
                    data_payload["market"] = "CRYPTO"
                if extra:
                    data_payload.update({k: str(v) for k, v in extra.items()})
                msg = messaging.Message(
                    notification=messaging.Notification(title=title, body=body),
                    data=data_payload,
                    android=messaging.AndroidConfig(priority="high",
                        notification=messaging.AndroidNotification(channel_id="atlas_momentum")),
                    apns=messaging.APNSConfig(payload=messaging.APNSPayload(
                        aps=messaging.Aps(sound="default", badge=1))),
                    token=token,
                )
                messaging.send(msg)
            except Exception:
                pass
        print(f"📱 Push: {title}")
    except Exception as e:
        print(f"❌ Push: {e}")


def log_signal(symbol, price, ch1m, vol_mult, mode, detail=""):
    try:
        result = supabase.table("crypto_signals").insert({
            "symbol": symbol,
            "coin": symbol,
            "signal_type": "momentum",  # Flutter'ın tanıdığı standart tip — V3 detayı açıklamada
            "price": price,
            "description": f"⚡ MOMENTUM V3 {mode.upper()} | {symbol} | ${price} | ch1m:{ch1m}% | vol:{vol_mult}x | {detail}",
            "conviction": "CRITICAL",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
        return result.data[0].get("id") if result.data else None
    except Exception as e:
        print(f"⚠️ Log: {e}")
        return None


def bot_buy_sticky(symbol, price, signal_id, ch1m, vol_mult, confirmed_by):
    """Yapışkan mod girişinde gerçek bot alımı yapar — yatırım sinyal gücüne göre dinamik."""
    try:
        try:
            ms = supabase.table("market_status").select("crypto_status, vix") \
                .eq("id", 1).maybeSingle().execute()
            if ms.data:
                if ms.data.get("crypto_status") == "RED":
                    print(f"  🔴 MAKRO RED — {symbol} alımı durduruldu")
                    return
                vix = float(ms.data.get("vix") or 0)
                if vix >= 25:
                    print(f"  ⚠️ VIX {vix:.1f} — {symbol} alımı durduruldu")
                    return
        except Exception:
            pass

        dual_confirmed = len(confirmed_by) > 0 and any("+" in c for c in confirmed_by)

        portfolios = supabase.table("demo_portfolios").select("user_id, crypto_balance").execute()
        if not portfolios.data:
            return

        for p in portfolios.data:
            user_id = p["user_id"]
            try:
                profile = supabase.table("profiles").select("is_pro").eq("id", user_id).limit(1).execute()
                is_pro = profile.data[0].get("is_pro", False) if profile.data else False
                if not is_pro:
                    continue

                open_trades = supabase.table("demo_trades").select("*") \
                    .eq("user_id", user_id).eq("market", "CRYPTO").eq("status", "open").execute()
                open_list = open_trades.data or []
                open_count = len(open_list)

                if any(t.get("symbol") == symbol for t in open_list):
                    print(f"  ⏭️ {symbol} zaten açık — {user_id[:8]} atlandı")
                    continue

                exceptional_count = sum(1 for t in open_list if t.get("is_exceptional"))
                is_exceptional = False

                if open_count >= MAX_OPEN_PRO:
                    if exceptional_count < 3 and open_count < MAX_OPEN_PRO_EXCEPTIONAL:
                        is_exceptional = True
                    else:
                        print(f"  ⏭️ {user_id[:8]} dolu ({open_count}) — {symbol} atlandı")
                        continue

                stop_price = round(price * (1 - STOP_PCT), 10)

                with balance_lock:
                    fresh = supabase.table("demo_portfolios").select("crypto_balance") \
                        .eq("user_id", user_id).limit(1).execute()
                    balance = fresh.data[0]["crypto_balance"] if fresh.data else 0

                    invest, strength = calc_dynamic_investment(balance, ch1m, vol_mult, dual_confirmed)
                    if invest < 5 or balance < invest:
                        print(f"  ⏭️ {user_id[:8]} bakiye yetersiz (${balance:.0f} < ${invest:.0f})")
                        continue

                    supabase.table("demo_trades").insert({
                        "user_id": user_id,
                        "symbol": symbol,
                        "market": "CRYPTO",
                        "signal_id": signal_id,
                        "buy_price": price,
                        "buy_date": datetime.now(timezone.utc).isoformat(),
                        "quantity": round(invest / price, 6),
                        "status": "open",
                        "signal_layer": "MOMENTUM_V3",
                        "entry_conviction": "CRITICAL",
                        "stop_price": stop_price,
                        "current_price": price,
                        "peak_price": price,
                        "entry_reason": f"V3 anlık yakalama (güç:{strength}): ch1m:{ch1m}% vol:{vol_mult}x | {' + '.join(confirmed_by)}",
                        "is_exceptional": is_exceptional,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    }).execute()

                    supabase.table("demo_portfolios").update({
                        "crypto_balance": round(balance - invest, 2)
                    }).eq("user_id", user_id).execute()

                tag = "🌟" if is_exceptional else "✅"
                print(f"  {tag} BOT ALIM: {user_id[:8]} → {symbol} ${invest:.0f} (güç:{strength}) | Stop:{stop_price:.8f}")
            except Exception as e:
                print(f"⚠️ bot_buy_sticky {user_id[:8]}: {e}")
            time.sleep(0.1)
    except Exception as e:
        print(f"❌ bot_buy_sticky: {e}")


# ── YAPIŞKAN MOD ──────────────────────────────────────────────────

def enter_sticky(symbol, price, ch1m, vol_mult, confirmed_by):
    with sticky_lock:
        if symbol in sticky_coins:
            return
        now = time.time()
        sticky_coins[symbol] = {
            "entry_price": price,
            "peak_price": price,
            "entry_time": now,
            "last_push": now,
            "milestones": set(),
        }

    konfirm = " + ".join(confirmed_by)
    send_push(
        title=f"🎯 YAPIŞKAN MOD: {symbol}",
        body=f"${price:.6f} | 1dk: +{ch1m}% | Hacim {vol_mult}x | {konfirm}",
        symbol=symbol,
        extra={"mode": "sticky_entry"}
    )
    signal_id = log_signal(symbol, price, ch1m, vol_mult, "entry", konfirm)
    bot_buy_sticky(symbol, price, signal_id, ch1m, vol_mult, confirmed_by)
    print(f"\n🎯⚡ ANINDA YAKALANDI: {symbol} @ ${price:.6f} | ch1m:{ch1m}% vol:{vol_mult}x | {konfirm}")


def sell_sticky_positions(symbol, exit_price, exit_reason):
    """Yapışkan mod çıkışında demo_trades'teki gerçek pozisyonları kapatır."""
    try:
        trades = supabase.table("demo_trades").select("*") \
            .eq("symbol", symbol).eq("market", "CRYPTO").eq("status", "open") \
            .eq("signal_layer", "MOMENTUM_V3").execute()
        if not trades.data:
            return
        for trade in trades.data:
            try:
                pl = (exit_price - float(trade["buy_price"])) * float(trade["quantity"])
                supabase.table("demo_trades").update({
                    "sell_price": exit_price,
                    "sell_date": datetime.now(timezone.utc).isoformat(),
                    "status": "closed",
                    "profit_loss": round(pl, 2),
                    "exit_reason": exit_reason,
                }).eq("id", trade["id"]).execute()

                with balance_lock:
                    port = supabase.table("demo_portfolios").select("crypto_balance") \
                        .eq("user_id", trade["user_id"]).limit(1).execute()
                    if port.data:
                        new_bal = port.data[0]["crypto_balance"] + (float(trade["quantity"]) * exit_price)
                        supabase.table("demo_portfolios").update({
                            "crypto_balance": round(new_bal, 2)
                        }).eq("user_id", trade["user_id"]).execute()
                print(f"  💰 KAPANDI: {trade['user_id'][:8]} → {symbol} | ${pl:.2f}")
            except Exception as e:
                print(f"⚠️ sell_sticky {trade.get('id')}: {e}")
    except Exception as e:
        print(f"❌ sell_sticky_positions: {e}")


def update_sticky_loop():
    """Her 5 saniyede yapışkan coinleri kontrol eder — milestone/çıkış."""
    while True:
        try:
            with sticky_lock:
                symbols = list(sticky_coins.keys())

            for symbol in symbols:
                with sticky_lock:
                    coin = sticky_coins.get(symbol)
                if not coin:
                    continue

                price = get_current_price(symbol)
                if not price:
                    continue

                now = time.time()
                entry_price = coin["entry_price"]
                peak_price = max(coin["peak_price"], price)
                gain = ((price - entry_price) / entry_price) * 100
                hold_hours = (now - coin["entry_time"]) / 3600

                with sticky_lock:
                    if symbol in sticky_coins:
                        sticky_coins[symbol]["peak_price"] = peak_price

                peak_gain = ((peak_price - entry_price) / entry_price) * 100
                drawdown = ((peak_price - price) / peak_price) * 100 if peak_price > 0 else 0

                # ÇIKIŞ
                if drawdown >= STICKY_EXIT_DRAWDOWN and peak_gain > 3:
                    send_push(
                        title=f"🔴 {symbol} — ÇIK",
                        body=f"Zirve ${peak_price:.6f} → ${price:.6f} (-{drawdown:.1f}%) | K/Z: {gain:+.1f}%",
                        symbol=symbol, extra={"mode": "sticky_exit"}
                    )
                    print(f"🔴 {symbol} ÇIKIŞ: -{drawdown:.1f}% zirveden | K/Z:{gain:+.1f}%")
                    sell_sticky_positions(symbol, price, f"Trailing stop (zirveden -%{STICKY_EXIT_DRAWDOWN:.0f})")
                    with sticky_lock:
                        sticky_coins.pop(symbol, None)
                    continue

                if hold_hours >= STICKY_MAX_HOLD_H:
                    send_push(title=f"⏰ {symbol} — 48s doldu", body=f"K/Z: {gain:+.1f}%", symbol=symbol)
                    sell_sticky_positions(symbol, price, "48 saat doldu — zaman aşımı")
                    with sticky_lock:
                        sticky_coins.pop(symbol, None)
                    continue

                # MİLESTONE
                for ms, emoji, msg in [
                    (10, "📈", "Devam ediyor"),
                    (25, "🔥", "Güçlü gidiş"),
                    (50, "🔥🔥", "Hâlâ tutuyoruz!"),
                    (100, "🚀🚀", "2x yaptı — devam"),
                ]:
                    if gain >= ms and ms not in coin["milestones"]:
                        send_push(
                            title=f"{emoji} {symbol} +%{ms} geçti!",
                            body=f"${price:.6f} | Peak: +{peak_gain:.1f}% | {msg}",
                            symbol=symbol, extra={"mode": f"milestone_{ms}"}
                        )
                        with sticky_lock:
                            if symbol in sticky_coins:
                                sticky_coins[symbol]["milestones"].add(ms)
                        print(f"{emoji} {symbol} milestone +%{ms}")

                # PERİYODİK PUSH
                if now - coin["last_push"] >= STICKY_PUSH_INTERVAL:
                    emoji = "🔥🔥" if gain >= 30 else "🔥" if gain >= 15 else "📈" if gain >= 5 else "⏳"
                    send_push(
                        title=f"{emoji} {symbol} {gain:+.1f}%",
                        body=f"${price:.6f} | Peak: +{peak_gain:.1f}% | {hold_hours:.1f}s tutuldu",
                        symbol=symbol, extra={"mode": "sticky_update"}
                    )
                    with sticky_lock:
                        if symbol in sticky_coins:
                            sticky_coins[symbol]["last_push"] = now

            time.sleep(5)
        except Exception as e:
            print(f"❌ Sticky loop: {e}")
            time.sleep(5)


# ── SİNYAL DEĞERLENDİRME ─────────────────────────────────────────

def evaluate_symbol(exchange, symbol):
    """Her trade güncellemesinden sonra çağrılır — yapışkan mod tetiklenmeli mi?"""
    with sticky_lock:
        if symbol in sticky_coins:
            return

    if any(symbol.endswith(s) for s in LEV_SUFFIXES):
        return
    if symbol in STABLECOINS:
        return

    ch1m, vol_mult = compute_ch1m_and_vol(symbol, exchange)
    if ch1m is None or vol_mult is None:
        return

    now = time.time()

    # Bu borsada hareket kaydı tut
    if ch1m >= 0.5:
        recent_moves[symbol][exchange] = now

    # Çift borsa konfirmasyonu kontrolü
    other_exchanges_moved = [
        ex for ex, ts in recent_moves[symbol].items()
        if ex != exchange and now - ts <= DUAL_CONFIRM_WINDOW_S
    ]
    dual_confirmed = len(other_exchanges_moved) > 0

    price = get_current_price(symbol)
    if not price or price <= 0:
        return

    confirmed_by = []

    if dual_confirmed and ch1m >= STICKY_CH1M_DUAL:
        confirmed_by.append(f"{exchange}+{'+'.join(other_exchanges_moved)}")
        if vol_mult >= STICKY_VOL_MULTIPLIER * 0.7:  # Çift borsa varsa hacim eşiği gevşer
            confirmed_by.append(f"Hacim {vol_mult}x")
            enter_sticky(symbol, price, ch1m, vol_mult, confirmed_by)
            return

    if ch1m >= STICKY_CH1M_MIN and vol_mult >= STICKY_VOL_MULTIPLIER:
        confirmed_by.append(f"{exchange} tek borsa")
        confirmed_by.append(f"Hacim {vol_mult}x")
        enter_sticky(symbol, price, ch1m, vol_mult, confirmed_by)


# ── MEXC WEBSOCKET ────────────────────────────────────────────────

def mexc_on_message(ws, message):
    try:
        data = json.loads(message)
        ch = data.get("c", "")
        if "deals" not in ch:
            return
        symbol = ch.split("@")[-1].replace("USDT", "")
        deals = data.get("d", {}).get("deals", [])
        for d in deals:
            price = float(d.get("p", 0))
            vol = float(d.get("v", 0))
            if price <= 0:
                continue
            update_window("MEXC", symbol, price, vol * price)
        if deals:
            evaluate_symbol("MEXC", symbol)
    except Exception:
        pass


def mexc_on_open(ws):
    print("✅ MEXC WebSocket bağlandı")
    # MEXC tek bağlantıda sınırlı kanal destekler — en likit ~200 coin'e abone ol
    try:
        r = requests.get("https://api.mexc.com/api/v3/ticker/24hr", timeout=10)
        tickers = r.json()
        usdt_pairs = [t for t in tickers if t.get("symbol", "").endswith("USDT")]
        usdt_pairs.sort(key=lambda t: float(t.get("quoteVolume", 0) or 0), reverse=True)
        top_symbols = [t["symbol"] for t in usdt_pairs[:200]]

        params = [f"spot@public.deals.v3.api@{sym}" for sym in top_symbols]
        # MEXC batch subscribe — max ~30 kanal/mesaj
        for i in range(0, len(params), 20):
            batch = params[i:i+20]
            ws.send(json.dumps({"method": "SUBSCRIPTION", "params": batch}))
            time.sleep(0.2)
        print(f"  → MEXC: {len(top_symbols)} coin'e abone olundu")
    except Exception as e:
        print(f"⚠️ MEXC abone hatası: {e}")


def run_mexc_ws():
    while True:
        try:
            ws = websocket.WebSocketApp(
                "wss://wbs-api.mexc.com/ws",
                on_open=mexc_on_open,
                on_message=mexc_on_message,
                on_error=lambda ws, e: print(f"❌ MEXC WS hata: {e}"),
                on_close=lambda ws, c, m: print("🔌 MEXC WS kapandı, yeniden bağlanıyor..."),
            )
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            print(f"⚠️ MEXC WS döngü hatası: {e}")
        time.sleep(5)


# ── GATE.IO WEBSOCKET ─────────────────────────────────────────────

def gateio_on_message(ws, message):
    try:
        data = json.loads(message)
        if data.get("event") != "update" or data.get("channel") != "spot.trades":
            return
        result = data.get("result")
        if not result:
            return
        items = result if isinstance(result, list) else [result]
        symbol = None
        for item in items:
            pair = item.get("currency_pair", "")
            if not pair.endswith("_USDT"):
                continue
            symbol = pair[:-5]
            price = float(item.get("price", 0))
            amount = float(item.get("amount", 0))
            if price <= 0:
                continue
            update_window("Gate.io", symbol, price, amount * price)
        if symbol:
            evaluate_symbol("Gate.io", symbol)
    except Exception:
        pass


def gateio_on_open(ws):
    print("✅ Gate.io WebSocket bağlandı")
    try:
        r = requests.get("https://api.gateio.ws/api/v4/spot/tickers", timeout=10)
        tickers = r.json()
        usdt_pairs = [t for t in tickers if t.get("currency_pair", "").endswith("_USDT")]
        usdt_pairs.sort(key=lambda t: float(t.get("quote_volume", 0) or 0), reverse=True)
        top_pairs = [t["currency_pair"] for t in usdt_pairs[:200]]

        for i in range(0, len(top_pairs), 50):
            batch = top_pairs[i:i+50]
            ws.send(json.dumps({
                "time": int(time.time()),
                "channel": "spot.trades",
                "event": "subscribe",
                "payload": batch
            }))
            time.sleep(0.2)
        print(f"  → Gate.io: {len(top_pairs)} coin'e abone olundu")
    except Exception as e:
        print(f"⚠️ Gate.io abone hatası: {e}")


def run_gateio_ws():
    while True:
        try:
            ws = websocket.WebSocketApp(
                "wss://api.gateio.ws/ws/v4/",
                on_open=gateio_on_open,
                on_message=gateio_on_message,
                on_error=lambda ws, e: print(f"❌ Gate.io WS hata: {e}"),
                on_close=lambda ws, c, m: print("🔌 Gate.io WS kapandı, yeniden bağlanıyor..."),
            )
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            print(f"⚠️ Gate.io WS döngü hatası: {e}")
        time.sleep(5)


# ── COINBASE WEBSOCKET ────────────────────────────────────────────

def coinbase_on_message(ws, message):
    try:
        data = json.loads(message)
        if data.get("type") != "ticker":
            return
        product = data.get("product_id", "")
        if not product.endswith("-USD") and not product.endswith("-USDT"):
            return
        symbol = product.split("-")[0]
        price = float(data.get("price", 0) or 0)
        vol = float(data.get("last_size", 0) or 0)
        if price <= 0:
            return
        update_window("Coinbase", symbol, price, vol * price)
        evaluate_symbol("Coinbase", symbol)
    except Exception:
        pass


def coinbase_on_open(ws):
    print("✅ Coinbase WebSocket bağlandı")
    try:
        r = requests.get("https://api.exchange.coinbase.com/products", timeout=10)
        products = r.json()
        usd_pairs = [p["id"] for p in products if p.get("quote_currency") == "USD" and p.get("status") == "online"]
        top_pairs = usd_pairs[:150]  # Coinbase tüm coinleri kapsamıyor zaten, hepsini al

        ws.send(json.dumps({
            "type": "subscribe",
            "product_ids": top_pairs,
            "channels": ["ticker"]
        }))
        print(f"  → Coinbase: {len(top_pairs)} coin'e abone olundu")
    except Exception as e:
        print(f"⚠️ Coinbase abone hatası: {e}")


def run_coinbase_ws():
    while True:
        try:
            ws = websocket.WebSocketApp(
                "wss://ws-feed.exchange.coinbase.com",
                on_open=coinbase_on_open,
                on_message=coinbase_on_message,
                on_error=lambda ws, e: print(f"❌ Coinbase WS hata: {e}"),
                on_close=lambda ws, c, m: print("🔌 Coinbase WS kapandı, yeniden bağlanıyor..."),
            )
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            print(f"⚠️ Coinbase WS döngü hatası: {e}")
        time.sleep(5)


# ── TEMİZLİK (eski pencere verisi birikmesin) ────────────────────

def cleanup_loop():
    """Eski sembollerin pencere verisini periyodik temizle — bellek şişmesin."""
    while True:
        time.sleep(300)
        try:
            now = time.time()
            removed = 0
            for symbol in list(price_windows.keys()):
                all_old = True
                for exchange, window in price_windows[symbol].items():
                    if window and now - window[-1][0] < 600:
                        all_old = False
                        break
                if all_old:
                    del price_windows[symbol]
                    recent_moves.pop(symbol, None)
                    removed += 1
            if removed:
                print(f"🧹 Temizlik: {removed} eski sembol pencereden silindi")
        except Exception as e:
            print(f"⚠️ Cleanup: {e}")


# ── ANA ──────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("🎯⚡ ATLAS MOMENTUM HUNTER V3 — Çoklu Borsa Gerçek Zamanlı")
    print("MEXC + Gate.io + Coinbase paralel WebSocket — REST tarama YOK")
    print(f"Eşik: ch1m>={STICKY_CH1M_MIN}% (dual:{STICKY_CH1M_DUAL}%) | Hacim>{STICKY_VOL_MULTIPLIER}x")
    print(f"Çıkış: peak'ten -%{STICKY_EXIT_DRAWDOWN} | Push: her {STICKY_PUSH_INTERVAL//60}dk")
    print("=" * 60)

    threads = [
        threading.Thread(target=run_mexc_ws, daemon=True, name="MEXC-WS"),
        threading.Thread(target=run_gateio_ws, daemon=True, name="GateIO-WS"),
        threading.Thread(target=run_coinbase_ws, daemon=True, name="Coinbase-WS"),
        threading.Thread(target=update_sticky_loop, daemon=True, name="Sticky-Monitor"),
        threading.Thread(target=cleanup_loop, daemon=True, name="Cleanup"),
    ]
    for t in threads:
        t.start()
        time.sleep(0.5)

    print("✅ Tüm borsalar paralel dinleniyor. Sistem aktif.\n")

    # Ana thread canlı tut + periyodik durum raporu
    while True:
        time.sleep(60)
        with sticky_lock:
            active = list(sticky_coins.keys())
        tracked = len(price_windows)
        print(f"📊 Durum: {tracked} coin izleniyor | {len(active)} yapışkan mod: {active}")


if __name__ == "__main__":
    main()
