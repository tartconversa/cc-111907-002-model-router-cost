"""并发与跨进程一致性：文件锁、快照重读、余额不被双重占用。"""

import json
import multiprocessing as mp
import unittest
from pathlib import Path

from _helpers import SettlementTestCase, ROOT
from settlement.ledger import team_ledger


def _reserve(workdir: str, request_id: str, model: str | None) -> None:
    """子进程入口：全新解释器、全新 Store，模拟另一笔并发申请。"""
    import sys
    sys.path.insert(0, str(ROOT))
    from settlement.store import Store
    from settlement import quota as quota_mod
    store = Store(workdir)
    store.init()
    with store.transaction():
        quota_mod.reserve(store, request_id, model=model)


class ConcurrencyTests(SettlementTestCase):
    def test_concurrent_reservations_serialize_and_never_double_spend(self):
        workdir = str(self.workdir)
        procs = [
            mp.Process(target=_reserve, args=(workdir, "B1", "small")),
            mp.Process(target=_reserve, args=(workdir, "B2", None)),
            mp.Process(target=_reserve, args=(workdir, "B3", None)),
        ]
        for p in procs:
            p.start()
        for p in procs:
            p.join()
        for p in procs:
            self.assertEqual(p.exitcode, 0)

        store = self.fresh_store()
        held = {k: v for k, v in store.snapshot["reservations"].items()
                if v["status"] == "held"}
        self.assertEqual(set(held), {"B1", "B2", "B3"})
        # 三笔预留金额各自只记一次
        self.assertEqual(held["B1"]["reserved_fen"], 1200)
        self.assertEqual(held["B2"]["reserved_fen"], 6000)
        self.assertEqual(held["B3"]["reserved_fen"], 10800)
        # 预算恒等式在并发交错后仍然成立
        for team in ("care", "ops"):
            lg = team_ledger(store, team, "2026-11")
            self.assertEqual(
                lg["committed_fen"] + lg["reserved_held_fen"] + lg["available_fen"],
                lg["budget_fen"])

    def test_duplicate_submission_from_two_processes_holds_once(self):
        workdir = str(self.workdir)
        procs = [
            mp.Process(target=_reserve, args=(workdir, "B2", None)),
            mp.Process(target=_reserve, args=(workdir, "B2", None)),
        ]
        for p in procs:
            p.start()
        for p in procs:
            p.join()
        events = [
            json.loads(line) for line in
            Path(workdir, "state", "events.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        created = [e for e in events
                   if e["type"] == "reservation.created"
                   and e["payload"]["request_id"] == "B2"]
        self.assertEqual(len(created), 1)


if __name__ == "__main__":
    unittest.main()
