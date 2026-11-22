"""命令行入口：财务与业务负责人的全部操作。

通用约定
========
* ``--workdir`` 指定独立工作目录，状态互不污染；首次使用自动初始化。
* 默认输出中文表格；``--json`` 输出结构化结果，便于脚本串联。
* 业务拒绝（预算不足、账期已关、重复占用等）以退出码 2 返回并打印中文原因。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import allocation as alloc_mod
from . import disputes as dispute_mod
from . import quota as quota_mod
from . import reports
from .importer import import_sources
from .ledger import unallocated_pool
from .money import fen_str
from .store import Store, StoreError


# ----------------------------------------------------------------- 输出工具
def _emit(data: Any, as_json: bool, table=None) -> None:
    if as_json or table is None:
        print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(table)


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    widths = [len(h) for h in headers]
    str_rows = [[str(c) for c in row] for row in rows]
    for row in str_rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    sep = "  ".join("-" * widths[i] for i in range(len(headers)))
    body = ["  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row))
            for row in str_rows]
    return "\n".join([line, sep, *body])


def _fen(v: int) -> str:
    return f"{v} 分（¥{fen_str(v)}）"


# ----------------------------------------------------------------- 命令实现
def cmd_init(store: Store, args) -> None:
    store.init()
    _emit({"workdir": str(store.root), "initialized": True}, args.json,
          f"工作目录已初始化：{store.root}")


def cmd_import(store: Store, args) -> None:
    with store.transaction():
        summary = import_sources(store, args.input_dir,
                                 verify_manifest=not args.no_manifest_check)
    if summary["validation_problems"]:
        summary["ok"] = False
    else:
        summary["ok"] = True
    lines = [
        f"新导入文件：{', '.join(x['file'] for x in summary['imported']) or '无（均为重复投递）'}",
        f"重复跳过：{', '.join(summary['skipped_duplicates']) or '无'}",
        f"字段校验问题：{len(summary['validation_problems'])} 条",
        f"版本冲突：{len(summary['conflicts'])} 条（已挂起保留，见 财务待办）",
    ]
    for p in summary["validation_problems"]:
        lines.append("  校验: " + p)
    for c in summary["conflicts"]:
        lines.append(f"  冲突: {c['entity']} {c['key']} - {c['detail']}")
    _emit(summary, args.json, "\n".join(lines))


def cmd_rebuild(store: Store, args) -> None:
    store.rebuild()
    _emit({"rebuilt": True, "seq": store.snapshot["seq"]}, args.json,
          f"快照已从事件日志重建，事件序号 {store.snapshot['seq']}")


def cmd_reconcile(store: Store, args) -> None:
    data = reports.reconciliation_report(store, args.period, args.as_of)
    rows = []
    for r in data["attempts"]:
        rows.append([
            r["request"], r["attempt_id"], r["team"], r["status"],
            r["price_id"], r["estimated_fen"], r["confirmed_fen"],
            r["pending_fen"], r["variance_fen"], r["state"],
        ])
    table = _table(
        ["请求", "尝试", "团队", "结果", "价目", "预估(分)", "确认(分)",
         "待处理(分)", "差额(分)", "状态"],
        rows,
    )
    t = data["totals"]
    footer = (
        f"\n合计：预估 {_fen(t['estimated_fen'])}，确认 {_fen(t['confirmed_fen'])}，"
        f"待处理 {_fen(t['pending_fen'])}，差额 {_fen(t['variance_fen'])}\n"
        f"未分配池余额：{_fen(data['unallocated_balance_fen'])}"
    )
    _emit(data, args.json, table + footer)


def cmd_request(store: Store, args) -> None:
    data = reports.request_report(store, args.request_id, args.as_of)
    rows = [[a["attempt_id"], a["provider"], a["model"], a["status"],
             a["price_id"], a["estimated_fen"], a["confirmed_fen"],
             a["pending_fen"], a["state"]] for a in data["attempts"]]
    table = _table(["尝试", "供应商", "模型", "结果", "价目", "预估(分)",
                    "确认(分)", "待处理(分)", "状态"], rows)
    footer = (
        f"\n请求 {data['request']}（团队 {data['team']}，业务成功：{data['success']}）\n"
        f"预估合计 {_fen(data['estimated_fen'])}；已确认 {_fen(data['confirmed_fen'])}；"
        f"待处理 {_fen(data['pending_fen'])}；争议中 {_fen(data['disputed_fen'])}"
    )
    _emit(data, args.json, table + footer)


def cmd_team_cost(store: Store, args) -> None:
    data = reports.team_cost_report(store, args.period, args.as_of)
    rows = [[t["team"], t["success_units"], t["estimated_total_fen"],
             t["confirmed_usage_fen"], t["exposure_fen"],
             t["shared_allocated_fen"], t["refund_credit_fen"],
             t["unit_estimated_fen"]] for t in data["teams"]]
    table = _table(["团队", "成功请求数", "预估(分)", "确认用量(分)", "敞口(分)",
                    "共享分摊(分)", "退款返还(分)", "单位成本(分/请求)"], rows)
    lines = [table, "\n日趋势（预估分）:"]
    for t in data["teams"]:
        trend = "，".join(f"{d}:{v}" for d, v in t["daily_estimated_fen"].items())
        lines.append(f"  {t['team']}: {trend}")
    _emit(data, args.json, "\n".join(lines))


def cmd_pool(store: Store, args) -> None:
    pool = unallocated_pool(store, args.period)
    rows = [[ln["line_id"], ln["kind"], ln["currency"], ln["amount"],
             ln["fen"], ln["allocated_fen"], ln["remaining_fen"]]
            for ln in pool["lines"]]
    table = _table(["发票行", "类型", "币种", "原币金额", "折人民币(分)",
                    "已分配(分)", "未分配(分)"], rows)
    footer = f"\n未分配池余额：{_fen(pool['balance_fen'])}（不强行均摊）"
    _emit(pool, args.json, table + footer)


def cmd_propose(store: Store, args) -> None:
    weights = json.loads(args.weights) if args.weights else None
    data = alloc_mod.propose_allocation(store, args.period, args.line_id, weights)
    blocks = [f"共享费用行 {args.line_id}，总额 {_fen(data['total_fen'])}，三种口径比较："]
    for plan in data["plans"]:
        if not plan["feasible"]:
            blocks.append(f"  [{plan['basis']}] 不可行：{plan['reason']}")
            continue
        blocks.append(
            f"  [{plan['basis']}] {plan['label']}；校验和={plan['sum_check_fen']}；"
            f"确认后最低额度充裕率={plan['min_headroom_pct_after']}%")
        for p in plan["parts"]:
            blocks.append(
                f"      {p['team']}: 权重 {p['weight']} -> {_fen(p['fen'])}，"
                f"可用 {p['available_before_fen']} -> {p['available_after_fen']} 分")
    if data["current_confirmed"]:
        blocks.append("当前已确认口径：" + data["current_confirmed"]["basis"])
    _emit(data, args.json, "\n".join(blocks))


def cmd_confirm_alloc(store: Store, args) -> None:
    weights = json.loads(args.weights) if args.weights else None
    with store.transaction():
        payload = alloc_mod.confirm_allocation(
            store, args.period, args.line_id, args.basis, weights, args.operator)
    _emit(payload, args.json,
          f"已确认分配口径 {payload['basis']}（{payload['basis_label']}），"
          f"总额 {_fen(payload['total_fen'])}，分配口径已写入事件日志留痕")


def cmd_evaluate(store: Store, args) -> None:
    data = quota_mod.evaluate(store, args.request_id, args.as_of)
    req = data["request"]
    blocks = [
        f"额度请求 {req['id']}：团队 {req['team']}，批次 {req['batch']}，"
        f"批量 {req['units']}，所需能力 {req['required_capability']}，"
        f"可延迟={req['deferrable']}",
        f"成本区间：{_fen(data['cost_range_fen']['low'])} ~ "
        f"{_fen(data['cost_range_fen']['high'])}；当前可用 {_fen(data['available_fen'])}",
        f"超支风险：{data['overspend_risk']}；建议：{data['recommendation']}",
        "合规模型方案：",
    ]
    for o in data["eligible_options"]:
        blocks.append(f"  {o['model']}（能力 {o['skills']}，价目 {o['price_id']}）："
                      f"单位 {_fen(o['unit_fen'])}，合计 {_fen(o['total_fen'])}")
    _emit(data, args.json, "\n".join(blocks))


def cmd_reserve(store: Store, args) -> None:
    with store.transaction():
        outcome = quota_mod.reserve(
            store, args.request_id, model=args.model, units=args.units,
            operator=args.operator, as_of=args.as_of)
    if outcome.get("waiting"):
        text = f"已进入等待清单：{outcome['detail']['reason']}"
    elif outcome.get("idempotent_hit"):
        r = outcome["reservation"]
        text = f"重复提交：返回既有预留（{r['status']}，{_fen(r['reserved_fen'])}），未二次占用"
    else:
        r = outcome["reservation"]
        text = (f"预留已确认：批次 {r['batch']}，模型 {r['model']}，"
                f"批量 {r['units']}，预留 {_fen(r['reserved_fen'])}")
    _emit(outcome, args.json, text)


def cmd_cancel(store: Store, args) -> None:
    with store.transaction():
        payload = quota_mod.cancel(store, args.request_id, args.reason, args.operator)
    _emit(payload, args.json, f"预留 {args.request_id} 已释放：{args.reason}")


def cmd_consume(store: Store, args) -> None:
    with store.transaction():
        payload = quota_mod.consume(
            store, args.request_id, args.units, args.fen, args.note, args.operator)
    text = (f"实际消耗已登记：{_fen(payload['consumed_fen'])}，"
            f"释放预留余量 {_fen(payload['reserved_fen'] - payload['consumed_fen'])}；"
            f"成本调整行 ADJ-CONSUME-{args.request_id} 同事务落账")
    _emit(payload, args.json, text)


def cmd_expire(store: Store, args) -> None:
    with store.transaction():
        changed = quota_mod.expire_due(store, args.now)
    _emit({"expired": changed}, args.json,
          "到期释放：" + ("，".join(x["request_id"] for x in changed) or "无"))


def cmd_budget(store: Store, args) -> None:
    with store.transaction():
        outcome = quota_mod.increase_budget(
            store, args.team, args.period, args.delta_fen, args.reason, args.operator)
    text = [f"预算追加 {_fen(args.delta_fen)} 已入账"]
    for item in outcome["reevaluated"]:
        text.append(f"  重评估 {item.get('request_id')}: {item}")
    _emit(outcome, args.json, "\n".join(text))


def cmd_verify(store: Store, args) -> None:
    with store.transaction():
        payload = dispute_mod.verify_invoice(
            store, args.line_id, args.decision, args.note, args.operator)
    _emit(payload, args.json,
          f"发票行 {args.line_id} 人工核实结论：{args.decision}")


def cmd_dispute(store: Store, args) -> None:
    with store.transaction():
        if args.action == "open":
            payload = dispute_mod.open_dispute(
                store, args.line_id, args.reason, args.operator)
        else:
            verdict = args.verdict_pos or args.verdict_opt
            if not verdict:
                raise StoreError("resolve 需要给出结论 accepted 或 rejected")
            payload = dispute_mod.resolve_dispute(
                store, args.line_id, verdict, args.note, args.operator)
    _emit(payload, args.json, f"争议处置完成：{json.dumps(payload, ensure_ascii=False)}")


def cmd_close(store: Store, args) -> None:
    with store.transaction():
        outcome = dispute_mod.close_period(store, args.period, args.force, args.operator)
    text = f"账期 {args.period} 已关账"
    if outcome["warnings"]:
        text += "（强制，附带警告：" + "；".join(outcome["warnings"]) + "）"
    _emit(outcome, args.json, text)


def cmd_correction(store: Store, args) -> None:
    with store.transaction():
        payload = dispute_mod.post_correction(
            store, args.origin_line, args.target_period, args.amount,
            args.kind, args.reason, request=args.request,
            attempt_id=args.attempt, team=args.team, operator=args.operator)
    _emit(payload, args.json,
          f"更正已入开放账期 {args.target_period}：调整行 {payload['line_id']}，"
          f"原账 {args.origin_line}（{payload['origin_period']}）未改写，联系已保留")


def cmd_shortfall(store: Store, args) -> None:
    with store.transaction():
        payload = dispute_mod.resolve_shortfall(
            store, args.shortfall_id, args.reason, args.operator)
    _emit(payload, args.json, f"差额单 {args.shortfall_id} 已处置：{args.reason}")


def cmd_worklist(store: Store, args) -> None:
    data = reports.finance_worklist(store)
    lines = ["财务待办："]
    lines.append(f"  待核实发票行：{len(data['pending_invoice_lines'])} 行")
    for ln in data["pending_invoice_lines"]:
        lines.append(f"    - {ln['line_id']} {ln['kind']} {ln['currency']} {ln['amount']}")
    lines.append(f"  未结争议：{len(data['open_disputes'])} 笔")
    for d in data["open_disputes"]:
        lines.append(f"    - {d['ref']}：{d['reason']}")
    lines.append(f"  未处置差额单：{len(data['open_shortfalls'])} 笔")
    for s in data["open_shortfalls"]:
        lines.append(f"    - {s['id']} {s['team']}/{s['period']} 缺口 {s['gap_fen']} 分：{s['reason']}")
    lines.append("  未分配池：")
    for period, pool in data["unallocated_pools"].items():
        lines.append(f"    - {period} 余额 {pool['balance_fen']} 分")
    lines.append(f"  来源冲突：{len(data['source_conflicts'])} 条")
    lines.append(f"  账期状态：{json.dumps(data['periods'], ensure_ascii=False)}")
    _emit(data, args.json, "\n".join(lines))


def cmd_quota_list(store: Store, args) -> None:
    data = reports.quota_report(store, args.period, args.as_of)
    lines = ["团队预算台账："]
    for lg in data["team_ledgers"]:
        lines.append(
            f"  {lg['team']}: 预算 {_fen(lg['budget_fen'])}，已承诺 {_fen(lg['committed_fen'])}"
            f"（确认 {lg['confirmed_usage_fen']} + 敞口 {lg['exposure_fen']} + "
            f"共享 {lg['shared_allocated_fen']} + 退款 {lg['confirmed_refund_fen']}），"
            f"持有预留 {_fen(lg['reserved_held_fen'])}，可用 {_fen(lg['available_fen'])}")
    lines.append("\n额度申请：")
    for item in data["requests"]:
        req = item["request"]
        ev = item["evaluation"]
        state = (item["reservation"] or {}).get("status") or (
            "waiting" if item["waiting"] else "未受理")
        lines.append(
            f"  {req['id']} {req['team']} {req['batch']} x{req['units']} "
            f"需要 {req['required_capability']} 可延迟={req['deferrable']}："
            f"区间 {ev['cost_range_fen']['low']}~{ev['cost_range_fen']['high']} 分，"
            f"风险 {ev['overspend_risk']}，状态 {state}")
    _emit(data, args.json, "\n".join(lines))


def cmd_integrity(store: Store, args) -> None:
    data = reports.integrity_check(store)
    text = "不变量自检通过：无半次处置、预算恒等式与分配校验全部成立" if data["ok"] \
        else "自检发现问题：\n" + "\n".join("  - " + p for p in data["problems"])
    _emit(data, args.json, text)
    if not data["ok"]:
        sys.exit(1)


# ----------------------------------------------------------------- 参数解析
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="settlement",
        description="模型用量结算与团队额度受理（本地合成数据，标准库实现）")
    parser.add_argument("--workdir", default="workdir", help="独立工作目录")
    parser.add_argument("--json", action="store_true", help="输出 JSON 而非中文表格")
    parser.add_argument("--as-of", default=None, help="只使用该时刻（含）前 received 的事实")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="初始化工作目录").set_defaults(func=cmd_init)

    p = sub.add_parser("import", help="导入契约目录下的来源 JSON")
    p.add_argument("input_dir")
    p.add_argument("--no-manifest-check", action="store_true")
    p.set_defaults(func=cmd_import)

    sub.add_parser("rebuild", help="从事件日志重建快照").set_defaults(func=cmd_rebuild)

    p = sub.add_parser("reconcile", help="尝试级对账（确认/预估/争议/差额）")
    p.add_argument("--period", default="2026-11"); p.set_defaults(func=cmd_reconcile)

    p = sub.add_parser("request", help="单次请求的尝试链与费用")
    p.add_argument("request_id"); p.set_defaults(func=cmd_request)

    p = sub.add_parser("team-cost", help="团队单位成本与趋势")
    p.add_argument("--period", default="2026-11"); p.set_defaults(func=cmd_team_cost)

    p = sub.add_parser("pool", help="未分配费用池")
    p.add_argument("--period", default="2026-11"); p.set_defaults(func=cmd_pool)

    p = sub.add_parser("propose", help="比较共享费用三种分配方案")
    p.add_argument("--period", default="2026-11")
    p.add_argument("line_id")
    p.add_argument("--weights", default=None, help='显式权重 JSON，如 {"care":1,"ops":2}')
    p.set_defaults(func=cmd_propose)

    p = sub.add_parser("confirm-allocation", help="财务确认分配口径")
    p.add_argument("--period", default="2026-11")
    p.add_argument("line_id")
    p.add_argument("basis", choices=list(alloc_mod.BASES))
    p.add_argument("--weights", default=None)
    p.add_argument("--operator", default="finance")
    p.set_defaults(func=cmd_confirm_alloc)

    p = sub.add_parser("evaluate", help="评估额度申请（只读）")
    p.add_argument("request_id"); p.set_defaults(func=cmd_evaluate)

    p = sub.add_parser("reserve", help="确认/等待额度预留")
    p.add_argument("request_id")
    p.add_argument("--model", default=None)
    p.add_argument("--units", type=int, default=None)
    p.add_argument("--operator", default="business")
    p.set_defaults(func=cmd_reserve)

    p = sub.add_parser("cancel", help="取消预留并释放额度")
    p.add_argument("request_id"); p.add_argument("--reason", default="业务取消")
    p.add_argument("--operator", default="business"); p.set_defaults(func=cmd_cancel)

    p = sub.add_parser("consume", help="登记实际消耗（成本与释放同事务）")
    p.add_argument("request_id"); p.add_argument("units", type=int)
    p.add_argument("fen", type=int); p.add_argument("--note", default="")
    p.add_argument("--operator", default="business"); p.set_defaults(func=cmd_consume)

    p = sub.add_parser("expire", help="扫描并释放到期预留")
    p.add_argument("--now", default=None); p.set_defaults(func=cmd_expire)

    p = sub.add_parser("budget", help="财务追加预算并重评估等待清单")
    p.add_argument("team"); p.add_argument("--period", default="2026-11")
    p.add_argument("delta_fen", type=int); p.add_argument("--reason", required=True)
    p.add_argument("--operator", default="finance"); p.set_defaults(func=cmd_budget)

    p = sub.add_parser("verify", help="人工核实未确认发票行")
    p.add_argument("line_id"); p.add_argument("decision", choices=["confirmed", "rejected"])
    p.add_argument("--note", default=""); p.add_argument("--operator", default="finance")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("dispute", help="争议开立/结论")
    p.add_argument("action", choices=["open", "resolve"])
    p.add_argument("line_id")
    p.add_argument("verdict_pos", nargs="?", default=None,
                   choices=["accepted", "rejected"],
                   help="resolve 时的结论，也可用 --verdict")
    p.add_argument("--reason", default="")
    p.add_argument("--verdict", dest="verdict_opt",
                   choices=["accepted", "rejected"], default=None)
    p.add_argument("--note", default=""); p.add_argument("--operator", default="finance")
    p.set_defaults(func=cmd_dispute)

    p = sub.add_parser("close", help="账期关账")
    p.add_argument("--period", default="2026-11")
    p.add_argument("--force", action="store_true")
    p.add_argument("--operator", default="finance"); p.set_defaults(func=cmd_close)

    p = sub.add_parser("correction", help="关账后更正进入开放账期")
    p.add_argument("origin_line"); p.add_argument("target_period")
    p.add_argument("amount"); p.add_argument("kind", choices=["usage", "shared", "refund", "adjustment"])
    p.add_argument("--reason", required=True)
    p.add_argument("--request", default=None); p.add_argument("--attempt", default=None)
    p.add_argument("--team", default=None); p.add_argument("--operator", default="finance")
    p.set_defaults(func=cmd_correction)

    p = sub.add_parser("shortfall", help="处置财务差额单")
    p.add_argument("shortfall_id"); p.add_argument("--reason", required=True)
    p.add_argument("--operator", default="finance"); p.set_defaults(func=cmd_shortfall)

    sub.add_parser("worklist", help="财务待办").set_defaults(func=cmd_worklist)

    p = sub.add_parser("quota-list", help="团队台账与额度申请总览")
    p.add_argument("--period", default="2026-11"); p.set_defaults(func=cmd_quota_list)

    sub.add_parser("integrity", help="不变量自检").set_defaults(func=cmd_integrity)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    store = Store(args.workdir)
    store.init()
    try:
        args.func(store, args)
    except StoreError as exc:
        print(f"业务拒绝：{exc}", file=sys.stderr)
        return 2
    except (KeyError, ValueError) as exc:
        print(f"输入错误：{exc}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
