"""分层候选证据专用路由。

统一资源路由的动作模型（单状态流转）承载不了证据门槛裁决、补证与并发互斥，
因此本模块单独暴露 REST 接口，业务判断仍全部落在 CandidateEvidenceService。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.candidate_evidence import (
    CandidateEvidenceService,
    EvidenceError,
    FusionTodoState,
)
from app.services.candidate_evidence.models import EvidenceRecord
from app.services.candidate_evidence.scenario import (
    build_service,
    seed_d07,
    supplement_evidence_a,
)

router = APIRouter(prefix="/api/candidate-evidence", tags=["分层候选证据"])

# 单进程内存态服务：与 store 一致，重启后回到 D07 初始冲突场景。
service: CandidateEvidenceService = seed_d07(build_service())


class EvidenceIn(BaseModel):
    evidence_id: str
    catalog_id: str
    catalog_gate_version: str
    evidence_kinds: list[str]
    captured_at: str
    locator: str = ""
    source_excerpt: str = ""
    source_version: str = ""


class CandidateIn(BaseModel):
    candidate_id: str
    asset_id: str
    target_entity: str
    catalog_id: str
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_ids: list[str]


class ConfirmIn(BaseModel):
    request_id: str
    reviewer: str


class SupplementIn(BaseModel):
    evidence: EvidenceIn


def _raise(error: EvidenceError) -> None:
    raise HTTPException(status_code=409, detail=str(error))


@router.post("/reset")
def reset_scenario() -> dict[str, Any]:
    """重置为 D07 初始两笔冲突候选场景。"""
    global service
    service = seed_d07(build_service())
    return {"ok": True, "message": "已重置为 D07 冲突候选场景"}


@router.get("/gates")
def list_gates() -> dict[str, Any]:
    return {"items": service.gates_snapshot()}


@router.post("/evidence")
def add_evidence(payload: EvidenceIn) -> dict[str, Any]:
    try:
        record = service.add_evidence(EvidenceRecord(**payload.model_dump()))
    except EvidenceError as error:
        _raise(error)
    return {"ok": True, "evidence": record.to_dict()}


@router.post("/candidates")
def ingest_candidate(payload: CandidateIn) -> dict[str, Any]:
    try:
        layered = service.ingest_candidate(**payload.model_dump())
    except EvidenceError as error:
        _raise(error)
    return {"ok": True, "layered_evidence": layered}


@router.get("/assets/{asset_id}/layered-evidence")
def layered_evidence(asset_id: str) -> dict[str, Any]:
    return service.layered_evidence(asset_id)


@router.post("/candidates/{candidate_id}/supplement")
def supplement(candidate_id: str, payload: SupplementIn) -> dict[str, Any]:
    try:
        layered = service.supplement_evidence(
            candidate_id, EvidenceRecord(**payload.evidence.model_dump())
        )
    except EvidenceError as error:
        _raise(error)
    return {"ok": True, "layered_evidence": layered}


@router.post("/candidates/{candidate_id}/confirm")
def confirm(candidate_id: str, payload: ConfirmIn) -> dict[str, Any]:
    try:
        decision = service.confirm(
            request_id=payload.request_id,
            candidate_id=candidate_id,
            reviewer=payload.reviewer,
        )
    except EvidenceError as error:
        _raise(error)
    ok = decision.action == "confirmed"
    return {"ok": ok, "decision": decision.to_dict()}


@router.get("/todos")
def todos(state: FusionTodoState | None = None) -> dict[str, Any]:
    return {"items": service.list_todos(state=state)}


@router.get("/candidates/{candidate_id}/claim")
def claim(candidate_id: str) -> dict[str, Any]:
    try:
        return service.claim_todo(candidate_id)
    except EvidenceError as error:
        _raise(error)


@router.post("/candidates/{candidate_id}/fuse")
def fuse(candidate_id: str) -> dict[str, Any]:
    try:
        todo = service.mark_fused(candidate_id)
    except EvidenceError as error:
        _raise(error)
    return {"ok": True, "todo": todo.to_dict()}


@router.get("/evidence/{evidence_id}/audit")
def audit_evidence(evidence_id: str) -> dict[str, Any]:
    try:
        return service.audit_evidence(evidence_id)
    except EvidenceError as error:
        _raise(error)


@router.get("/assets/{asset_id}/decisions")
def decisions(asset_id: str) -> dict[str, Any]:
    return {"items": service.decisions(asset_id)}


@router.get("/scenarios/d07/supplement-evidence")
def d07_supplement_template() -> dict[str, Any]:
    """D07 目录 A 后补原文证据的模板，供演示直接回灌。"""
    return {"evidence": supplement_evidence_a().to_dict()}
