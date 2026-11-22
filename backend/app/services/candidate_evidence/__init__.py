"""分层候选证据裁决能力（实体链接 × 消岐融合）。"""
from app.services.candidate_evidence.engine import CandidateEvidenceService, EvidenceError
from app.services.candidate_evidence.models import (
    CandidateLayer,
    CatalogGate,
    DecisionRecord,
    EvidenceRecord,
    FusionTodoState,
)

__all__ = [
    "CandidateEvidenceService",
    "EvidenceError",
    "CandidateLayer",
    "CatalogGate",
    "DecisionRecord",
    "EvidenceRecord",
    "FusionTodoState",
]
