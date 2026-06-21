#!/usr/bin/env python3
"""融券成本计算器 - 仅当 position_pct < 0 时调用"""


def calculate_short_pnl(entry_price: float, current_price: float, position_pct: float, base_capital: float) -> tuple[float, float]:
    """计算空头浮动盈亏（收益率, 金额）"""
    if position_pct >= 0:
        return 0.0, 0.0
    if not entry_price or entry_price <= 0 or not current_price or current_price <= 0:
        return 0.0, 0.0

    abs_pos_pct = abs(float(position_pct))
    notional = float(base_capital) * abs_pos_pct / 100.0
    pnl_pct = (float(entry_price) - float(current_price)) / float(entry_price)
    pnl_amount = notional * pnl_pct
    return pnl_pct, pnl_amount

def calculate_short_sell_cost(
 turnover: float,
 days_held: int,
 interest_rate: float = 0.10,
 commission_rate: float = 0.0003,
 stamp_tax_rate: float = 0.0005
) -> dict:
 stamp_tax = turnover * stamp_tax_rate
 commission = max(turnover * commission_rate, 5.0)
 transfer_fee = turnover * 0.00001
 interest = (turnover * interest_rate / 360) * max(days_held, 0)
 total = stamp_tax + commission + transfer_fee + interest
 return {
 "stamp_tax": round(stamp_tax, 2),
 "commission": round(commission, 2),
 "transfer_fee": round(transfer_fee, 2),
 "interest": round(interest, 2),
 "total": round(total, 2),
 "_comment": "融券利息（持有期间累计），仅在 position_pct < 0 时存在"
 }
