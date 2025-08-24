# fr_arbitrage_discord_bot.py  — 公開APIオンリー / Discord専用チャンネル限定
# 依存: pip install discord.py requests python-dotenv
import os, json, math, time, random
from datetime import datetime, timezone, timedelta
from typing import Dict, Any

import requests
import discord
from discord.ext import tasks
from discord import app_commands
from dotenv import load_dotenv

# ========= 基本設定 =========
load_dotenv()
BOT_TOKEN  = os.getenv("DISCORD_BOT_TOKEN", "")
CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0"))  # frアビトラ専用チャンネルID

UTC = timezone.utc
JST = timezone(timedelta(hours=9))

STATE_DIR      = "state"
POSITIONS_FILE = os.path.join(STATE_DIR, "positions.json")
COOLDOWN_FILE  = os.path.join(STATE_DIR, "cooldown.json")

# 監視・通知のパラメータ
APR_MIN_ALERT = float(os.getenv("APR_MIN_ALERT", "100"))  # APR<100%で警告
SCAN_MINUTES  = int(os.getenv("SCAN_MINUTES", "5"))       # 監視間隔(分)

# 手数料・滑り（概算、必要なら .env 側で後日調整）
TAKER_BYBIT  = float(os.getenv("TAKER_FEE_BYBIT",  "0.0006"))
TAKER_BITGET = float(os.getenv("TAKER_FEE_BITGET", "0.0006"))
TAKER_MEXC   = float(os.getenv("TAKER_FEE_MEXC",   "0.0007"))
ENTRY_SLIP   = float(os.getenv("ENTRY_SLIP_FRAC",  "0.0002"))

def ensure_state():
    os.makedirs(STATE_DIR, exist_ok=True)
    if not os.path.exists(POSITIONS_FILE): json.dump({}, open(POSITIONS_FILE,"w"))
    if not os.path.exists(COOLDOWN_FILE):  json.dump({}, open(COOLDOWN_FILE,"w"))

def load_json(path, default):
    try:
        with open(path,"r",encoding="utf-8") as f: return json.load(f)
    except: return default

def save_json(path, obj):
    with open(path,"w",encoding="utf-8") as f: json.dump(obj,f,ensure_ascii=False,indent=2)

def now_utc(): return datetime.now(UTC)
def to_jst_str(dt): return dt.astimezone(JST).strftime("%Y-%m-%d %H:%M:%S JST")

# ========= HTTPユーティリティ（指数バックオフ） =========
def http_get(url, params=None, headers=None, timeout=15, max_retries=3, backoff=0.5):
    for i in range(max_retries):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=timeout)
            if r.status_code == 429 or 500 <= r.status_code < 600:
                raise RuntimeError(f"HTTP {r.status_code}")
            return r.json()
        except Exception:
            if i == max_retries-1: raise
            time.sleep(backoff * (2**i) + random.uniform(0,0.2))

# ========= 取引所 公開API =========
BYBIT_BASE  = "https://api.bybit.com"
BITGET_BASE = "https://api.bitget.com"
MEXC_BASE   = "https://contract.mexc.com"

def mexc_symbol(symbol):  # BTCUSDT → BTC_USDT
    return symbol.replace("USDT","_USDT") if "_" not in symbol else symbol

# Funding rate / interval
def bybit_funding_last(symbol):
    r = http_get(f"{BYBIT_BASE}/v5/market/funding/history",
                 params={"category":"linear","symbol":symbol,"limit":"1"})
    it = (r.get("result",{}).get("list") or [{}])[0]
    fr = float(it.get("fundingRate",0.0))
    ts = int(it.get("fundingRateTimestamp",0))
    dt = datetime.fromtimestamp(ts/1000, tz=UTC) if ts else None
    return {"fr":fr, "time":dt}

def bybit_instrument_interval(symbol):
    r = http_get(f"{BYBIT_BASE}/v5/market/instruments-info",
                 params={"category":"linear","symbol":symbol})
    it = (r.get("result",{}).get("list") or [{}])[0]
    return int(it.get("fundingInterval", 480) or 480)  # 既定8h

def bitget_funding_current(symbol):
    r = http_get(f"{BITGET_BASE}/api/v2/mix/market/current-fund-rate",
                 params={"symbol":symbol,"productType":"USDT-FUTURES"})
    return float((r.get("data") or {}).get("fundingRate") or 0.0)

def mexc_funding_current(symbol_mexc):
    r = http_get(f"{MEXC_BASE}/api/v1/contract/fundingRate/{symbol_mexc}")
    fr = r.get("fundingRate") or (r.get("data",{}).get("fundingRate") if isinstance(r.get("data"),dict) else 0.0)
    try: return float(fr)
    except: return 0.0

# 価格（mark/last どれか）
def bybit_mark_last(symbol):
    r = http_get(f"{BYBIT_BASE}/v5/market/tickers", params={"category":"linear","symbol":symbol})
    it = (r.get("result",{}).get("list") or [{}])[0]
    return float(it.get("markPrice") or it.get("lastPrice") or 0.0)

def bitget_mark_last(symbol):
    r = http_get(f"{BITGET_BASE}/api/v2/mix/market/ticker", params={"productType":"USDT-FUTURES","symbol":symbol})
    d = r.get("data") or {}
    return float(d.get("indexPrice") or d.get("last") or 0.0)

def mexc_mark_last(symbol_mexc):
    r = http_get(f"{MEXC_BASE}/api/v1/contract/ticker", params={"symbol":symbol_mexc})
    d = (r.get("data") or [{}])[0] if isinstance(r.get("data"), list) else r.get("data",{})
    return float(d.get("fairPrice") or d.get("lastPrice") or d.get("indexPrice") or 0.0)

def get_mark(exchange, symbol):
    if exchange=="Bybit":  return bybit_mark_last(symbol)
    if exchange=="Bitget": return bitget_mark_last(symbol)
    if exchange=="MEXC":   return mexc_mark_last(mexc_symbol(symbol))
    return 0.0

# Orderbook best(数量×価格のnotionalで板厚をみる)
def bybit_orderbook_best(symbol):
    r = http_get(f"{BYBIT_BASE}/v5/market/orderbook",
                 params={"category":"linear","symbol":symbol,"limit":"1"})
    a = (r.get("result",{}).get("a") or [[0,0]])[0]
    b = (r.get("result",{}).get("b") or [[0,0]])[0]
    ask_px, ask_sz = float(a[0]), float(a[1]); bid_px, bid_sz = float(b[0]), float(b[1])
    return ask_px*ask_sz, bid_px*bid_sz

def bitget_orderbook_best(symbol):
    r = http_get(f"{BITGET_BASE}/api/v2/mix/market/depth",
                 params={"productType":"USDT-FUTURES","symbol":symbol,"limit":"1"})
    d = r.get("data") or {}
    a = (d.get("asks") or [[0,0]])[0]; b = (d.get("bids") or [[0,0]])[0]
    ask_px, ask_sz = float(a[0]), float(a[1]); bid_px, bid_sz = float(b[0]), float(b[1])
    return ask_px*ask_sz, bid_px*bid_sz

def mexc_orderbook_best(symbol_mexc):
    r = http_get(f"{MEXC_BASE}/api/v1/contract/depth", params={"symbol":symbol_mexc,"limit":"1"})
    a = (r.get("asks") or [[0,0]])[0]; b = (r.get("bids") or [[0,0]])[0]
    ask_px, ask_sz = float(a[0]), float(a[1]); bid_px, bid_sz = float(b[0]), float(b[1])
    return ask_px*ask_sz, bid_px*bid_sz

# ========= 計算系 =========
def calc_apr(diff_fraction: float, interval_min: int) -> float:
    per_day = 1440.0 / interval_min
    return diff_fraction * per_day * 365 * 100.0

def symbol_interval_minutes(symbol: str) -> int:
    try: return bybit_instrument_interval(symbol)  # Bybit基準で取得（多くが8h/4h）
    except: return 480

def minutes_to_next_funding(symbol: str) -> int:
    iv = symbol_interval_minutes(symbol)
    last = bybit_funding_last(symbol)
    if not last["time"]:
        next_time = now_utc().replace(second=0, microsecond=0) + timedelta(minutes=iv)
    else:
        next_time = last["time"] + timedelta(minutes=iv)
    return max(0, int((next_time - now_utc()).total_seconds() // 60))

def fetch_fr_for_exchange(exchange: str, symbol: str) -> float:
    if exchange == "Bybit":
        return bybit_funding_last(symbol)["fr"]
    elif exchange == "Bitget":
        return bitget_funding_current(symbol)
    elif exchange == "MEXC":
        return mexc_funding_current(mexc_symbol(symbol))
    else:
        return 0.0

def taker_for(exchange: str) -> float:
    return TAKER_BYBIT if exchange=="Bybit" else (TAKER_BITGET if exchange=="Bitget" else TAKER_MEXC)

# ========= Discord Bot =========
intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

def only_target_channel(inter) -> bool:
    return inter.channel_id == CHANNEL_ID

def fmt_pct(x, d=2): return f"{x:.{d}f}%"
def fmt_usd(x): return f"${x:,.2f}"

# ---------- ランク精査 ----------
def evaluate_liquidity_and_rank(symbol: str, short_ex: str, long_ex: str) -> dict:
    """
    公開APIで出来高/板/価格乖離/現在APRをチェックして S/A/B/C/D を返す
    しきい値は下の定数でチューニング可能
    """
    VOL_TIERS = [2_000_000_000, 1_000_000_000, 300_000_000, 100_000_000]   # S/A/B/C
    BBO_TIERS = [1_000_000, 500_000, 200_000, 100_000]                      # S/A/B/C（各脚のmin(a,b)）
    GAP_BPS_PENALTY = [(15, -2), (5, -1)]  # 価格乖離(bps)が閾値超で減点
    APR_BONUS = [(200, +1), (100, 0), (80, -1), (0, -2)]

    iv = symbol_interval_minutes(symbol)
    fr_s = fetch_fr_for_exchange(short_ex, symbol)
    fr_l = fetch_fr_for_exchange(long_ex,  symbol)
    diff = max(0.0, fr_s - fr_l)
    apr  = calc_apr(diff, iv)

    # 出来高/板
    def liq(ex):
        if ex=="Bybit":
            a,b = bybit_orderbook_best(symbol)
            t = http_get(f"{BYBIT_BASE}/v5/market/tickers", params={"category":"linear","symbol":symbol})
            vol = float(((t.get("result",{}).get("list") or [{}])[0]).get("turnover24h") or 0)
            return vol, float(a), float(b)
        if ex=="Bitget":
            a,b = bitget_orderbook_best(symbol)
            t = http_get(f"{BITGET_BASE}/api/v2/mix/market/ticker", params={"productType":"USDT-FUTURES","symbol":symbol})
            d = t.get("data") or {}
            vol = float(d.get("usdtVolume") or d.get("quoteVolume") or 0)
            return vol, float(a), float(b)
        a,b = mexc_orderbook_best(mexc_symbol(symbol))
        t = http_get(f"{MEXC_BASE}/api/v1/contract/ticker", params={"symbol":mexc_symbol(symbol)})
        d = (t.get("data") or [{}])[0] if isinstance(t.get("data"), list) else t.get("data",{})
        vol = float(d.get("turnover24h") or d.get("amount24") or 0)
        return vol, float(a), float(b)

    vol_s, ask_s, bid_s = liq(short_ex)
    vol_l, ask_l, bid_l = liq(long_ex)

    # 価格乖離(bps)
    px_s = get_mark(short_ex, symbol)
    px_l = get_mark(long_ex,  symbol)
    gap_bps = abs(px_s - px_l) / max(px_s, px_l, 1e-9) * 10_000

    def tier_score(x, tiers):
        return 4 if x>=tiers[0] else 3 if x>=tiers[1] else 2 if x>=tiers[2] else 1 if x>=tiers[3] else 0

    vol_score = min(tier_score(vol_s, VOL_TIERS), tier_score(vol_l, VOL_TIERS))
    bbo_score = min(tier_score(min(ask_s,bid_s), BBO_TIERS), tier_score(min(ask_l,bid_l), BBO_TIERS))

    gap_pen = 0
    for th, pen in GAP_BPS_PENALTY:
        if gap_bps > th:
            gap_pen = pen
            break

    apr_adj = 0
    for th, adj in APR_BONUS:
        if apr >= th:
            apr_adj = adj
            break
        elif apr < 80:
            apr_adj = -2

    total = vol_score + bbo_score + gap_pen + apr_adj
    rank = "S" if total>=7 else "A" if total>=5 else "B" if total>=3 else "C" if total>=1 else "D"

    return {
        "rank": rank,
        "score": total,
        "metrics": {
            "apr": apr, "diff": diff, "iv_min": iv,
            "vol_short": vol_s, "vol_long": vol_l,
            "bbo_short_min": min(ask_s,bid_s), "bbo_long_min": min(ask_l,bid_l),
            "gap_bps": gap_bps
        }
    }

# ---------- Discord UI ----------
class EntryModal(discord.ui.Modal, title="エントリー登録（数字4つだけ）"):
    def __init__(self, symbol: str, short_ex: str, long_ex: str):
        super().__init__(timeout=300)
        self.symbol = symbol
        self.short_ex = short_ex
        self.long_ex = long_ex
        # 数字4つだけ
        self.add_item(discord.ui.TextInput(label=f"{short_ex} ショート建値", placeholder="例: 65000", required=True))
        self.add_item(discord.ui.TextInput(label=f"{short_ex} ショートロット(USDT)", placeholder="例: 10000", required=True))
        self.add_item(discord.ui.TextInput(label=f"{long_ex} ロング建値", placeholder="例: 64950", required=True))
        self.add_item(discord.ui.TextInput(label=f"{long_ex} ロングロット(USDT)", placeholder="例: 10000", required=True))

    async def on_submit(self, interaction: discord.Interaction):
        if not only_target_channel(interaction):
            await interaction.response.send_message("❌ frアビトラ専用チャンネルでのみ使用できます。", ephemeral=True)
            return
        try:
            spx = float(self.children[0].value)
            snt = float(self.children[1].value)
            lpx = float(self.children[2].value)
            lnt = float(self.children[3].value)
        except ValueError:
            await interaction.response.send_message("⚠️ 数字を正しく入力してください。", ephemeral=True)
            return

        positions = load_json(POSITIONS_FILE, {})
        key = f"{self.symbol}|{self.short_ex}-Short|{self.long_ex}-Long"
        positions[key] = {
            "symbol": self.symbol,
            "short_ex": self.short_ex,
            "long_ex": self.long_ex,
            "avg_entry_short_px": spx,
            "avg_entry_long_px": lpx,
            "notional": min(snt, lnt),  # 小さい方で合わせる
            "taker_short": taker_for(self.short_ex),
            "taker_long": taker_for(self.long_ex),
            "entry_slip_frac": ENTRY_SLIP,
            "intervals_received": 0
        }
        save_json(POSITIONS_FILE, positions)

        # 初回の理論値計算
        iv = symbol_interval_minutes(self.symbol)
        fr_short = fetch_fr_for_exchange(self.short_ex, self.symbol)
        fr_long  = fetch_fr_for_exchange(self.long_ex,  self.symbol)
        diff = max(0.0, fr_short - fr_long)  # 受取方向
        apr = calc_apr(diff, iv)
        per_gain = diff * positions[key]["notional"]
        fees = (positions[key]["taker_short"] + positions[key]["taker_long"] + positions[key]["entry_slip_frac"]) * positions[key]["notional"]
        be_intervals = math.ceil(fees / per_gain) if per_gain > 0 else 10**9

        embed = discord.Embed(
            title=f"登録: {self.symbol} | {self.short_ex}-Short / {self.long_ex}-Long",
            color=0x55CC66
        )
        embed.add_field(name="初期APR", value=f"**{fmt_pct(apr,1)}** (ΔFR {(diff*100):.3f}% / {iv}min)")
        embed.add_field(name="推定受取/回", value=fmt_usd(per_gain))
        embed.add_field(name="損益分岐", value=f"{be_intervals} intervals")
        embed.add_field(name="ノーション", value=fmt_usd(positions[key]["notional"]))
        embed.set_footer(text=to_jst_str(now_utc()))
        await interaction.response.send_message(embed=embed, ephemeral=True)

        # 登録と同時に精査ランクも通知（公開チャンネルに出す）
        eva = evaluate_liquidity_and_rank(self.symbol, self.short_ex, self.long_ex)
        rank = eva["rank"]; score = eva["score"]
        d_bps = eva["metrics"]["diff"] * 10_000
        color = {"S":0x00C853, "A":0x55CC66, "B":0xE7C000, "C":0xE67E22, "D":0xCC3333}.get(rank, 0x3388cc)

        rank_embed = discord.Embed(
            title=f"🔎 精査完了 | {self.symbol} {self.short_ex}-Short / {self.long_ex}-Long",
            description=f"Rank **{rank}** (score {score})",
            color=color
        )
        rank_embed.add_field(name="APR / ΔFR", value=f"**{eva['metrics']['apr']:.1f}%** / {d_bps:.1f} bps / {eva['metrics']['iv_min']}min", inline=True)
        rank_embed.add_field(name="出来高(短/長)", value=f"{eva['metrics']['vol_short']:,.0f} / {eva['metrics']['vol_long']:,.0f}", inline=True)
        rank_embed.add_field(name="板Min(短/長)", value=f"${eva['metrics']['bbo_short_min']:,.0f} / ${eva['metrics']['bbo_long_min']:,.0f}", inline=True)
        rank_embed.add_field(name="価格乖離", value=f"{eva['metrics']['gap_bps']:.1f} bps", inline=True)
        rank_embed.set_footer(text=to_jst_str(now_utc()))
        ch = bot.get_channel(CHANNEL_ID)
        if ch:
            await ch.send(embed=rank_embed)

class EntryView(discord.ui.View):
    def __init__(self, symbol: str, short_ex: str, long_ex: str):
        super().__init__(timeout=None)
        self.symbol = symbol
        self.short_ex = short_ex
        self.long_ex = long_ex

    @discord.ui.button(label="エントリー登録", style=discord.ButtonStyle.primary, custom_id="entry_btn")
    async def entry(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not only_target_channel(interaction):
            await interaction.response.send_message("❌ このチャンネルでのみ使用できます。", ephemeral=True)
            return
        await interaction.response.send_modal(EntryModal(self.symbol, self.short_ex, self.long_ex))

class DecideView(discord.ui.View):
    def __init__(self, pos_key: str, symbol: str):
        super().__init__(timeout=600)
        self.pos_key = pos_key
        self.symbol = symbol

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if not only_target_channel(interaction):
            await interaction.response.send_message("❌ frアビトラ専用チャンネルでのみ使用できます。", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="✅ クローズ", style=discord.ButtonStyle.danger, custom_id="close_btn")
    async def close_pos(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not (await self._guard(interaction)): return
        positions = load_json(POSITIONS_FILE, {})
        if positions.pop(self.pos_key, None) is None:
            await interaction.response.send_message("⚠️ 既に削除済み、または見つかりません。", ephemeral=True)
            return
        save_json(POSITIONS_FILE, positions)
        await interaction.response.send_message(f"✅ クローズ登録: {self.symbol}（記録削除→通知停止）", ephemeral=True)

    @discord.ui.button(label="🟢 キープ", style=discord.ButtonStyle.success, custom_id="keep_btn")
    async def keep_pos(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not (await self._guard(interaction)): return
        positions = load_json(POSITIONS_FILE, {})
        p = positions.get(self.pos_key)
        if not p:
            await interaction.response.send_message("⚠️ 記録が見つかりません。", ephemeral=True)
            return
        p["keep_flag"] = True
        p["keep_timestamp"] = now_utc().isoformat()
        save_json(POSITIONS_FILE, positions)
        await interaction.response.send_message("👌 キープ登録（監視継続）", ephemeral=True)

# ===== スラコマ：登録用カードを出す（数字4つだけ入力する流れ）
@tree.command(name="entry_setup", description="登録用カードを出す（ボタン→数字4つ入力）")
@app_commands.describe(symbol="例: BTCUSDT", short_exchange="Bybit/Bitget/MEXC", long_exchange="Bybit/Bitget/MEXC")
async def entry_setup(inter: discord.Interaction, symbol: str, short_exchange: str, long_exchange: str):
    if not only_target_channel(inter):
        await inter.response.send_message("❌ frアビトラ専用チャンネルでのみ使用できます。", ephemeral=True)
        return
    symbol = symbol.upper()
    short_exchange = short_exchange.strip()
    long_exchange  = long_exchange.strip()
    if short_exchange not in ("Bybit","Bitget","MEXC") or long_exchange not in ("Bybit","Bitget","MEXC"):
        await inter.response.send_message("⚠️ 取引所は Bybit / Bitget / MEXC のみ対応です。", ephemeral=True)
        return
    embed = discord.Embed(
        title=f"{symbol} | {short_exchange}-Short / {long_exchange}-Long",
        description="ボタンを押して建値・ロット（数字4つ）だけ入力してください。",
        color=0x3388cc
    )
    embed.set_footer(text=to_jst_str(now_utc()))
    view = EntryView(symbol, short_exchange, long_exchange)
    await inter.response.send_message(embed=embed, view=view)

# ===== 監視ループ：5分ごとにFR・APR・5分前・100%割れ通知 =====
def apr_alert_cooldown_key(pos_key:str) -> str:
    return f"apr_alert|{pos_key}"

@tasks.loop(minutes=SCAN_MINUTES)
async def scan_positions():
    try:
        ch = bot.get_channel(CHANNEL_ID)
        if not ch: return
        positions = load_json(POSITIONS_FILE, {})
        cooldown  = load_json(COOLDOWN_FILE, {})

        for key, p in list(positions.items()):
            sym = p.get("symbol"); sx = p.get("short_ex"); lx = p.get("long_ex")
            if not sym or not sx or not lx: continue

            iv = symbol_interval_minutes(sym)
            m = minutes_to_next_funding(sym)

            fr_s = fetch_fr_for_exchange(sx, sym)
            fr_l = fetch_fr_for_exchange(lx, sym)
            diff = max(0.0, fr_s - fr_l)
            apr  = calc_apr(diff, iv)

            notional = float(p.get("notional", 0.0))
            per_gain = diff * notional
            fees = (taker_for(sx) + taker_for(lx) + float(p.get("entry_slip_frac", ENTRY_SLIP))) * notional
            got = int(p.get("intervals_received", 0))
            be_intervals = math.ceil(fees / per_gain) if per_gain > 0 else 10**9
            remain_be = max(0, be_intervals - got)

            # FR 5分前通知（0〜5分で一回出す）
            if 0 <= m <= 5:
                embed = discord.Embed(
                    title=f"⏰ Funding 5min before | {sym}",
                    color=0x3355cc
                )
                embed.add_field(name="推定受取/回", value=fmt_usd(per_gain) if per_gain>0 else "-", inline=True)
                embed.add_field(name="損益分岐まで", value=f"{remain_be} intervals", inline=True)
                embed.add_field(name="Interval", value=f"{iv} min", inline=False)
                hh,mm = divmod(m,60)
                embed.set_footer(text=f"next funding in {hh:02d}:{mm:02d} | {to_jst_str(now_utc())}")
                await ch.send(embed=embed, view=DecideView(key, sym))

            # APR 100%割れアラート（クールダウン30分）
            if apr < APR_MIN_ALERT:
                cdkey = apr_alert_cooldown_key(key)
                last = cooldown.get(cdkey, "")
                should = True
                if last:
                    last_dt = datetime.fromisoformat(last)
                    if (now_utc() - last_dt).total_seconds() < 30*60:
                        should = False
                if should:
                    embed = discord.Embed(
                        title=f"⚠️ APR低下 | {sym} {sx}-Short / {lx}-Long",
                        description=f"現在APR: **{fmt_pct(apr,1)}**（ΔFR {(diff*100):.3f}% / {iv}min）\nクローズ検討 or 次回受取で準備を。",
                        color=0xE7C000
                    )
                    embed.set_footer(text=to_jst_str(now_utc()))
                    await ch.send(embed=embed, view=DecideView(key, sym))
                    cooldown[cdkey] = now_utc().isoformat()

        save_json(COOLDOWN_FILE, cooldown)
    except Exception as e:
        ch = bot.get_channel(CHANNEL_ID)
        if ch: await ch.send(f"⚠️ エラー: {e}")

@bot.event
async def on_ready():
    ensure_state()
    print(f"Logged in as {bot.user} | latency {bot.latency*1000:.0f}ms")
    try:
        await tree.sync()
    except Exception:
        pass
    if not scan_positions.is_running():
        scan_positions.start()

# 起動前チェック
if __name__ == "__main__":
    ensure_state()
    if not BOT_TOKEN or not CHANNEL_ID:
        print("環境変数を設定してください: DISCORD_BOT_TOKEN / DISCORD_CHANNEL_ID")
        raise SystemExit(1)
    bot.run(BOT_TOKEN)
