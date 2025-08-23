# fr_arbitrage_bot.py — 最小版（固定4h・入口コストのみ）

from __future__ import annotations
from dataclasses import dataclass
from math import ceil, inf

@dataclass
class EvalResult:
    symbol: str
    interval_h: float
    diff_per_int: float
    diff_4h: float
    be_per_int: float
    be_4h: float
    apr_gross_pct: float
    apr_net_pct: float
    n_min: int
    notional_total: float
    fees_entry_usdt: float
    entry_basis_cost_usdt: float
    decision: str

def _est_vwap_slippage_cost(notional: float, vwap_slippage_bps: float) -> float:
    return float(notional) * (float(vwap_slippage_bps) / 10000.0)

def _total_entry_fees(side_notional_usdt: float,
                      fee_taker_high: float, fee_taker_low: float,
                      slip_bps_high: float, slip_bps_low: float) -> float:
    # 入口は片道（両所でエントリー）とする
    fees = side_notional_usdt * (fee_taker_high + fee_taker_low)
    slip = _est_vwap_slippage_cost(side_notional_usdt, slip_bps_high) + \
           _est_vwap_slippage_cost(side_notional_usdt, slip_bps_low)
    return fees + slip

def _estimate_entry_basis_cost_usdt(price_high: float, price_low: float,
                                    qty_high: float, qty_low: float,
                                    slip_bps_high: float, slip_bps_low: float) -> float:
    """
    高FR側=ショート想定（売り約定: price*(1 - slip)）
    低FR側=ロング想定（買い約定: price*(1 + slip)）
    買値 > 売値 の差 × マッチ数量 をコスト扱い
    """
    exp_sell = float(price_high) * (1.0 - float(slip_bps_high)/10000.0)
    exp_buy  = float(price_low)  * (1.0 + float(slip_bps_low)/10000.0)
    matched_qty = max(0.0, min(float(qty_high), float(qty_low)))
    gap = exp_buy - exp_sell
    return max(0.0, gap) * matched_qty

def evaluate_candidate_simple(
    *,
    symbol: str,
    # FR（4h等価）… runner から渡す
    fr_high_4h: float, fr_low_4h: float,
    # 価格 … runner から渡す（マーク or ミッド）
    price_high: float, price_low: float,
    # 片側名目・手数料・推定スリップ
    side_notional_usdt: float,
    fee_taker_high: float, fee_taker_low: float,
    slip_bps_high: float = 6.0, slip_bps_low: float = 6.0,
    # 判定パラメータ
    interval_h: float = 4.0,
    keep_margin_bp: float = 5.0
) -> EvalResult:
    """
    最小動作版：FR差・入口コスト・価格差コストで BE/APR/n_min を評価。
    price_high/low はそれぞれショート側/ロング側の参照価格。
    """
    # 数量は名目/価格で概算（刻み丸めはRunner側の実装へ）
    qty_high = max(0.0, float(side_notional_usdt) / float(price_high))
    qty_low  = max(0.0, float(side_notional_usdt) / float(price_low))

    # 入口コスト（手数料+スリップ）と価格差コスト
    fees_entry = _total_entry_fees(side_notional_usdt, fee_taker_high, fee_taker_low, slip_bps_high, slip_bps_low)
    basis_cost = _estimate_entry_basis_cost_usdt(price_high, price_low, qty_high, qty_low, slip_bps_high, slip_bps_low)

    notional_total = side_notional_usdt * 2.0
    total_cost_4h_base = fees_entry + basis_cost

    # FR差・BE
    diff_4h = abs(float(fr_high_4h) - float(fr_low_4h))
    diff_per_int = diff_4h * (interval_h / 4.0)
    be_4h = (total_cost_4h_base / notional_total) if notional_total > 0 else 9e9
    be_per_int = be_4h * (interval_h / 4.0)

    # APR等
    annual_mult = (24.0 / interval_h) * 365.0
    apr_gross_pct = max(0.0, diff_per_int * annual_mult * 100.0)
    apr_net_pct   = max(0.0, (diff_per_int - be_per_int) * annual_mult * 100.0)
    n_min = int(ceil(total_cost_4h_base / (notional_total * diff_per_int))) if diff_per_int > 0 else int(inf)

    # 判定
    margin_bps = (diff_per_int - be_per_int) * 10000.0
    if margin_bps >= keep_margin_bp:
        decision = "keep"
    elif margin_bps >= 0.0:
        decision = "watch"
    else:
        decision = "close"

    return EvalResult(
        symbol=symbol,
        interval_h=interval_h,
        diff_per_int=diff_per_int,
        diff_4h=diff_4h,
        be_per_int=be_per_int,
        be_4h=be_4h,
        apr_gross_pct=apr_gross_pct,
        apr_net_pct=apr_net_pct,
        n_min=n_min,
        notional_total=notional_total,
        fees_entry_usdt=fees_entry,
        entry_basis_cost_usdt=basis_cost,
        decision=decision
    )

def build_simple_notification(ev: EvalResult) -> str:
    lines = []
    lines.append(f"[FRアビトラ候補] {ev.symbol} | 区間 {ev.interval_h:.0f}h")
    lines.append(f"差分: {ev.diff_per_int*100:.3f}%/区間 (4h等価 {ev.diff_4h*100:.3f}%)")
    lines.append(f"BE: {ev.be_per_int*100:.3f}%/区間")
    lines.append(f"APR: gross {ev.apr_gross_pct:.1f}% / net {ev.apr_net_pct:.1f}%")
    lines.append(f"黒字目安: {ev.n_min} 回")
    lines.append(f"費用: 手数料+スリップ {ev.fees_entry_usdt:.2f} USDT / 価格差 {ev.entry_basis_cost_usdt:.2f} USDT")
    lines.append(f"判定: {ev.decision.upper()}  (基準: 差分−BE ≥ 5bp)")
    return "\n".join(lines)

    
