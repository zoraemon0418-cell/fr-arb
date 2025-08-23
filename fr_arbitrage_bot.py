# fr_arbitrage_bot.py （成行のクロス価格差コストをBE/推定損益に反映する版）
# 変更点の要約：
# - estimate_entry_basis_cost_usdt(): 成行想定の売値/買値から「クロス価格差コスト」を算出
# - evaluate_candidate(): 価格差コストを fees_total に加算して BE/APR/n_min を評価
# - Position に entry_basis_cost_usdt を追加して保存
# - evaluate_position_now(): BE/推定損益で price差コストを考慮（FR差の粗利益 − 手数料/スリップ − 価格差コスト）
# - 通知ビルダーに“価格差コスト”の表示を追加（任意）

from __future__ import annotations
from dataclasses import dataclass
from typing import List, Dict, Tuple, Optional, Protocol
from datetime import datetime, timezone, timedelta
from math import ceil, inf

# ============================================================
# 取引所アダプタ & レジストリ
# ============================================================

class ExchangeAdapter(Protocol):
    @property
    def name(self) -> str: ...
    def fetch_available_balance_usdt(self) -> float: ...
    def fetch_fr_4h(self, symbol: str, mode: str) -> float: ...
    def fetch_mark_price(self, symbol: str) -> float: ...
    def round_qty_to_lot(self, symbol: str, qty: float) -> float: ...
    def place_market_order(self, symbol: str, side: str, qty: float, reduce_only: bool = False) -> Dict: ...
    def set_leverage(self, symbol: str, leverage: float) -> None: ...

_EXCHANGE_REGISTRY: Dict[str, ExchangeAdapter] = {}

def register_exchange_adapter(adapter: ExchangeAdapter) -> None:
    if not adapter.name:
        raise ValueError("adapter.name が空です。")
    _EXCHANGE_REGISTRY[adapter.name] = adapter

def get_adapter(name: str) -> ExchangeAdapter:
    try:
        return _EXCHANGE_REGISTRY[name]
    except KeyError:
        raise ValueError(f"未登録の取引所: {name}. register_exchange_adapter(...) を呼んでください。")

# ============================================================
# データ構造
# ============================================================

@dataclass
class Leg:
    exchange: str
    symbol: str
    side: str                  # "long" or "short"
    notional: float            # 片側名目(USDT)
    qty: float                 # ベース数量（刻み丸め後）
    mark_price: float
    fee_rate_taker: float      # 例: 0.0006 (=6bps)
    vwap_slippage_est_bps: float

@dataclass
class PairFR:
    fr_a_4h: float
    fr_b_4h: float
    diff_4h: float

@dataclass
class Position:
    leg_a: Leg
    leg_b: Leg
    entry_ts: datetime
    entry_diff_4h: float
    leverage: float
    next_funding_ts: datetime
    interval_h: float
    fees_total_usdt: float           # 往復手数料+往復スリッページ
    entry_basis_cost_usdt: float     # ★ 成行のクロス価格差コスト（保守的にコスト扱い）
    notional_total: float
    entry_be_per_int: float
    entry_n_min: int

@dataclass
class EvalResult:
    symbol: str
    high_fr_ex: str
    low_fr_ex: str
    interval_h: float
    diff_per_int: float
    diff_4h: float
    be_per_int: float
    be_4h: float
    apr_gross_pct: float
    apr_net_pct: float
    n_min: int
    notional_total: float
    fees_total_usdt: float
    entry_basis_cost_usdt: float      # ★ 価格差コスト
    decision: str

@dataclass
class PositionStatus:
    symbol: str
    short_ex: str
    long_ex: str
    interval_h: float
    diff_per_int_now: float
    diff_4h_now: float
    be_per_int_now: float
    be_4h_now: float
    apr_gross_now_pct: float
    apr_net_now_pct: float
    n_min_now: int
    diff_per_int_entry: float
    apr_gross_entry_pct: float
    n_min_entry: int
    diff_delta_bp: float
    apr_gross_delta_pct: float
    n_min_delta: int
    est_pnl_usdt: float
    entry_ts: datetime
    now_ts: datetime
    entry_basis_cost_usdt: float      # ★ 表示用

# ============================================================
# 取引所アクセス（レジストリ経由のラッパ）
# ============================================================

def fetch_available_balance_usdt(exchange: str) -> float:
    return get_adapter(exchange).fetch_available_balance_usdt()

def fetch_fr_4h(exchange: str, symbol: str, mode: str) -> float:
    return get_adapter(exchange).fetch_fr_4h(symbol, mode)

def fetch_mark_price(exchange: str, symbol: str) -> float:
    return get_adapter(exchange).fetch_mark_price(symbol)

def round_qty_to_lot(exchange: str, symbol: str, qty: float) -> float:
    return get_adapter(exchange).round_qty_to_lot(symbol, qty)

def place_market_order(exchange: str, symbol: str, side: str, qty: float, reduce_only: bool = False) -> Dict:
    return get_adapter(exchange).place_market_order(symbol, side, qty, reduce_only)

def set_leverage(exchange: str, symbol: str, leverage: float) -> None:
    return get_adapter(exchange).set_leverage(symbol, leverage)

# ============================================================
# 計算ユーティリティ
# ============================================================

def est_vwap_slippage_cost(notional: float, vwap_slippage_bps: float) -> float:
    return notional * (vwap_slippage_bps / 10000.0)

def be_fr_4h(fees_total_usdt: float, notional_total_usdt: float) -> float:
    if notional_total_usdt <= 0:
        return 9e9
    return fees_total_usdt / notional_total_usdt

def total_fees_with_slippage(leg_a: Leg, leg_b: Leg) -> float:
    fees = (leg_a.notional * leg_a.fee_rate_taker + leg_b.notional * leg_b.fee_rate_taker) * 2
    slip_a = est_vwap_slippage_cost(leg_a.notional, leg_a.vwap_slippage_est_bps) * 2
    slip_b = est_vwap_slippage_cost(leg_b.notional, leg_b.vwap_slippage_est_bps) * 2
    return fees + slip_a + slip_b

def estimate_entry_basis_cost_usdt(
    short_px_ref: float, long_px_ref: float,
    qty_short_base: float, qty_long_base: float,
    slip_bps_short: float, slip_bps_long: float
) -> float:
    """
    成行で想定される「売値/買値」を作り、クロス価格差（買いが売りより高ければコスト）をUSDTで見積もる。
    - 売り(ショート)は price*(1 - slip)
    - 買い(ロング) は price*(1 + slip)
    - ベース数量は“マッチした方”（min）だけ評価（端数は無視）
    """
    exp_sell = short_px_ref * (1.0 - slip_bps_short / 10000.0)
    exp_buy  = long_px_ref  * (1.0 + slip_bps_long  / 10000.0)
    matched_qty = max(0.0, min(qty_short_base, qty_long_base))      # ベース数量
    gap = exp_buy - exp_sell                                        # >0: 不利（コスト）
    return max(0.0, gap) * matched_qty

def estimate_pnl_now(
    notional_total: float,
    diff_now_4h: float,
    entry_ts: datetime,
    now: datetime,
    leg_fees_total: float,
    entry_basis_cost_usdt: float = 0.0
) -> float:
    """
    推定損益（USDT）。FR差の粗利益 − (手数料+スリッページ+価格差コスト)。
    経過4h本数 = 経過時間(時間)/4
    ※ 価格変動PnLは評価しない保守的推定
    """
    periods = max(0.0, (now - entry_ts).total_seconds() / 3600.0 / 4.0)
    gross = notional_total * diff_now_4h * periods
    return gross - (leg_fees_total + entry_basis_cost_usdt)

# ============================================================
# スクリーニング/ユーティリティ
# ============================================================

def balance_imbalance_alert(bybit_avail: float, bitget_avail: float, threshold_ratio: float = 0.10) -> Tuple[bool, float]:
    avg = (bybit_avail + bitget_avail) / 2.0
    if avg <= 0:
        return False, 0.0
    ratio = abs(bybit_avail - bitget_avail) / avg
    return (ratio >= threshold_ratio), ratio

def generate_dynamic_lot_options(bybit_avail: float, bitget_avail: float, step: int = 10, buffer_usdt: float = 2.0) -> List[int]:
    cap = max(0.0, min(bybit_avail, bitget_avail) - buffer_usdt)
    if cap < step:
        return [step]
    options = list(range(step, int(cap // step) * step + step, step))
    return options

# ============================================================
# 候補評価（区間対応 + 価格差コスト反映）
# ============================================================

def evaluate_candidate(
    symbol: str,
    ex_a: str, ex_b: str,
    side_notional_usdt: float,
    fee_taker_a: float, fee_taker_b: float,
    slip_bps_a: float = 6.0, slip_bps_b: float = 6.0,
    interval_h: float = 4.0,
    mode: str = "predicted",
    keep_margin_bp: float = 5.0
) -> EvalResult:

    # 1) FR(4h)
    fr_a_4h = fetch_fr_4h(ex_a, symbol, mode)
    fr_b_4h = fetch_fr_4h(ex_b, symbol, mode)

    # 2) 方向
    if fr_a_4h >= fr_b_4h:
        high_fr_ex, low_fr_ex = ex_a, ex_b
        fr_high_4h, fr_low_4h = fr_a_4h, fr_b_4h
        fee_high, fee_low = fee_taker_a, fee_taker_b
        slip_high, slip_low = slip_bps_a, slip_bps_b
    else:
        high_fr_ex, low_fr_ex = ex_b, ex_a
        fr_high_4h, fr_low_4h = fr_b_4h, fr_a_4h
        fee_high, fee_low = fee_taker_b, fee_taker_a
        slip_high, slip_low = slip_bps_b, slip_bps_a

    # 3) 価格/数量
    price_high = fetch_mark_price(high_fr_ex, symbol)  # ショート側の参照価格
    price_low  = fetch_mark_price(low_fr_ex,  symbol)  # ロング側の参照価格
    qty_high = round_qty_to_lot(high_fr_ex, symbol, max(0.0, side_notional_usdt / price_high))
    qty_low  = round_qty_to_lot(low_fr_ex,  symbol, max(0.0, side_notional_usdt / price_low))

    leg_h = Leg(high_fr_ex, symbol, "short", qty_high * price_high, qty_high, price_high, fee_high, slip_high)
    leg_l = Leg(low_fr_ex,  symbol, "long",  qty_low  * price_low,  qty_low,  price_low,  fee_low,  slip_low)

    # 4) 基本費用（往復手数料+往復スリッページ）
    fees_total = total_fees_with_slippage(leg_h, leg_l)

    # 5) ★ 成行クロス価格差コスト（買値 > 売値 になった分を保守的にコスト扱い）
    entry_basis_cost = estimate_entry_basis_cost_usdt(
        short_px_ref=price_high, long_px_ref=price_low,
        qty_short_base=qty_high, qty_long_base=qty_low,
        slip_bps_short=slip_high, slip_bps_long=slip_low
    )

    # 6) 名目・BE
    notional_total = leg_h.notional + leg_l.notional
    be_4h = be_fr_4h(fees_total + entry_basis_cost, notional_total)       # ← コスト合算
    be_per_int = be_4h * (interval_h / 4.0)

    # 7) 乖離・APR・n_min
    diff_4h = abs(fr_high_4h - fr_low_4h)
    diff_per_int = diff_4h * (interval_h / 4.0)
    annual_mult = (24.0 / interval_h) * 365.0
    apr_gross_pct = max(0.0, diff_per_int * annual_mult * 100.0)
    apr_net_pct   = max(0.0, (diff_per_int - be_per_int) * annual_mult * 100.0)
    n_min = int(ceil((fees_total + entry_basis_cost) / (notional_total * diff_per_int))) if diff_per_int > 0 else int(inf)

    # 8) 判定
    margin_bps = (diff_per_int - be_per_int) * 10000.0
    if margin_bps >= keep_margin_bp:
        decision = "keep"
    elif margin_bps >= 0.0:
        decision = "watch"
    else:
        decision = "close"

    return EvalResult(
        symbol=symbol,
        high_fr_ex=high_fr_ex,
        low_fr_ex=low_fr_ex,
        interval_h=interval_h,
        diff_per_int=diff_per_int,
        diff_4h=diff_4h,
        be_per_int=be_per_int,
        be_4h=be_4h,
        apr_gross_pct=apr_gross_pct,
        apr_net_pct=apr_net_pct,
        n_min=n_min,
        notional_total=notional_total,
        fees_total_usdt=fees_total,
        entry_basis_cost_usdt=entry_basis_cost,
        decision=decision
    )

# ============================================================
# Position を作る（エントリー時保存）
# ============================================================

def make_position_from_eval(ev: EvalResult,
                            entry_ts: datetime,
                            next_funding_ts: datetime,
                            leverage: float,
                            fee_taker_high: Optional[float] = None,
                            fee_taker_low: Optional[float] = None,
                            slip_bps_high: Optional[float] = None,
                            slip_bps_low: Optional[float] = None) -> Position:
    leg_high = Leg(exchange=ev.high_fr_ex, symbol=ev.symbol, side="short",
                   notional=ev.notional_total/2, qty=0.0, mark_price=0.0,
                   fee_rate_taker=fee_taker_high or 0.00055, vwap_slippage_est_bps=slip_bps_high or 6.0)
    leg_low  = Leg(exchange=ev.low_fr_ex,  symbol=ev.symbol, side="long",
                   notional=ev.notional_total/2, qty=0.0, mark_price=0.0,
                   fee_rate_taker=fee_taker_low or 0.00060, vwap_slippage_est_bps=slip_bps_low or 6.0)

    return Position(
        leg_a=leg_high,
        leg_b=leg_low,
        entry_ts=entry_ts,
        entry_diff_4h=ev.diff_4h,
        leverage=leverage,
        next_funding_ts=next_funding_ts,
        interval_h=ev.interval_h,
        fees_total_usdt=ev.fees_total_usdt,
        entry_basis_cost_usdt=ev.entry_basis_cost_usdt,   # ★ 保存
        notional_total=ev.notional_total,
        entry_be_per_int=ev.be_per_int,
        entry_n_min=ev.n_min
    )

# ============================================================
# 既存ポジの“いま”評価（価格差コスト込み）
# ============================================================

def evaluate_position_now(position: Position,
                          mode: str = "predicted",
                          now_ts: Optional[datetime] = None,
                          keep_margin_bp: float = 5.0) -> PositionStatus:
    now_ts = now_ts or datetime.now(timezone.utc)
    sym = position.leg_a.symbol
    ex_a = position.leg_a.exchange
    ex_b = position.leg_b.exchange

    fr_a_4h_now = fetch_fr_4h(ex_a, sym, mode)
    fr_b_4h_now = fetch_fr_4h(ex_b, sym, mode)

    if fr_a_4h_now >= fr_b_4h_now:
        short_ex, long_ex = ex_a, ex_b
        fr_high_4h_now, fr_low_4h_now = fr_a_4h_now, fr_b_4h_now
    else:
        short_ex, long_ex = ex_b, ex_a
        fr_high_4h_now, fr_low_4h_now = fr_b_4h_now, fr_a_4h_now

    diff_4h_now = abs(fr_high_4h_now - fr_low_4h_now)
    diff_per_int_now = diff_4h_now * (position.interval_h / 4.0)

    # BE（エントリー時の費用＋価格差コスト込み）
    notional_total = position.notional_total
    fees_total = position.fees_total_usdt
    basis_cost = position.entry_basis_cost_usdt
    be_4h_now = be_fr_4h(fees_total + basis_cost, notional_total)
    be_per_int_now = be_4h_now * (position.interval_h / 4.0)

    annual_mult = (24.0 / position.interval_h) * 365.0
    apr_gross_now_pct = max(0.0, diff_per_int_now * annual_mult * 100.0)
    apr_net_now_pct   = max(0.0, (diff_per_int_now - be_per_int_now) * annual_mult * 100.0)
    n_min_now = int(ceil((fees_total + basis_cost) / (notional_total * diff_per_int_now))) if diff_per_int_now > 0 else int(inf)

    # 推定損益（FR差の粗利益 − (手数料+スリップ+価格差コスト)）
    est_pnl = estimate_pnl_now(notional_total, diff_4h_now, position.entry_ts, now_ts, fees_total, entry_basis_cost_usdt=basis_cost)

    # 入口比較
    diff_per_int_entry = position.entry_diff_4h * (position.interval_h / 4.0)
    apr_gross_entry_pct = max(0.0, diff_per_int_entry * annual_mult * 100.0)
    n_min_entry = position.entry_n_min
    diff_delta_bp = (diff_per_int_now - diff_per_int_entry) * 10000.0
    apr_gross_delta_pct = apr_gross_now_pct - apr_gross_entry_pct
    n_min_delta = n_min_now - n_min_entry

    return PositionStatus(
        symbol=sym,
        short_ex=short_ex,
        long_ex=long_ex,
        interval_h=position.interval_h,
        diff_per_int_now=diff_per_int_now,
        diff_4h_now=diff_4h_now,
        be_per_int_now=be_per_int_now,
        be_4h_now=be_4h_now,
        apr_gross_now_pct=apr_gross_now_pct,
        apr_net_now_pct=apr_net_now_pct,
        n_min_now=n_min_now,
        diff_per_int_entry=diff_per_int_entry,
        apr_gross_entry_pct=apr_gross_entry_pct,
        n_min_entry=n_min_entry,
        diff_delta_bp=diff_delta_bp,
        apr_gross_delta_pct=apr_gross_delta_pct,
        n_min_delta=n_min_delta,
        est_pnl_usdt=est_pnl,
        entry_ts=position.entry_ts,
        now_ts=now_ts,
        entry_basis_cost_usdt=basis_cost
    )

# ============================================================
# 通知（候補 / 保有）
# ============================================================

def build_simple_notification(result: EvalResult,
                              now_jst: Optional[datetime] = None,
                              bybit_url: Optional[str] = None,
                              bitget_url: Optional[str] = None) -> str:
    now_jst = now_jst or datetime.now(timezone(timedelta(hours=9)))
    bybit_url = bybit_url or "https://www.bybit.com/en/trade/usdt/"
    bitget_url = bitget_url or "https://www.bitget.com/en/futures/usdt"
    lines = []
    lines.append(f"[FRアビトラ候補] {now_jst:%Y-%m-%d %H:%M} JST")
    lines.append(f"銘柄: {result.symbol}")
    lines.append(f"方向: {result.high_fr_ex} ショート(受取) / {result.low_fr_ex} ロング(支払)")
    lines.append(f"区間差分: {result.diff_per_int*100:.3f}%/区間  (4h等価: {result.diff_4h*100:.3f}%/4h)")
    lines.append(f"理論APR(グロス): {result.apr_gross_pct:.1f}%/年")
    lines.append(f"BE: {result.be_per_int*100:.3f}%/区間  ※手数料/スリップ/価格差コスト込み")
    lines.append(f"黒字化の目安: {result.n_min} 回目の更新から")
    if result.entry_basis_cost_usdt > 0:
        lines.append(f"参考: 価格差コスト ≈ {result.entry_basis_cost_usdt:.2f} USDT")
    lines.append(f"判定: {result.decision.upper()}  (基準: 差分−BE ≥ 5bp)")
    lines.append("リンク:")
    lines.append(f"• Bybit  → {bybit_url}")
    lines.append(f"• Bitget → {bitget_url}")
    return "\n".join(lines)

def build_position_notification(stat: PositionStatus,
                                bybit_url: Optional[str] = None,
                                bitget_url: Optional[str] = None) -> str:
    jst = stat.now_ts.astimezone(timezone(timedelta(hours=9)))
    bybit_url = bybit_url or "https://www.bybit.com/en/trade/usdt/"
    bitget_url = bitget_url or "https://www.bitget.com/en/futures/usdt"
    margin_bp_now = (stat.diff_per_int_now - stat.be_per_int_now) * 10000.0
    decision = "KEEP" if margin_bp_now >= 5.0 else ("WATCH" if margin_bp_now >= 0.0 else "CLOSE")
    lines = []
    lines.append(f"[保有チェック] {jst:%Y-%m-%d %H:%M} JST  （{stat.symbol} | 次のFRまで 5分想定）")
    lines.append(f"方向(現状): {stat.short_ex} ショート(受取) / {stat.long_ex} ロング(支払)")
    lines.append(f"FR差分: {stat.diff_per_int_now*100:.3f}%/区間  (入口: {stat.diff_per_int_entry*100:.3f}%/区間, Δ {stat.diff_delta_bp:+.1f} bp)")
    lines.append(f"理論APR(グロス): {stat.apr_gross_now_pct:.1f}%/年  (入口: {stat.apr_gross_entry_pct:.1f}%/年, Δ {stat.apr_gross_delta_pct:+.1f}%)")
    lines.append(f"BE: {stat.be_per_int_now*100:.3f}%/区間   余裕: {margin_bp_now:.1f} bp  ※価格差コスト込み")
    lines.append(f"黒字化の目安: n_min {stat.n_min_now}  (入口: {stat.n_min_entry}, 変化: {stat.n_min_delta:+d})")
    lines.append(f"推定損益: {stat.est_pnl_usdt:+.2f} USDT  （FR差 − 手数料/スリップ/価格差）")
    if stat.entry_basis_cost_usdt > 0:
        lines.append(f"参考: エントリー時の価格差コスト ≈ {stat.entry_basis_cost_usdt:.2f} USDT")
    lines.append(f"判定: {decision}")
    lines.append("リンク:")
    lines.append(f"• Bybit  → {bybit_url}")
    lines.append(f"• Bitget → {bitget_url}")
    return "\n".join(lines)

# ============================================================
# アダプタ雛形（必要に応じて実装して register してください）
# ============================================================

class BybitAdapter:
    @property
    def name(self) -> str: return "Bybit"
    def fetch_available_balance_usdt(self) -> float: raise NotImplementedError
    def fetch_fr_4h(self, symbol: str, mode: str) -> float: raise NotImplementedError
    def fetch_mark_price(self, symbol: str) -> float: raise NotImplementedError
    def round_qty_to_lot(self, symbol: str, qty: float) -> float: raise NotImplementedError
    def place_market_order(self, symbol: str, side: str, qty: float, reduce_only: bool = False) -> Dict: raise NotImplementedError
    def set_leverage(self, symbol: str, leverage: float) -> None: raise NotImplementedError

class BitgetAdapter:
    @property
    def name(self) -> str: return "Bitget"
    def fetch_available_balance_usdt(self) -> float: raise NotImplementedError
    def fetch_fr_4h(self, symbol: str, mode: str) -> float: raise NotImplementedError
    def fetch_mark_price(self, symbol: str) -> float: raise NotImplementedError
    def round_qty_to_lot(self, symbol: str, qty: float) -> float: raise NotImplementedError
    def place_market_order(self, symbol: str, side: str, qty: float, reduce_only: bool = False) -> Dict: raise NotImplementedError
    def set_leverage(self, symbol: str, leverage: float) -> None: raise NotImplementedError
