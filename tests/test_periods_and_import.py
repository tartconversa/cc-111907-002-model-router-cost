"""关账守卫、关账后更正、来源冲突与重复投递测试。"""

import json
import unittest

import _helpers
from _helpers import SettlementTestCase
from settlement import disputes as dispute_mod
from settlement.ledger import effective_invoice_lines, unallocated_pool
from settlement.allocation import confirm_allocation
from settlement.store import Store, StoreError


class ClosePeriodTests(SettlementTestCase):
    def _clear_worklist(self):
        with self.store.transaction():
            dispute_mod.verify_invoice(self.store, "I4", "rejected")
            confirm_allocation(self.store, "2026-11", "I3", "requests")

    def test_close_blocked_with_pending_items(self):
        with self.store.transaction():
            with self.assertRaises(StoreError) as ctx:
                dispute_mod.close_period(self.store, "2026-11")
        self.assertIn("关账被阻止", str(ctx.exception))

    def test_close_then_writes_are_rejected(self):
        self._clear_worklist()
        with self.store.transaction():
            dispute_mod.close_period(self.store, "2026-11")
            with self.assertRaises(StoreError):
                confirm_allocation(self.store, "2026-11", "I3", "usage")
            with self.assertRaises(StoreError):
                dispute_mod.close_period(self.store, "2026-11")

    def test_post_close_correction_enters_open_period_with_link(self):
        self._clear_worklist()
        with self.store.transaction():
            dispute_mod.close_period(self.store, "2026-11")
        # 补 12 月预算与汇率（模拟新账期事实导入）
        self._add_december_facts()
        with self.store.transaction():
            payload = dispute_mod.post_correction(
                self.store, "I3", "2026-12", "5.00", "shared",
                "复核少计", team="care")
        self.assertEqual(payload["origin_line"], "I3")
        self.assertEqual(payload["origin_period"], "2026-11")
        # 11 月原账未被改写
        nov_pool = unallocated_pool(self.store, "2026-11")
        self.assertEqual(nov_pool["balance_fen"], 0)
        # 调整行进 12 月且可追溯
        adj = next(x for x in self.store.snapshot["manual_invoice_lines"]
                   if x["line_id"] == payload["line_id"])
        self.assertEqual(adj["period"], "2026-12")
        self.assertEqual(adj["origin_line"], "I3")

    def test_correction_cannot_target_closed_period(self):
        self._clear_worklist()
        with self.store.transaction():
            dispute_mod.close_period(self.store, "2026-11")
            with self.assertRaises(StoreError):
                dispute_mod.post_correction(
                    self.store, "I3", "2026-11", "1.00", "shared", "x")

    def _add_december_facts(self):
        with self.store.transaction():
            self.store.snapshot["facts"]["teams"]["care|2026-12"] = {
                "team": "care", "period": "2026-12",
                "budget_fen": 5000, "carry": False}
            self.store.snapshot["facts"]["fx"]["2026-12|CNY"] = {
                "period": "2026-12", "currency": "CNY", "cny_rate": "1.00"}


class ImportRobustnessTests(SettlementTestCase):
    def test_reimport_same_bytes_is_idempotent(self):
        before = self.store.snapshot["seq"]
        with self.store.transaction():
            from settlement.importer import import_sources
            summary = import_sources(self.store, _helpers.INPUTS)
        self.assertEqual(summary["imported"], [])
        self.assertEqual(set(summary["skipped_duplicates"]),
                         {f.name for f in _helpers.INPUTS.glob("*.json")
                          if f.name not in ("manifest.json", "schema.json")})
        self.assertEqual(self.store.snapshot["seq"], before)

    def test_same_key_conflicting_version_is_retained_not_overwritten(self):
        conflict_dir = self.write_dataset(
            "invoices_x", [{
                "id": "I1", "revision": 2, "provider": "A", "period": "2026-11",
                "request": "R1", "attempt_id": "A1", "currency": "CNY",
                "amount": "9.99", "kind": "usage", "confirmed": True,
            }])
        with self.store.transaction():
            from settlement.importer import import_sources
            import_sources(self.store, conflict_dir, verify_manifest=False)
        conflicts = [c for c in self.store.snapshot["conflicts"]
                     if c["key"] == "I1|2"]
        self.assertEqual(len(conflicts), 1)
        # 生效行仍是先到的 0.02
        line = next(l for l in effective_invoice_lines(self.store)
                    if l["line_id"] == "I1")
        self.assertEqual(str(line["amount"]), "0.02")

    def test_rebuild_reproduces_snapshot_including_seq(self):
        with self.store.transaction():
            confirm_allocation(self.store, "2026-11", "I3", "usage")
        seq = self.store.snapshot["seq"]
        fresh = Store(self.workdir)
        fresh.init()
        fresh.rebuild()
        self.assertEqual(fresh.snapshot["seq"], seq)
        self.assertEqual(
            len(fresh.snapshot["facts"]["calls"]),
            len(self.store.snapshot["facts"]["calls"]))
        self.assertEqual(
            fresh.snapshot["allocations"][-1]["basis"], "usage")


if __name__ == "__main__":
    unittest.main()
