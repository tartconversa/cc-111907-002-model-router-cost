"""本地状态存储：只追加事件日志 + 可重建快照。

所有人工处置（分配确认、争议、关账、预算调整、额度生命周期……）都以事件形式
追加到 ``state/events.jsonl``；快照 ``state/snapshot.json`` 只是事件与来源事实
的派生缓存，删除后可由 :meth:`Store.rebuild` 完整重建。

互斥：所有写操作在 ``state/store.lock`` 上加文件锁，保证两份额度申请不可能在
两个进程里同时花掉同一余额。
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .timeutil import now_ts

SNAPSHOT_VERSION = 1

# 各来源实体的自然键。带版本的发票行以 (行标识, 版本) 为键。
ENTITY_KEYS = {
    "calls": lambda r: r["id"],
    "invoices": lambda r: f"{r['id']}|{r['revision']}",
    "prices": lambda r: r["id"],
    "fx": lambda r: f"{r['period']}|{r['currency']}",
    "teams": lambda r: f"{r['team']}|{r['period']}",
    "requests": lambda r: r["id"],
    "capabilities": lambda r: r["model"],
}


def empty_snapshot() -> dict[str, Any]:
    return {
        "version": SNAPSHOT_VERSION,
        "source_files": [],          # 每次导入的文件指纹与条数
        "facts": {name: {} for name in ENTITY_KEYS},
        "conflicts": [],             # 同键同版本内容冲突等来源问题
        "verifications": {},         # "发票行|版本" -> confirmed/rejected 人工核实
        "fact_overrides": [],        # 内部事实修订（如演练/复核调整预算行）
        "manual_invoice_lines": [],  # 内部记账调整行（争议反转、关账后更正）
        "allocations": [],           # 共享费用分配确认历史（开放账期可被新确认替代）
        "disputes": {},              # 争议键 -> 状态
        "periods": {},               # 账期 -> open/closed
        "budget_adjustments": [],    # 财务追加预算事件
        "reservations": {},          # 额度请求 id -> 预留生命周期记录
        "waiting": {},               # 主动等待的请求 -> 原因
        "shortfalls": {},            # 差额单
        "seq": 0,
    }


class StoreError(Exception):
    """业务级错误（重复占用、账期已关等），CLI 转为非零退出码。"""


class Store:
    def __init__(self, workdir: str | Path):
        self.root = Path(workdir)
        self.source_dir = self.root / "source"
        self.state_dir = self.root / "state"
        self.events_path = self.state_dir / "events.jsonl"
        self.snapshot_path = self.state_dir / "snapshot.json"
        self.lock_path = self.state_dir / "store.lock"
        self.snapshot = empty_snapshot()

    # ------------------------------------------------------------------ 初始化
    def init(self) -> None:
        self.source_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        if self.snapshot_path.exists():
            self.snapshot = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
        elif self.events_path.exists():
            self.rebuild()
        else:
            self._flush_snapshot()

    def rebuild(self) -> None:
        """从事件日志重建快照（校验/排障用）。

        来源导入事件本身携带行内容（同时在 source/ 留有只读副本），因此按
        seq 顺序重放全部事件即可完整还原事实表与全部人工处置。
        """
        snap = empty_snapshot()
        last_seq = 0
        if self.events_path.exists():
            for line in self.events_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                event = json.loads(line)
                last_seq = max(last_seq, event["seq"])
                # 重放时直接应用，不重新分配 seq（保持事件身份不变）。
                if event["type"] == "source.imported":
                    self._apply_import(snap, event, replay=True)
                else:
                    self.apply_event(snap, event)
        snap["seq"] = last_seq
        self.snapshot = snap
        self._flush_snapshot()

    # ------------------------------------------------------------------ 锁/事务
    @contextmanager
    def transaction(self):
        """进入文件锁后重新加载磁盘快照，保证并发事务基于最新状态决策。

        没有这一步，两个进程先后拿到锁时会各自基于启动时的旧快照计算余额，
        后提交者把先提交者的预留整体覆盖——两份额度申请就花掉了同一余额。
        """
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "w", encoding="utf-8") as lock_fh:
            fcntl.flock(lock_fh, fcntl.LOCK_EX)
            try:
                if self.snapshot_path.exists():
                    on_disk = json.loads(
                        self.snapshot_path.read_text(encoding="utf-8"))
                    if on_disk.get("seq", 0) >= self.snapshot.get("seq", 0):
                        self.snapshot = on_disk
                yield self
                self._flush_snapshot()
            finally:
                fcntl.flock(lock_fh, fcntl.LOCK_UN)

    def append_event(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        """生成并追加事件，立刻应用到快照。调用方必须持有 transaction 锁。"""
        snap = self.snapshot
        snap["seq"] += 1
        event = {
            "seq": snap["seq"],
            "at": now_ts(),
            "type": event_type,
            "payload": payload,
        }
        with open(self.events_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        if event_type == "source.imported":
            self._apply_import(snap, event, replay=False)
        else:
            self.apply_event(snap, event)
        return event

    def _flush_snapshot(self) -> None:
        tmp = self.snapshot_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self.snapshot, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(tmp, self.snapshot_path)

    # ------------------------------------------------------------------ 来源导入
    def import_file(self, path: str | Path) -> dict[str, Any]:
        """登记一次来源导入：哈希去重、事实合并、版本冲突保留。"""
        path = Path(path)
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        rows = json.loads(raw.decode("utf-8"))
        if not isinstance(rows, list):
            raise StoreError(f"{path.name}: 来源文件必须是记录列表")
        payload = {
            "file": path.name,
            "sha256": digest,
            "record_count": len(rows),
            "rows": rows,
        }
        return self.append_event("source.imported", payload)

    def _apply_import(self, snap: dict[str, Any], event: dict[str, Any], replay: bool) -> None:
        payload = event["payload"]
        # 同一字节内容重复投递：不产生新的业务活动，仅保留一条登记。
        if any(s["sha256"] == payload["sha256"] for s in snap["source_files"]):
            return
        saved = {k: v for k, v in payload.items() if k != "rows"}
        saved["imported_seq"] = event["seq"]
        # 重放时从 source 副本读取行；首次导入行在事件里。
        rows = payload.get("rows")
        if rows is None:
            copy_path = self.source_dir / f"{event['seq']:06d}_{saved['file']}"
            rows = json.loads(copy_path.read_text(encoding="utf-8"))
        else:
            copy_path = self.source_dir / f"{event['seq']:06d}_{saved['file']}"
            copy_path.write_text(
                json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        snap["source_files"].append(saved)

        stem = saved["file"].removesuffix(".json")
        entity = stem.split("_", 1)[0] if "_" in stem else stem
        if entity not in ENTITY_KEYS:
            # 未知实体不进事实表，但保留文件与问题，便于人工核对。
            snap["conflicts"].append(
                {
                    "seq": event["seq"],
                    "entity": entity,
                    "key": None,
                    "detail": "未知来源实体，未并入事实表",
                }
            )
            return
        key_fn = ENTITY_KEYS[entity]
        table = snap["facts"][entity]
        for row in rows:
            key = key_fn(row)
            if key in table:
                if table[key] == row:
                    continue  # 重复投递同一事实
                # 同自然键同版本内容不一致：来源版本冲突，保留问题且不覆盖。
                snap["conflicts"].append(
                    {
                        "seq": event["seq"],
                        "entity": entity,
                        "key": key,
                        "detail": "同键记录内容冲突，保留先到版本，新来版本挂起",
                        "existing": table[key],
                        "incoming": row,
                    }
                )
                continue
            table[key] = row

    # ------------------------------------------------------------------ 事件应用
    def apply_event(self, snap: dict[str, Any], event: dict[str, Any]) -> None:
        etype = event["type"]
        p = event["payload"]
        if etype == "fact.overridden":
            # 内部复核修订来源事实（如演练收紧预算）：覆盖并保留原值，可重建。
            table = snap["facts"][p["entity"]]
            record = dict(p)
            record["previous"] = table.get(p["key"])
            table[p["key"]] = p["row"]
            snap["fact_overrides"].append(record)
        elif etype == "invoice.verified":
            snap["verifications"][f"{p['line_id']}|{p['revision']}"] = p["decision"]
        elif etype == "adjustment.posted":
            snap["manual_invoice_lines"].append(p)
        elif etype == "allocation.confirmed":
            snap["allocations"].append(p)
        elif etype == "dispute.opened":
            snap["disputes"][p["ref"]] = {
                "status": "open",
                "line_id": p["line_id"],
                "reason": p["reason"],
                "opened_at": event["at"],
                "history": [{"at": event["at"], "action": "opened", "detail": p["reason"]}],
            }
        elif etype == "dispute.resolved":
            dispute = snap["disputes"][p["ref"]]
            dispute["status"] = p["verdict"]
            dispute["resolved_at"] = event["at"]
            dispute["history"].append(
                {"at": event["at"], "action": p["verdict"], "detail": p.get("note", "")}
            )
        elif etype == "period.closed":
            snap["periods"][p["period"]] = {"status": "closed", "closed_at": event["at"]}
        elif etype == "budget.increased":
            snap["budget_adjustments"].append(p)
            for sf in list(snap["shortfalls"].values()):
                if (
                    sf["status"] == "open"
                    and sf["team"] == p["team"]
                    and sf["period"] == p["period"]
                ):
                    sf["status"] = "resolved"
                    sf["resolved_by"] = event["seq"]
                    sf["resolved_at"] = event["at"]
        elif etype == "shortfall.opened":
            snap["shortfalls"][p["id"]] = dict(p, status="open", opened_at=event["at"])
        elif etype == "shortfall.resolved":
            sf = snap["shortfalls"][p["id"]]
            sf["status"] = "resolved"
            sf["resolved_by"] = event["seq"]
            sf["resolved_at"] = event["at"]
            sf["resolution_note"] = p.get("reason", "")
        elif etype == "reservation.created":
            snap["reservations"][p["request_id"]] = dict(
                p, status="held", created_at=event["at"], history=[
                    {"at": event["at"], "action": "created", "detail": p.get("basis", "")}
                ]
            )
        elif etype == "reservation.waiting":
            snap["waiting"][p["request_id"]] = dict(p, at=event["at"])
        elif etype == "reservation.cancelled":
            r = snap["reservations"][p["request_id"]]
            r["status"] = "released"
            r["released_at"] = event["at"]
            r["history"].append({"at": event["at"], "action": "cancelled", "detail": p.get("reason", "")})
        elif etype == "reservation.expired":
            r = snap["reservations"][p["request_id"]]
            r["status"] = "expired"
            r["released_at"] = event["at"]
            r["history"].append({"at": event["at"], "action": "expired", "detail": "到期自动释放"})
        elif etype == "reservation.consumed":
            r = snap["reservations"][p["request_id"]]
            r["status"] = "consumed"
            r["consumed_fen"] = p["consumed_fen"]
            r["released_fen"] = r["reserved_fen"] - p["consumed_fen"]
            r["consumed_at"] = event["at"]
            r["history"].append(
                {
                    "at": event["at"],
                    "action": "consumed",
                    "detail": f"实际消耗 {p['consumed_fen']} 分，释放余量 {r['released_fen']} 分",
                }
            )
        else:
            raise StoreError(f"未知事件类型: {etype}")

    # ------------------------------------------------------------------ 读取助手
    def override_fact(self, entity: str, key: str, row: dict[str, Any],
                      reason: str) -> dict[str, Any]:
        """内部复核修订一条来源事实（原值随事件保留，快照可重建）。"""
        if entity not in ENTITY_KEYS:
            raise StoreError(f"未知实体 {entity}")
        if key not in self.snapshot["facts"][entity]:
            raise StoreError(f"事实 {entity}/{key} 不存在，不能修订")
        return self.append_event("fact.overridden", {
            "entity": entity, "key": key, "row": row, "reason": reason,
        })["payload"]

    def table(self, entity: str) -> list[dict[str, Any]]:
        return list(self.snapshot["facts"][entity].values())

    def fact(self, entity: str, key: str) -> dict[str, Any] | None:
        return self.snapshot["facts"][entity].get(key)

    def is_closed(self, period: str) -> bool:
        return self.snapshot["periods"].get(period, {}).get("status") == "closed"
