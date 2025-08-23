# runner.py — 最小実行エントリ
import os
import random
import requests
from datetime import datetime, timezone, timedelta

from fr_arbitrage_bot import evaluate_candidate_simple, build_simple_notification

# Discord Webhook (GitHub Secrets から渡す想定)
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

def send_discord(msg: str):
    if not DISCORD_WEBHOOK_URL:
        print("[DRY-RUN] Discord未設定:\n", msg)
        return
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": msg})
    except Exception as e:
        print("通知エラー:", e)

def main():
    # テスト用の銘柄リスト
    symbols = ["BTCUSDT", "ETHUSDT", "BIDUSDT"]

    lines = []
    for sym in symbols:
        # ダミーのFR差 (ランダム生成)
        fr_high_4h = random.uniform(0.0003, 0.0008)  # 例: +0.03%〜+0.08%/4h
        fr_low_4h  = random.uniform(-0.0002, 0.0002) # 例: -0.02%〜+0.02%/4h

        # ダミーの価格
        price_high = 30000.0 if "BTC" in sym else 2000.0
        price_low  = price_high * random.uniform(0.999, 1.001)

        # 候補を評価
        ev = evaluate_candidate_simple(
            symbol=sym,
            fr_high_4h=fr_high_4h, fr_low_4h=fr_low_4h,
            price_high=price_high, price_low=price_low,
            side_notional_usdt=50,   # 片側の名目額
            fee_taker_high=0.00055,  # 例: Bybit taker
            fee_taker_low=0.00060,   # 例: Bitget taker
            slip_bps_high=6.0,
            slip_bps_low=6.0,
            interval_h=4.0,
            keep_margin_bp=5.0
        )

        msg = build_simple_notification(ev)
        lines.append(msg)

    now_jst = datetime.now(timezone(timedelta(hours=9)))
    header = f"=== FR Arbitrage Screening {now_jst:%Y-%m-%d %H:%M} JST ==="
    full_msg = header + "\n\n" + "\n\n".join(lines)

    send_discord(full_msg)

if __name__ == "__main__":
    main()
