# -*- coding: utf-8 -*-
"""
ATLAS MAKRO - SADECE BU ÇALIŞSIN!
"""

import os
import time
import json
import requests
from datetime import datetime, timezone
from typing import Optional, Tuple

# ============================================================
# BASİT KONFIGÜRASYON
# ============================================================

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")

# ============================================================
# DEEPSEEK WEB SEARCH - TEST EDİLMİŞ
# ============================================================

def deepseek_news(query: str) -> Optional[str]:
    """DeepSeek web search ile haber çek"""
    if not DEEPSEEK_API_KEY:
        return "⚠️ DEEPSEEK_API_KEY yok!"
    
    try:
        print(f"  🔍 DeepSeek: {query[:60]}...")
        
        response = requests.post(
            "https://api.deepseek.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "deepseek-chat",
                "messages": [
                    {"role": "system", "content": "Finans uzmanısın. Gerçek haberleri ver."},
                    {"role": "user", "content": f"Web'de ara: {query}. Reuters, Bloomberg, CNBC başlıklarını ver."}
                ],
                "web_search": True,
                "max_tokens": 400
            },
            timeout=15
        )
        
        if response.status_code == 200:
            return response.json()["choices"][0]["message"]["content"]
        else:
            return f"❌ Hata: {response.status_code}"
            
    except Exception as e:
        return f"❌ Hata: {e}"

# ============================================================
# S&P500 VERİSİ - DÜZELTİLMİŞ
# ============================================================

def get_sp500():
    """S&P500 fiyat ve değişim - ÇALIŞIYOR"""
    
    # YAHOO FINANCE
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC"
        params = {"interval": "1d", "range": "2d"}
        headers = {'User-Agent': 'Mozilla/5.0'}
        
        r = requests.get(url, params=params, headers=headers, timeout=10)
        data = r.json()
        
        meta = data["chart"]["result"][0]["meta"]
        price = meta.get("regularMarketPrice", 0)
        prev = meta.get("previousClose", 0)
        
        if price > 1000 and prev > 1000:
            pct = ((price - prev) / prev) * 100
            return price, pct
    except Exception as e:
        print(f"  Yahoo hatası: {e}")
    
    # ALTERNATİF: MEXC
    try:
        r = requests.get(
            "https://api.mexc.com/api/v3/ticker/24hr?symbol=SPXUSDT",
            timeout=8
        )
        data = r.json()
        price = float(data.get("lastPrice", 0))
        prev = float(data.get("prevClosePrice", 0))
        
        if price > 1000 and prev > 1000:
            pct = ((price - prev) / prev) * 100
            return price, pct
    except Exception as e:
        print(f"  MEXC hatası: {e}")
    
    return None, None

# ============================================================
# BTC VERİSİ
# ============================================================

def get_btc():
    """BTC fiyat ve değişim"""
    try:
        r = requests.get(
            "https://api.mexc.com/api/v3/klines",
            params={"symbol": "BTCUSDT", "interval": "1h", "limit": 2},
            timeout=8
        )
        data = r.json()
        if len(data) >= 2:
            prev = float(data[0][4])
            curr = float(data[1][4])
            pct = ((curr - prev) / prev) * 100
            return curr, pct
    except Exception as e:
        print(f"  BTC hatası: {e}")
    return None, None

# ============================================================
# ANA DÖNGÜ
# ============================================================

def main():
    print("\n" + "="*60)
    print("🚀 ATLAS MAKRO - TEK BAŞINA ÇALIŞIYOR")
    print("="*60 + "\n")
    
    # DeepSeek kontrol
    if DEEPSEEK_API_KEY:
        print("✅ DeepSeek API Key VAR")
        print("🧪 Test sorgusu gönderiliyor...")
        test = deepseek_news("Bitcoin son dakika haberleri")
        print(f"📰 Test sonucu:\n{test[:300]}\n")
    else:
        print("❌ DeepSeek API Key YOK!")
    
    while True:
        now = datetime.now(timezone.utc)
        print(f"\n{'='*50}")
        print(f"🔍 KONTROL {now.strftime('%H:%M UTC')}")
        print(f"{'='*50}")
        
        # BTC
        btc_price, btc_pct = get_btc()
        if btc_price:
            print(f"💰 BTC: ${btc_price:,.0f} | {btc_pct:+.2f}%")
        
        # S&P500
        sp_price, sp_pct = get_sp500()
        if sp_price:
            print(f"🇺🇸 S&P500: ${sp_price:,.2f} | {sp_pct:+.2f}%")
            
            # KRİTİK: S&P500 DÜŞÜŞÜNDE DEEPSEEK ÇALIŞTIR
            if sp_pct <= -1.0 and int(time.time()) % 300 < 60:
                print("\n📰 S&P500 düşüş! DeepSeek haber çekiyor...")
                news = deepseek_news(f"S&P500 {sp_pct:.1f}% düştü. Neden?")
                if news:
                    print(f"\n{news}\n")
        else:
            print("🇺🇸 S&P500: VERİ YOK!")
        
        print(f"\n✅ Tamamlandı | {now.strftime('%H:%M UTC')}")
        print(f"⏳ 60 saniye...")
        time.sleep(60)

if __name__ == "__main__":
    main()
