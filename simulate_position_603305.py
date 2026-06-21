#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from signal_router_603305 import fetch_eastmoney
from short_cost_calculator import calculate_short_sell_cost, calculate_short_pnl

STOCK_CODE = "603305"
STOCK_NAME = "旭升集团"

BASE_DIR = Path(__file__).resolve().parent
STATE_PATH = BASE_DIR / "sim_state_603305.json"
LOG_PATH = BASE_DIR / "sim_trades_603305.jsonl"
LOG_DAILY_DIR = BASE_DIR / "sim_logs_daily"
COSTS_PATH = BASE_DIR / "sim_costs_603305.json"
SIM_RULES_PATH = BASE_DIR / "simulate_rules_603305.json"

DEFAULT_STATE = {
    "symbol": "603305",
    "position_pct": 0,
    "last_price": None,
    "updated_at": None,
    "avg_entry_price": None,
    "entry_time": None,
    "entry_price": None,
    "peak_price_since_entry": None,
    "tp_done": {"dd1": False, "dd2": False, "dd3": False, "p1": False, "p2": False, "p3": False},
    "cumulative_cost": 0.0,
}


def load_state() -> dict:
    if not STATE_PATH.exists():
        return dict(DEFAULT_STATE)
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return dict(DEFAULT_STATE)


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def load_sim_rules() -> dict:
    defaults = {
        "rule_version": "builtin-default",
        "thresholds": {"bull_pct": 0.6, "bear_pct": -1.2},
        "position_management": {
            "max_long": 100,
            "max_short": -100,
            "neutral_when_short_cover_pct": 20,
            "bull_when_short_cover_pct": 40,
            "strong_bull_when_short_cover_pct": 40,
            "forbid_add_short_at_full_short": True,
        },
        "long_side_actions": {"bull_add_pct": 20, "strong_bull_add_pct": 30},
        "short_side_actions": {"bear_add_pct": 20, "strong_bear_add_pct": 30},
    }
    if not SIM_RULES_PATH.exists():
        return defaults
    try:
        user = json.loads(SIM_RULES_PATH.read_text(encoding="utf-8"))
        # 浅合并，满足当前结构
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(defaults.get(k), dict):
                defaults[k].update(v)
            else:
                defaults[k] = v
    except Exception:
        pass
    return defaults


def signal_and_delta(pct: float, bull: float, bear: float, curr: int, rules: dict) -> tuple[str, int, str]:
    pm = rules.get("position_management", {})
    long_cfg = rules.get("long_side_actions", {})
    short_cfg = rules.get("short_side_actions", {})

    strong_bull_add = int(long_cfg.get("strong_bull_add_pct", 30))
    bull_add = int(long_cfg.get("bull_add_pct", 20))
    strong_bear_add = int(short_cfg.get("strong_bear_add_pct", 30))
    bear_add = int(short_cfg.get("bear_add_pct", 20))

    neutral_cover = int(pm.get("neutral_when_short_cover_pct", 20))
    bull_cover = int(pm.get("bull_when_short_cover_pct", 40))
    strong_bull_cover = int(pm.get("strong_bull_when_short_cover_pct", 40))
    forbid_add_short_at_full = bool(pm.get("forbid_add_short_at_full_short", True))

    if pct >= bull * 2:
        if curr < 0:
            return "强多", +strong_bull_cover, f"强多信号：优先平空/减空{strong_bull_cover}%"
        return "强多", +strong_bull_add, f"涨幅显著高于多头阈值，按规则加仓{strong_bull_add}%"
    if pct >= bull:
        if curr < 0:
            return "偏多", +bull_cover, f"偏多信号：减空{bull_cover}%"
        return "偏多", +bull_add, f"涨幅超过多头阈值，按规则加仓{bull_add}%"
    if pct <= bear * 2:
        if forbid_add_short_at_full and curr <= -100:
            return "强空", 0, "已满空仓，禁止继续加空"
        return "强空", -strong_bear_add, f"跌幅显著超过空头阈值，按规则加空{strong_bear_add}%"
    if pct <= bear:
        if forbid_add_short_at_full and curr <= -100:
            return "偏空", 0, "已满空仓，禁止继续加空"
        return "偏空", -bear_add, f"跌幅超过空头阈值，按规则加空{bear_add}%"

    if curr < 0:
        return "中性", +neutral_cover, f"中性信号：减空{neutral_cover}%锁定利润"
    return "中性", 0, "未触发阈值，维持当前仓位"


def clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


def apply_short_risk_control(curr: int, target: int, state: dict, market: dict, rules: dict) -> tuple[int, str | None]:
    cfg = rules.get("short_risk_control", {})
    if not cfg or target >= 0:
        return target, None

    max_short = int(cfg.get("max_short", -100))
    max_add_short = int(cfg.get("max_add_short_per_trade", 30))
    target = max(target, max_short)
    if target < curr and abs(target - curr) > max_add_short:
        target = curr - max_add_short

    avg = state.get("avg_entry_price")
    base = float(state.get("base_capital_cny", 100000) or 100000)
    pnl_pct, _ = calculate_short_pnl(avg, market.get("last"), target, base)
    loss_pct = pnl_pct * 100

    reason = None
    for level in cfg.get("stop_loss_levels", []):
        th = float(level.get("loss_pct", -999))
        if loss_pct <= th:
            if level.get("action") == "强制平空":
                return 0, "空头止损：触发强制平空"
            reduce_pct = int(level.get("reduce_pct", 0))
            if reduce_pct > 0:
                target = min(0, target + reduce_pct)
                reason = f"空头止损：浮亏{loss_pct:.2f}%触发减空{reduce_pct}%"
    return target, reason


def apply_take_profit(state: dict, market: dict, curr: int, base_target: int, rules: dict) -> tuple[int, str | None]:
    tp = rules.get("take_profit", {})
    if not tp.get("enabled", False):
        return base_target, None
    if curr <= 0:
        return base_target, None

    last = float(market["last"])
    avg = state.get("avg_entry_price")
    peak = state.get("peak_price_since_entry")
    flags = state.get("tp_done", {"dd1": False, "dd2": False, "dd3": False, "p1": False, "p2": False, "p3": False})

    if not avg:
        return base_target, None
    if not peak or last > peak:
        peak = last

    profit_pct = (last / float(avg) - 1.0) * 100.0
    drawdown_pct = (1.0 - last / float(peak)) * 100.0 if peak else 0.0

    target = base_target
    reason = None

    dls = tp.get("drawdown_levels", [])
    pls = tp.get("profit_levels", [])

    if len(dls) >= 1 and (not flags.get("dd1")) and drawdown_pct >= float(dls[0].get("drawdown_pct", 1.0)):
        target = min(target, curr - int(dls[0].get("reduce_pct", 30)))
        flags["dd1"] = True
        reason = f"止盈触发：较高点回撤≥{dls[0].get('drawdown_pct')}%，减仓{dls[0].get('reduce_pct')}%"
    if len(dls) >= 2 and (not flags.get("dd2")) and drawdown_pct >= float(dls[1].get("drawdown_pct", 1.8)):
        target = min(target, curr - int(dls[1].get("reduce_pct", 30)))
        flags["dd2"] = True
        reason = f"止盈触发：较高点回撤≥{dls[1].get('drawdown_pct')}%，减仓{dls[1].get('reduce_pct')}%"
    if len(dls) >= 3 and (not flags.get("dd3")) and drawdown_pct >= float(dls[2].get("drawdown_pct", 2.5)):
        target = min(target, int(dls[2].get("reduce_to_floor_pct", 20)))
        flags["dd3"] = True
        reason = f"止盈触发：较高点回撤≥{dls[2].get('drawdown_pct')}%，仓位降至{dls[2].get('reduce_to_floor_pct')}%"

    if len(pls) >= 1 and (not flags.get("p1")) and profit_pct >= float(pls[0].get("profit_pct", 3.0)):
        target = min(target, curr - int(pls[0].get("reduce_pct", 30)))
        flags["p1"] = True
        reason = f"止盈触发：浮盈≥{pls[0].get('profit_pct')}%，减仓{pls[0].get('reduce_pct')}%"
    if len(pls) >= 2 and (not flags.get("p2")) and profit_pct >= float(pls[1].get("profit_pct", 5.0)):
        target = min(target, curr - int(pls[1].get("reduce_pct", 30)))
        flags["p2"] = True
        reason = f"止盈触发：浮盈≥{pls[1].get('profit_pct')}%，减仓{pls[1].get('reduce_pct')}%"
    if len(pls) >= 3 and (not flags.get("p3")) and profit_pct >= float(pls[2].get("profit_pct", 7.0)):
        target = min(target, curr - int(pls[2].get("reduce_pct", 20)))
        flags["p3"] = True
        reason = f"止盈触发：浮盈≥{pls[2].get('profit_pct')}%，减仓{pls[2].get('reduce_pct')}%"

    state["peak_price_since_entry"] = peak
    state["tp_done"] = flags
    return clamp(target, -100, 100), reason


def action_text(curr: int, target: int) -> str:
    if target == curr:
        return "持仓不变"

    delta = target - curr

    # 多头侧
    if curr >= 0 and target >= 0:
        return f"模拟加仓 +{delta}%" if delta > 0 else f"模拟减仓 {delta}%"

    # 空头侧（仓位为负）
    if curr <= 0 and target <= 0:
        # 更负 = 加空；更接近0 = 减空
        return f"模拟加空 {abs(delta)}%" if delta < 0 else f"模拟减空 +{delta}%"

    # 穿越0轴
    if curr >= 0 and target < 0:
        return f"模拟平多并建空 {abs(target)}%"
    if curr <= 0 and target > 0:
        return f"模拟平空并建多 {target}%"

    return "持仓调整"


def load_costs() -> dict:
    defaults = {
        "commission_rate_one_way": 0.0001,
        "stamp_tax_sell_rate": 0.0005,
        "transfer_fee_rate_sh_rate": 0.00001,
        "exchange_code": "SH",
        "base_capital_cny": 100000,
    }
    if not COSTS_PATH.exists():
        return defaults
    try:
        user = json.loads(COSTS_PATH.read_text(encoding="utf-8"))
        defaults.update(user)
    except Exception:
        pass
    return defaults


def calc_trade_cost(curr: int, target: int, price: float, costs: dict) -> dict:
    trade_pct = abs(target - curr) / 100.0
    notional = float(costs.get("base_capital_cny", 100000)) * trade_pct
    commission = notional * float(costs.get("commission_rate_one_way", 0.0001))
    transfer_fee = notional * float(costs.get("transfer_fee_rate_sh_rate", 0.00001)) if costs.get("exchange_code") == "SH" else 0.0
    stamp_tax = 0.0
    # 卖出（减多、加空、平多建空）计印花税
    if target < curr:
        stamp_tax = notional * float(costs.get("stamp_tax_sell_rate", 0.0005))
    total = commission + transfer_fee + stamp_tax
    return {
        "trade_notional": notional,
        "commission": commission,
        "transfer_fee": transfer_fee,
        "stamp_tax": stamp_tax,
        "total_cost": total,
    }


def main() -> None:
    rules = load_sim_rules()
    bull = float(rules["thresholds"].get("bull_pct", 0.6))
    bear = float(rules["thresholds"].get("bear_pct", -1.2))
    rule_version = str(rules.get("rule_version", "builtin-default"))

    market, err, dbg = fetch_eastmoney(symbol="603305", retries=2)
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if market is None:
        print(f"时间：{now}")
        print(f"标的：{STOCK_CODE} {STOCK_NAME}")
        print("信号：数据异常")
        print("动作：不调仓")
        print("模拟仓位：保持不变")
        print(f"理由：行情获取失败（{err}），按风控规则观望")
        print(f"error_code：{dbg.get('error_code','')}")
        print(f"fetch_url：{dbg.get('fetch_url','')}")
        print(f"retry_count：{dbg.get('retry_count','')}")
        print(f"raw_length：{dbg.get('raw_length','')}")
        print(f"missing_fields：{','.join(dbg.get('missing_fields',[])) if dbg.get('missing_fields') else '-'}")
        print(f"parse_stage：{dbg.get('parse_stage','')}")
        print(f"provider_primary：{dbg.get('provider_primary','')}")
        print(f"primary_url：{dbg.get('primary_url','')}")
        print(f"primary_error_code：{dbg.get('primary_error_code','')}")
        print(f"primary_raw_length：{dbg.get('primary_raw_length','')}")
        print(f"provider_fallback：{dbg.get('provider_fallback','')}")
        print(f"fallback_url：{dbg.get('fallback_url','')}")
        print(f"fallback_error_code：{dbg.get('fallback_error_code','')}")
        print(f"fallback_raw_length：{dbg.get('fallback_raw_length','')}")
        print(f"provider_fallback_used：{str(dbg.get('provider_fallback_used', False)).lower()}")
        print(f"provider_third：{dbg.get('provider_third','')}")
        print(f"third_url：{dbg.get('third_url','')}")
        print(f"third_error_code：{dbg.get('third_error_code','')}")
        print(f"third_raw_length：{dbg.get('third_raw_length','')}")
        print(f"third_result：{dbg.get('third_result','')}")
        print(f"provider_final：{dbg.get('provider_final','')}")
        print(f"final_error_code：{dbg.get('final_error_code', dbg.get('error_code',''))}")
        return

    state = load_state()
    costs = load_costs()
    curr = int(state.get("position_pct", 0))

    calc_pct = ((float(market["last"]) - float(market["prev_close"])) / float(market["prev_close"]) * 100.0) if float(market.get("prev_close") or 0) > 0 else 0.0
    market["pct"] = calc_pct
    sig, delta, reason = signal_and_delta(market["pct"], bull, bear, curr, rules)
    target = clamp(curr + delta, -100, 100)
    target, tp_reason = apply_take_profit(state, market, curr, target, rules)
    if tp_reason:
        reason = tp_reason
    target, short_rc_reason = apply_short_risk_control(curr, target, state, market, rules)
    if short_rc_reason:
        reason = short_rc_reason
    action = action_text(curr, target)
    if target == curr:
        action = '持仓不变'
        if any(k in str(reason) for k in ['加仓', '减仓']):
            reason = '已达仓位上限，强多信号不再加仓'
    fee = calc_trade_cost(curr, target, market["last"], costs)
    # ========== 融券成本计算（仅做空时启用）==========
    short_days = int(state.get("short_holding_days", 0))
    if target < 0:
        turnover = abs((target - curr) / 100.0 * float(costs.get("base_capital_cny", 100000)))
        short_rate = float(rules.get("short_interest_rate", state.get("short_interest_rate", 0.10)))
        short_costs = calculate_short_sell_cost(turnover=turnover, days_held=short_days, interest_rate=short_rate)
    else:
        short_costs = None
    # ========== 融券成本计算结束 ==========

    # 更新持仓均价/高点
    if curr == 0 and target > 0:
        state["avg_entry_price"] = market["last"]
        state["entry_price"] = market["last"]
        state["entry_time"] = now
        state["peak_price_since_entry"] = market["last"]
        state["tp_done"] = {"dd1": False, "dd2": False, "dd3": False, "p1": False, "p2": False, "p3": False}
        if curr == 0:
            state["cumulative_cost"] = 0.0
    elif curr > 0 and target > curr:
        old_avg = float(state.get("avg_entry_price") or market["last"])
        add = target - curr
        new_avg = (old_avg * curr + market["last"] * add) / target
        state["avg_entry_price"] = new_avg
        state["entry_price"] = market["last"]
        state["entry_time"] = now
        state["peak_price_since_entry"] = max(float(state.get("peak_price_since_entry") or market["last"]), market["last"])
    elif target <= 0:
        state["avg_entry_price"] = None
        state["entry_price"] = None
        state["entry_time"] = None
        state["peak_price_since_entry"] = None
        state["tp_done"] = {"dd1": False, "dd2": False, "dd3": False, "p1": False, "p2": False, "p3": False}
    else:
        state["peak_price_since_entry"] = max(float(state.get("peak_price_since_entry") or market["last"]), market["last"])

    if target != curr:
        state["cumulative_cost"] = float(state.get("cumulative_cost", 0.0)) + float(fee.get("total_cost", 0.0))

    if target < 0:
        state["short_holding_days"] = int(state.get("short_holding_days", 0)) + 1
    else:
        state["short_holding_days"] = 0
    state["short_interest_rate"] = float(rules.get("short_interest_rate", state.get("short_interest_rate", 0.10)))
    state["position_pct"] = target
    state["last_price"] = market["last"]
    state["updated_at"] = now
    save_state(state)

    avg_price = (market["open"] + market["high"] + market["low"] + market["last"]) / 4.0

    cross_zero = (curr > 0 and target < 0) or (curr < 0 and target > 0)
    cross_zero_action = None
    if cross_zero:
        cross_zero_action = f"多空穿越（{curr}% → {target}%）；先平{'多' if curr > 0 else '空'}仓{abs(curr)}%，再建{'空' if target < 0 else '多'}仓{abs(target)}%"

    long_pnl = 0.0
    short_pnl = 0.0
    if state.get("avg_entry_price") and target != 0:
        if target > 0:
            long_pnl = (market["last"] - float(state.get("avg_entry_price"))) / float(state.get("avg_entry_price"))
        else:
            short_pnl, _amt = calculate_short_pnl(float(state.get("avg_entry_price")), market["last"], target, float(costs.get("base_capital_cny", 100000)))

    record = {
        "short_cost": short_costs,
        "ts": now,
        "price": market["last"],
        "pct": market["pct"],
        "prev_close": market.get("prev_close"),
        "open": market.get("open"),
        "high": market.get("high"),
        "low": market.get("low"),
        "avg_price": avg_price,
        "signal": sig,
        "action": action,
        "position_from": curr,
        "position_to": target,
        "reason": reason,
        "cross_zero": cross_zero,
        "cross_zero_from": curr if cross_zero else None,
        "cross_zero_to": target if cross_zero else None,
        "cross_zero_action": cross_zero_action,
        "performance": {
            "long_pnl": round(long_pnl, 6),
            "short_pnl": round(short_pnl, 6),
            "cross_zero_pnl": round((long_pnl + short_pnl) if cross_zero else 0.0, 6),
            "short_max_drawdown": 0.0,
            "short_cost_ratio": round((float(short_costs.get("total", 0.0)) / float(fee.get("trade_notional", 1.0))) if short_costs and fee.get("trade_notional", 0.0) else 0.0, 6),
        },
        "cost": fee,
    }
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    LOG_DAILY_DIR.mkdir(parents=True, exist_ok=True)
    daily_log = LOG_DAILY_DIR / f"sim_trades_603305_{dt.datetime.now().strftime('%Y-%m-%d')}.jsonl"
    with daily_log.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    print(f"时间：{now}")
    print(f"标的：{STOCK_CODE} {STOCK_NAME}")
    print(
        f"行情：昨收 {market['prev_close']:.2f}｜今开 {market['open']:.2f}｜最高 {market['high']:.2f}｜最低 {market['low']:.2f}｜均价(近似) {avg_price:.2f}"
    )
    print(f"信号：{sig}（现价 {market['last']:.2f}，涨跌幅 {market['pct']:+.2f}%）")
    print(f"动作：{action}")
    side = "空仓" if target < 0 else ("多仓" if target > 0 else "空仓(0%)")
    print(f"模拟仓位：{curr}% -> {target}%（{side}）")
    entry_time = state.get("entry_time")
    entry_price = state.get("entry_price")
    if entry_time and entry_price:
        print(f"建仓时间：{entry_time}")
        print(f"建仓价格：{float(entry_price):.2f}")
    else:
        print("建仓时间：暂无")
        print("建仓价格：暂无")

    avg_entry = state.get("avg_entry_price")
    if avg_entry and target != 0:
        gross_pct = (market["last"] / float(avg_entry) - 1.0) * 100.0
        direction = 1 if target > 0 else -1
        gross_pct = gross_pct * direction
        base_capital = float(costs.get("base_capital_cny", 100000))
        cum_cost = float(state.get("cumulative_cost", 0.0))
        net_pct = gross_pct - (cum_cost / base_capital * 100.0)
        print(f"当前浮盈（含成本）：{net_pct:+.2f}%（持仓均价 {float(avg_entry):.2f}，累计成本 {cum_cost:.2f} 元）")
    else:
        print("当前浮盈（含成本）：0.00%（当前空仓）")

    print(f"理由：{reason}")
    print(f"规则版本：{rule_version}")
    print(
        f"成本：佣金 {fee['commission']:.2f}｜印花税 {fee['stamp_tax']:.2f}｜过户费 {fee['transfer_fee']:.2f}｜合计 {fee['total_cost']:.2f} 元"
    )
    if short_costs is not None:
        print(f"融券成本：利息 {short_costs['interest']:.2f}｜佣金 {short_costs['commission']:.2f}｜印花税 {short_costs['stamp_tax']:.2f}｜过户费 {short_costs['transfer_fee']:.2f}｜合计 {short_costs['total']:.2f} 元")
    print(f"provider_primary：{dbg.get('provider_primary','')}")
    print(f"primary_url：{dbg.get('primary_url','')}")
    print(f"primary_error_code：{dbg.get('primary_error_code','')}")
    print(f"primary_raw_length：{dbg.get('primary_raw_length','')}")
    print(f"provider_fallback：{dbg.get('provider_fallback','')}")
    print(f"fallback_url：{dbg.get('fallback_url','')}")
    print(f"fallback_error_code：{dbg.get('fallback_error_code','')}")
    print(f"fallback_raw_length：{dbg.get('fallback_raw_length','')}")
    print(f"provider_fallback_used：{str(dbg.get('provider_fallback_used', False)).lower()}")
    print(f"provider_third：{dbg.get('provider_third','')}")
    print(f"third_url：{dbg.get('third_url','')}")
    print(f"third_error_code：{dbg.get('third_error_code','')}")
    print(f"third_raw_length：{dbg.get('third_raw_length','')}")
    print(f"third_result：{dbg.get('third_result','')}")
    print(f"provider_final：{dbg.get('provider_final','')}")
    print(f"final_error_code：{dbg.get('final_error_code', dbg.get('error_code','OK'))}")


if __name__ == "__main__":
    main()
