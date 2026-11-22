"""D07 冲突候选场景工厂：目录 A 高分缺原文、目录 B 低分带可回访序列号记录。"""
from __future__ import annotations

from typing import Callable

from app.services.candidate_evidence import (
    CandidateEvidenceService,
    CatalogGate,
    EvidenceRecord,
)

ASSET_D07 = "D07"

GATE_A = CatalogGate(
    catalog_id="CAT-A",
    catalog_name="目录A",
    auto_confirm_threshold=0.90,
    required_kinds=frozenset({"source_text"}),
    require_locator=True,
    require_source_text=True,
    gate_version="v1",
)
GATE_B = CatalogGate(
    catalog_id="CAT-B",
    catalog_name="目录B",
    auto_confirm_threshold=0.90,
    required_kinds=frozenset({"serial_record"}),
    require_locator=True,
    require_source_text=False,
    gate_version="v1",
)


def build_service(now_fn: Callable[[], str] | None = None) -> CandidateEvidenceService:
    """构建仅登记目录门槛的空白裁决服务。"""
    return CandidateEvidenceService(gates=[GATE_A, GATE_B], now_fn=now_fn)


def seed_d07(service: CandidateEvidenceService) -> CandidateEvidenceService:
    """送入 D07 的两笔互斥候选：A 候选 0.96 分缺原文，B 候选 0.81 分带序列号记录。"""
    service.add_evidence(EvidenceRecord(
        evidence_id="EV-A1",
        catalog_id="CAT-A",
        catalog_gate_version="v1",
        evidence_kinds=["confidence_score"],
        captured_at="2026-11-22T09:00:00+00:00",
        locator="",
        source_excerpt="",
        source_version="",
    ))
    service.add_evidence(EvidenceRecord(
        evidence_id="EV-B1",
        catalog_id="CAT-B",
        catalog_gate_version="v1",
        evidence_kinds=["serial_record"],
        captured_at="2026-11-22T09:05:00+00:00",
        locator="catalog-b://records/SN-8842-21",
        source_excerpt="序列号 SN-8842-21，登记设备 D07",
        source_version="rev3",
    ))
    service.ingest_candidate(
        candidate_id="C-D07-A",
        asset_id=ASSET_D07,
        target_entity="EQ-A-07",
        catalog_id="CAT-A",
        confidence=0.96,
        evidence_ids=["EV-A1"],
    )
    service.ingest_candidate(
        candidate_id="C-D07-B",
        asset_id=ASSET_D07,
        target_entity="EQ-B-07",
        catalog_id="CAT-B",
        confidence=0.81,
        evidence_ids=["EV-B1"],
    )
    return service


def supplement_evidence_a() -> EvidenceRecord:
    """目录 A 后补的原文证据（A 的证据晚于 B 的人工裁决上报）。"""
    return EvidenceRecord(
        evidence_id="EV-A2",
        catalog_id="CAT-A",
        catalog_gate_version="v1",
        evidence_kinds=["source_text"],
        captured_at="2026-11-22T10:30:00+00:00",
        locator="catalog-a://records/D07/source-text",
        source_excerpt="设备铭牌原文：D07，出厂编号 SN-8842-21，归属机房 A7",
        source_version="rev2",
    )
