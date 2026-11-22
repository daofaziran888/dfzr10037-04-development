"""分层候选证据裁决引擎单元测试。

运行：
  python3 -I /workspace/scripts/run_tests.py
（-I 隔离模式不注入当前目录，由运行器显式插入 backend 路径）
"""
from __future__ import annotations

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from app.services.candidate_evidence import (
    CandidateLayer,
    CandidateEvidenceService,
    EvidenceError,
    FusionTodoState,
)
from app.services.candidate_evidence.models import EvidenceRecord
from app.services.candidate_evidence.scenario import (
    ASSET_D07,
    build_service,
    seed_d07,
    supplement_evidence_a,
)


class LayerAdjudicationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = seed_d07(build_service())

    def test_d07_initial_layering(self) -> None:
        """A：0.96 达标但缺原文 → 补证层；B：0.81 不足但证据可回查 → 人工层。"""
        candidate_a = self.svc._candidates["C-D07-A"]
        candidate_b = self.svc._candidates["C-D07-B"]
        self.assertIs(candidate_a.layer, CandidateLayer.SUPPLEMENT)
        self.assertIs(candidate_b.layer, CandidateLayer.MANUAL)
        self.assertTrue(candidate_a.gate_reasons)  # 门槛未达标原因已记录
        self.assertEqual(candidate_b.gate_reasons, [])

        layered = self.svc.layered_evidence(ASSET_D07)
        self.assertEqual(len(layered["layers"]["supplement"]), 1)
        self.assertEqual(len(layered["layers"]["manual"]), 1)
        self.assertIsNone(layered["official_link"])

        todo_a = self.svc._todos["C-D07-A"]
        todo_b = self.svc._todos["C-D07-B"]
        self.assertIs(todo_a.state, FusionTodoState.SUPPLEMENTING)
        self.assertIs(todo_b.state, FusionTodoState.PENDING)
        self.assertFalse(todo_a.can_merge)
        self.assertTrue(todo_b.can_merge)

    def test_supplement_promotes_to_auto_confirm(self) -> None:
        """补证通过目录门槛后，候选从补证层升为自动确认层，待办转可合并。"""
        self.svc.supplement_evidence("C-D07-A", supplement_evidence_a())
        candidate_a = self.svc._candidates["C-D07-A"]
        self.assertIs(candidate_a.layer, CandidateLayer.AUTO_CONFIRM)
        self.assertEqual(candidate_a.gate_reasons, [])
        self.assertTrue(self.svc._todos["C-D07-A"].can_merge)
        # 旧证据未被覆盖，台账保留来源与版本
        old = self.svc.get_evidence("EV-A1")
        self.assertEqual(old.catalog_id, "CAT-A")
        self.assertEqual(old.catalog_gate_version, "v1")
        self.assertEqual(self.svc.get_evidence("EV-A2").source_version, "rev2")

    def test_supplement_only_allowed_from_supplement_layer(self) -> None:
        with self.assertRaises(EvidenceError):
            self.svc.supplement_evidence("C-D07-B", supplement_evidence_a())

    def test_evidence_ledger_is_append_only(self) -> None:
        with self.assertRaises(EvidenceError):
            self.svc.add_evidence(self.svc.get_evidence("EV-A1"))

    def test_rejected_layer(self) -> None:
        """置信度不足且证据不过门槛 → 驳回层。"""
        svc = build_service()
        svc.add_evidence(EvidenceRecord(
            evidence_id="EV-X1", catalog_id="CAT-B", catalog_gate_version="v1",
            evidence_kinds=["confidence_score"], captured_at="2026-11-22T09:00:00+00:00",
        ))
        svc.ingest_candidate(candidate_id="C-X", asset_id="X", target_entity="T-X",
                             catalog_id="CAT-B", confidence=0.50, evidence_ids=["EV-X1"])
        self.assertIs(svc._candidates["C-X"].layer, CandidateLayer.REJECTED)
        self.assertIs(svc._todos["C-X"].state, FusionTodoState.BLOCKED)
        self.assertFalse(svc._todos["C-X"].can_merge)


class ConcurrencyAdjudicationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = seed_d07(build_service())

    def test_same_request_id_confirms_once_under_concurrency(self) -> None:
        barrier = threading.Barrier(2)

        def review(_: int) -> dict:
            barrier.wait()
            return self.svc.confirm(
                request_id="REQ-B-01", candidate_id="C-D07-B", reviewer="消岐员-王"
            ).to_dict()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(review, range(2)))

        actions = sorted(item["action"] for item in results)
        self.assertEqual(actions, ["confirmed", "duplicate"])
        confirm_records = [d for d in self.svc.decisions(ASSET_D07) if d["action"] == "confirmed"]
        self.assertEqual(len(confirm_records), 1)
        link = self.svc.official_link(ASSET_D07)
        self.assertEqual(link["candidate_id"], "C-D07-B")
        self.assertEqual(link["evidence_refs"], ["EV-B1"])
        self.assertEqual(link["gate_versions"], {"EV-B1": "v1"})

    def test_mutually_exclusive_candidate_cannot_become_second_link(self) -> None:
        self.svc.confirm(request_id="REQ-B-01", candidate_id="C-D07-B", reviewer="消岐员-王")
        # B 已在处理：A 的确认先被拒（补证层），补证达标后再确认仍被互斥阻断
        rejected = self.svc.confirm(request_id="REQ-A-01", candidate_id="C-D07-A",
                                    reviewer="自动通道")
        self.assertEqual(rejected.action, "rejected")
        self.svc.supplement_evidence("C-D07-A", supplement_evidence_a())
        blocked = self.svc.confirm(request_id="REQ-A-02", candidate_id="C-D07-A",
                                   reviewer="自动通道")
        self.assertEqual(blocked.action, "blocked")
        self.assertEqual(self.svc.official_link(ASSET_D07)["candidate_id"], "C-D07-B")
        todo_a = self.svc._todos["C-D07-A"]
        self.assertIs(todo_a.state, FusionTodoState.BLOCKED)
        self.assertFalse(todo_a.can_merge)
        # B 融合成功，A 无法融合
        self.svc.mark_fused("C-D07-B")
        self.assertIs(self.svc._todos["C-D07-B"].state, FusionTodoState.FUSED)
        with self.assertRaises(EvidenceError):
            self.svc.mark_fused("C-D07-A")

    def test_claim_uses_recorded_catalog_evidence(self) -> None:
        self.svc.confirm(request_id="REQ-B-01", candidate_id="C-D07-B", reviewer="消岐员-王")
        claim_b = self.svc.claim_todo("C-D07-B")
        self.assertTrue(claim_b["merge_assessment"]["can_merge"])
        audit = claim_b["merge_assessment"]["evidence_audit"][0]
        self.assertEqual(audit["evidence"]["catalog_id"], "CAT-B")
        self.assertTrue(audit["gate_passed"])

    def test_audit_traces_catalog_and_gate(self) -> None:
        audit_a1 = self.svc.audit_evidence("EV-A1")
        self.assertFalse(audit_a1["gate_passed"])
        self.assertEqual(audit_a1["catalog_gate"]["catalog_id"], "CAT-A")
        self.assertTrue(audit_a1["gate_reasons"])
        audit_b1 = self.svc.audit_evidence("EV-B1")
        self.assertTrue(audit_b1["gate_passed"])
        self.assertEqual(audit_b1["evidence"]["locator"], "catalog-b://records/SN-8842-21")


if __name__ == "__main__":
    unittest.main()
