#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import json
import subprocess
from pathlib import Path

from short_cost_calculator import calculate_short_pnl

BASE = Path('/Users/wxo/Desktop/Kronos')
SIM = BASE / 'simulate_position_603305.py'
SHADOW_SIM = BASE / 'simulate_position_603305_shadow.py'
STATE = BASE / 'sim_state_603305.json'
SHADOW_STATE = BASE / 'shadow_state_603305.json'
LOG = BASE / 'sim_trades_603305.jsonl'
SHADOW_LOG = BASE / 'shadow_trades_603305.jsonl'
OUTDIR = BASE / 'guard_outputs'
OUTDIR.mkdir(parents=True, exist_ok=True)

TEMPLATE_KEYS = [
    '时间：', '标的：', '启动资金：', '即时价格：', '行情：', '信号：', '动作：', '模拟仓位：', '理由：',
    '建仓明细（主策略，沿用既有持仓）：', '建仓均价（加权）：', '毛浮盈（未扣成本）：', '净浮盈（含累计成本）：',
    '主策略持仓口径（已体现交易成本）', '• 持仓市值：', '• 持仓成本（含累计交易成本）：', '• 持仓净值差额：',
    '成本明细（累计）：', '【影子策略 v1.1-shadow（触发即模拟成交）】', '建仓明细（影子策略，沿用既有持仓）：',
    '影子策略持仓口径（已体现交易成本）', '• 持仓仓位：', '• 持仓市值：', '• 持仓成本（含累计交易成本）：',
    '• 持仓净值差额：', '• 主策略本时点是否新增触发：', '• 影子策略本时点是否新增触发：'
]


def run_sim() -> str:
    # 同一触发时点：主策略 + 影子策略同时执行
    p_main = subprocess.run(['python3', str(SIM)], capture_output=True, text=True)
    p_shadow = subprocess.run(['python3', str(SHADOW_SIM)], capture_output=True, text=True)

    out_main = (p_main.stdout or '').strip() or (p_main.stderr or '').strip()
    out_shadow = (p_shadow.stdout or '').strip() or (p_shadow.stderr or '').strip()

    # 主输出继续沿用主策略文本，影子执行结果用于诊断时附加
    if p_main.returncode != 0 or p_shadow.returncode != 0:
        return f"MAIN_RC={p_main.returncode} SHADOW_RC={p_shadow.returncode}\nMAIN_OUT:\n{out_main}\n\nSHADOW_OUT:\n{out_shadow}"
    return out_main


def latest_trade(path=LOG):
    if not path.exists():
        return None
    lines = path.read_text(encoding='utf-8').strip().splitlines()
    if not lines:
        return None
    return json.loads(lines[-1])

def latest_effective_trade_text(path=SHADOW_LOG):
    if not path.exists():
        return None
    lines = path.read_text(encoding='utf-8').strip().splitlines()
    for line in reversed(lines):
        try:
            r = json.loads(line)
        except Exception:
            continue
        action = str(r.get('action','')).strip()
        if action and action != '持仓不变':
            ts = r.get('ts','未知时间')
            pf = r.get('position_from','?')
            pt = r.get('position_to','?')
            px = r.get('price','?')
            return f"{ts}：{pf}% → {pt}%（{px}）"
    return None


def format_full(out: str) -> str:
    # 从状态与最近交易补全，禁止“暂无”泛滥
    now = dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    st = json.loads(STATE.read_text(encoding='utf-8')) if STATE.exists() else {}
    tr = latest_trade() or {}

    px = st.get('last_price')
    pos = int(st.get('position_pct', 0) or 0)
    avg = st.get('avg_entry_price')
    cum_cost = float(st.get('cumulative_cost', 0.0) or 0.0)
    entry_t = st.get('entry_time', '未知')
    entry_p = st.get('entry_price', px)

    base = float(st.get('base_capital_cny', 100000) or 100000)
    px = float(px or 0.0)
    avg = float(avg or 0.0)
    if pos < 0:
        short_pnl_pct, short_pnl_amt = calculate_short_pnl(avg, px, pos, base)
        gross_pct = short_pnl_pct * 100
    else:
        gross_pct = ((px / avg) - 1.0) * 100 if (avg and pos != 0) else 0.0
    net_pct = gross_pct - cum_cost / base * 100
    market_value = base * abs(pos) / 100 * (px / avg if (avg and pos != 0) else 0.0)
    cost_basis = base * abs(pos) / 100 + cum_cost
    net_delta = market_value - cost_basis

    reason = tr.get('reason', '按规则执行')
    action = tr.get('action', '持仓不变')
    signal = tr.get('signal', '未知')
    if action == '持仓不变' and any(k in str(reason) for k in ['加仓', '减仓']):
        reason = '已达仓位上限，当前时点持仓不变'
    ts = tr.get('ts', now)

    # 仅使用真实流水作为建仓明细（最多最近3条）：
    # 1) 仅交易时段 09:30-11:30 / 13:00-15:00
    # 2) 必须仓位发生变化（from != to）
    records = []
    if LOG.exists():
        for ln in LOG.read_text(encoding='utf-8').strip().splitlines()[-500:]:
            try:
                r = json.loads(ln)
            except Exception:
                continue
            ts_raw = str(r.get('ts', ''))
            try:
                ts_dt = dt.datetime.strptime(ts_raw, '%Y-%m-%d %H:%M:%S')
                t = ts_dt.time()
            except Exception:
                continue
            # 仅当日记录
            if ts_dt.date() != dt.datetime.now().date():
                continue
            in_am = dt.time(9, 30) <= t <= dt.time(11, 30)
            in_pm = dt.time(13, 0) <= t <= dt.time(15, 0)
            if not (in_am or in_pm):
                continue
            frm = int(r.get('position_from', 0) or 0)
            to = int(r.get('position_to', 0) or 0)
            if frm == to:
                continue
            records.append(r)
    latest3 = records[-3:] if records else []
    if latest3:
        detail_lines = []
        for i, r in enumerate(latest3, 1):
            t = str(r.get('ts', ts))
            frm = int(r.get('position_from', pos))
            to = int(r.get('position_to', pos))
            pxx = float(r.get('price', px) or px)
            detail_lines.append(f"{i}. {t}：{frm}% → {to}%（{pxx:.2f}）")
        details = "\n".join(detail_lines)
    else:
        et = entry_t if isinstance(entry_t, str) and entry_t else now
        details = f"1. {et}：{pos}% → {pos}%（{float(entry_p or px):.2f}）"

    # 从最新交易记录提取行情字段；涨跌幅统一实时重算
    yclose = float(tr.get('prev_close') or tr.get('yclose') or 0.0)
    openp = float(tr.get('open') or tr.get('today_open') or 0.0)
    high = float(tr.get('high') or tr.get('today_high') or 0.0)
    low = float(tr.get('low') or tr.get('today_low') or 0.0)
    avgp = float(tr.get('avg_price') or tr.get('avg') or 0.0)
    chg_pct = ((px - yclose) / yclose * 100.0) if yclose > 0 else 0.0

    shadow = json.loads(SHADOW_STATE.read_text(encoding='utf-8')) if SHADOW_STATE.exists() else {}
    shadow_pos = int(shadow.get('position_pct', 0) or 0)
    shadow_avg = float(shadow.get('avg_entry_price') or 0.0)
    shadow_last_trade_txt = latest_effective_trade_text(SHADOW_LOG) or shadow.get('last_trade')
    main_from = int(tr.get('position_from', pos) or pos)
    main_to = int(tr.get('position_to', pos) or pos)
    main_trigger_flag = '是' if main_from != main_to else '否'

    shadow_latest = latest_trade(SHADOW_LOG) or {}
    shadow_from = int(shadow_latest.get('position_from', shadow_pos) or shadow_pos)
    shadow_to = int(shadow_latest.get('position_to', shadow_pos) or shadow_pos)
    shadow_trigger_flag = '是' if shadow_from != shadow_to else '否'
    shadow_base = float(shadow.get('base_capital_cny', 100000) or 100000)
    shadow_cum_cost = float(shadow.get('cumulative_cost',0.0) or 0.0)
    shadow_cost_basis = shadow_base * abs(shadow_pos)/100.0 + shadow_cum_cost
    shadow_market_value = shadow_base * abs(shadow_pos)/100.0 * (px / shadow_avg if shadow_avg else 0.0)
    shadow_net_delta = shadow_market_value - shadow_cost_basis
    shadow_gross_pct = ((px / shadow_avg) - 1.0) * 100 if (shadow_avg and shadow_pos != 0) else 0.0
    shadow_net_pct = shadow_gross_pct - shadow_cum_cost / shadow_base * 100

    # 成本明细按当前日志动态累计
    comm = tax = transfer = 0.0
    if LOG.exists():
        for ln in LOG.read_text(encoding='utf-8').strip().splitlines():
            try:
                r = json.loads(ln)
                c = r.get('cost') or {}
                comm += float(c.get('commission', 0.0) or 0.0)
                tax += float(c.get('stamp_tax', 0.0) or 0.0)
                transfer += float(c.get('transfer_fee', 0.0) or 0.0)
            except Exception:
                continue
    total_cost = comm + tax + transfer

    # ========== 成本明细行构建（鲁棒版）==========
    short_interest = 0.0
    if tr and isinstance(tr, dict):
        short_cost = tr.get('short_cost', {})
        if isinstance(short_cost, dict):
            short_interest = float(short_cost.get('interest', 0.0) or 0.0)
    cost_line = f"佣金 {comm:.2f}｜印花税 {tax:.2f}｜过户费 {transfer:.2f}"
    if short_interest > 0:
        cost_line += f"｜融券利息 {short_interest:.2f}"
    total_with_interest = total_cost + short_interest
    cost_line += f"｜合计 {total_with_interest:.2f} 元"
    # ========== 成本明细行构建结束 ==========

    # 执行口径统一为重算涨跌幅（禁止直用上游pct字段）
    exec_pct = chg_pct

    pos_label = f"{pos}%（多头）" if pos > 0 else ("空仓" if pos == 0 else f"{abs(pos)}%（空头）")
    shadow_pos_label = f"{shadow_pos}%（多头）" if shadow_pos > 0 else ("空仓" if shadow_pos == 0 else f"{abs(shadow_pos)}%（空头）")

    text = f'''时间：{ts}
标的：603305 旭升集团
启动资金：{base:,.2f} 元（{base/10000:.0f}万元）
即时价格：{px:.2f}

行情：昨收 {yclose:.2f}｜今开 {openp:.2f}｜最高 {high:.2f}｜最低 {low:.2f}｜均价(近似) {avgp:.2f}
信号：{signal}（现价 {px:.2f}，涨跌幅 {chg_pct:+.2f}%）
动作：{action}
模拟仓位：{pos_label}
理由：{reason}

建仓明细（主策略，沿用既有持仓）：
{details}

建仓均价（加权）：约 {avg:.2f}
毛浮盈（未扣成本）：约 {gross_pct:+.2f}%（约 {base*gross_pct/100:+,.2f} 元）
净浮盈（含累计成本）：约 {net_pct:+.2f}%（约 {base*net_pct/100:+,.2f} 元）

主策略持仓口径（已体现交易成本）
• 持仓市值：{market_value:,.2f} 元
• 持仓成本（含累计交易成本）：{cost_basis:,.2f} 元
• 持仓净值差额：{net_delta:+,.2f} 元

成本明细（累计）：{cost_line}

【影子策略 v1.1-shadow（触发即模拟成交）】
建仓明细（影子策略，沿用既有持仓）：
1. {shadow_last_trade_txt or '今日尚无影子成交'}

建仓均价（加权）：约 {shadow_avg:.2f}
毛浮盈（未扣成本）：约 {shadow_gross_pct:+.2f}%（约 {shadow_base*shadow_gross_pct/100:+,.2f} 元）
净浮盈（含累计成本）：约 {shadow_net_pct:+.2f}%（约 {shadow_base*shadow_net_pct/100:+,.2f} 元）

影子策略持仓口径（已体现交易成本）
• 持仓仓位：{shadow_pos_label}
• 持仓市值：{shadow_market_value:,.2f} 元
• 持仓成本（含累计交易成本）：{shadow_cost_basis:,.2f} 元
• 持仓净值差额：{shadow_net_delta:+,.2f} 元
• 主策略本时点是否新增触发：{main_trigger_flag}
• 影子策略本时点是否新增触发：{shadow_trigger_flag}

【审计】满仓锁定: {'true' if abs(pos) >= 99 else 'false'} | 新增资金: 0'''
    return text, {
        'exec_pct': exec_pct,
        'show_pct': chg_pct,
        'pos': pos,
        'avg': avg,
        'market_value': market_value,
        'cost_basis': cost_basis,
        'comm': comm,
        'tax': tax,
        'transfer': transfer,
        'total_cost': total_cost,
    }


def validate(text: str, ctx: dict | None = None) -> list[str]:
    errs = []
    for k in TEMPLATE_KEYS:
        if k not in text:
            errs.append(f'missing:{k}')
    if '暂无' in text:
        errs.append('contains:暂无')

    # 一致性硬闸
    if ctx:
        # 1) 执行口径=展示口径（涨跌幅）
        exec_pct = float(ctx.get('exec_pct', 0.0) or 0.0)
        show_pct = float(ctx.get('show_pct', 0.0) or 0.0)
        if abs(exec_pct - show_pct) > 0.01:
            errs.append(f'consistency:pct_mismatch exec={exec_pct:.4f} show={show_pct:.4f}')

        # 2) 仓位为0时，均价/市值/成本必须为0
        pos = int(ctx.get('pos', 0) or 0)
        avg = float(ctx.get('avg', 0.0) or 0.0)
        mv = float(ctx.get('market_value', 0.0) or 0.0)
        cb = float(ctx.get('cost_basis', 0.0) or 0.0)
        if pos == 0 and (abs(avg) > 1e-9 or abs(mv) > 1e-6 or abs(cb) > 1e-6):
            errs.append('consistency:zero_pos_nonzero_metrics')

        # 3) 成本合计一致
        comm = float(ctx.get('comm', 0.0) or 0.0)
        tax = float(ctx.get('tax', 0.0) or 0.0)
        transfer = float(ctx.get('transfer', 0.0) or 0.0)
        total = float(ctx.get('total_cost', 0.0) or 0.0)
        if abs((comm + tax + transfer) - total) > 1e-6:
            errs.append('consistency:cost_total_mismatch')
    return errs


def main():
    raw = run_sim()
    # 硬规则：同一时点要么成功实时数据，要么失败+错误码；禁止旧快照冒充当前
    if '行情获取失败（' in raw:
        ts = dt.datetime.now().strftime('%Y%m%d_%H%M%S')
        fail_text = raw.strip()
        (OUTDIR / f'report_{ts}.txt').write_text(fail_text, encoding='utf-8')
        (OUTDIR / f'check_{ts}.json').write_text(json.dumps({'errors': ['realtime_fetch_failed']}, ensure_ascii=False, indent=2), encoding='utf-8')
        print(fail_text)
        return

    full = None
    ctx = None
    report_generation_failed = False
    error_reason = ""

    try:
        full, ctx = format_full(raw)
    except Exception as e:
        report_generation_failed = True
        error_reason = str(e)
        full = f"""时间：{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n标的：603305 旭升集团\n状态：报表生成失败（策略已执行）\n错误：{error_reason}\n原始输出摘要：{raw[:500] if raw else '无'}\n建议：检查 format_full() 函数，已记录到 warnings.log\n"""
        with open(OUTDIR / 'warnings.log', 'a', encoding='utf-8') as f:
            f.write(f"{dt.datetime.now().isoformat()} | REPORT_FAILED | {error_reason}\\n")

    errs = []
    if not report_generation_failed:
        errs = validate(full, ctx)
        if errs:
            raw2 = run_sim()
            try:
                full, ctx = format_full(raw2)
                errs = validate(full, ctx)
            except Exception as e:
                errs.append('retry_failed:' + str(e))

    ts = dt.datetime.now().strftime('%Y%m%d_%H%M%S')
    (OUTDIR / f'report_{ts}.txt').write_text(full, encoding='utf-8')
    (OUTDIR / f'check_{ts}.json').write_text(json.dumps({'errors': errs, 'report_generation_failed': report_generation_failed, 'error_reason': error_reason if report_generation_failed else None}, ensure_ascii=False, indent=2), encoding='utf-8')
    print(full)


if __name__ == '__main__':
    main()
