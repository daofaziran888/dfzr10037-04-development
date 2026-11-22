"""分层候选证据领域模型。

覆盖四个概念：
- CatalogGate：目录级回查门槛（目录证据要满足什么条件才允许被回访采信）；
- EvidenceRecord：证据台账条目，只追加、永不覆盖，留存来源目录与版本；
- LinkCandidate：互斥实体链接候选，携带置信字段与分层裁决结果；
- FusionTodo / DecisionRecord / OfficialLink：消岐融合待办与裁决留痕。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class CandidateLayer(str, Enum):
    """候选分层。

    AUTO_CONFIRM  置信度达自动确认阈值且证据通过目录回查门槛；
    MANUAL        置信度不足但证据可回查，进入人工消岐裁决；
    SUPPLEMENT    置信度达标但证据不满足回查标准，仅具备补证资格；
    REJECTED      置信度与证据均不达标，直接驳回。
    """

    AUTO_CONFIRM = "auto_confirm"
    MANUAL = "manual"
    SUPPLEMENT = "supplement"
    REJECTED = "rejected"


class FusionTodoState(str, Enum):
    PENDING = "pending"          # 待消岐员领取
    MERGEABLE = "mergeable"      # 证据齐备，目标可合并
    BLOCKED = "blocked"          # 互斥候选已抢先成为正式链接
    FUSED = "fused"              # 已执行融合
    SUPPLEMENTING = "supplementing"  # 等待补证


@dataclass(frozen=True)
class CatalogGate:
    """目录证据回查门槛。

    required_kinds：该目录证据至少要包含的证据类型（如 source_text、serial_record）；
    require_locator：是否必须携带可回访定位符（url/record_id 等）；
    require_source_text：是否必须采集到原文摘录。
    """

    catalog_id: str
    catalog_name: str
    auto_confirm_threshold: float
    required_kinds: frozenset[str]
    require_locator: bool = True
    require_source_text: bool = False
    gate_version: str = "v1"

    def check(self, evidence: "EvidenceRecord") -> tuple[bool, list[str]]:
        """按本目录门槛校验单条证据，返回（是否通过、未达标原因）。"""
        reasons: list[str] = []
        if evidence.catalog_id != self.catalog_id:
            reasons.append(
                f"证据 {evidence.evidence_id} 来自目录 {evidence.catalog_id}，与门槛目录 {self.catalog_id} 不一致"
            )
        if self.require_locator and not evidence.locator:
            reasons.append(f"目录 {self.catalog_id} 门槛要求可回访定位符，证据 {evidence.evidence_id} 未携带")
        if self.require_source_text and not evidence.source_excerpt:
            reasons.append(f"目录 {self.catalog_id} 门槛要求采集原文，证据 {evidence.evidence_id} 无原文摘录")
        if not evidence.evidence_kinds:
            reasons.append(f"证据 {evidence.evidence_id} 未声明证据类型")
        return not reasons, reasons

    def check_bundle(self, evidences: list["EvidenceRecord"]) -> tuple[bool, list[str]]:
        """按候选证据集合做合并校验：要求在集合层面整体满足。

        定位符/原文只要证据集合里有一条满足即可，证据类型由各条证据的类型并集凑齐；
        单条记录自身的目录归属等问题仍逐条报出。
        """
        reasons: list[str] = []
        kinds: set[str] = set()
        has_locator = False
        has_source_text = False
        for evidence in evidences:
            if evidence.catalog_id != self.catalog_id:
                reasons.append(
                    f"证据 {evidence.evidence_id} 来自目录 {evidence.catalog_id}，"
                    f"与门槛目录 {self.catalog_id} 不一致"
                )
            kinds.update(evidence.evidence_kinds)
            has_locator = has_locator or bool(evidence.locator)
            has_source_text = has_source_text or bool(evidence.source_excerpt)
        missing_kinds = sorted(self.required_kinds - kinds)
        if missing_kinds:
            reasons.append(
                f"目录 {self.catalog_id} 门槛要求证据类型 {sorted(self.required_kinds)}，"
                f"候选证据集合缺少 {missing_kinds}"
            )
        if self.require_locator and not has_locator:
            reasons.append(f"目录 {self.catalog_id} 门槛要求至少一条证据携带可回访定位符")
        if self.require_source_text and not has_source_text:
            reasons.append(f"目录 {self.catalog_id} 门槛要求至少一条证据采集原文")
        return not reasons, reasons

    def to_dict(self) -> dict[str, Any]:
        return {
            "catalog_id": self.catalog_id,
            "catalog_name": self.catalog_name,
            "auto_confirm_threshold": self.auto_confirm_threshold,
            "required_kinds": sorted(self.required_kinds),
            "require_locator": self.require_locator,
            "require_source_text": self.require_source_text,
            "gate_version": self.gate_version,
        }


@dataclass
class EvidenceRecord:
    """证据台账条目。台账仅追加，已用于裁决的证据来源与版本永久留存。"""

    evidence_id: str
    catalog_id: str
    catalog_gate_version: str          # 采信当时的门槛版本，门槛升级不影响历史裁决
    evidence_kinds: list[str]
    captured_at: str
    locator: str = ""                  # 可回访定位符
    source_excerpt: str = ""           # 采集到的原文（没有原文即为补证场景）
    source_version: str = ""           # 来源记录版本，如序列号记录的修订号
    immutable: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "catalog_id": self.catalog_id,
            "catalog_gate_version": self.catalog_gate_version,
            "evidence_kinds": list(self.evidence_kinds),
            "captured_at": self.captured_at,
            "locator": self.locator,
            "source_excerpt": self.source_excerpt,
            "source_version": self.source_version,
            "immutable": self.immutable,
        }


@dataclass
class LinkCandidate:
    """同一资产下的互斥实体链接候选，每个候选对应一个目录给出的目标指派。"""

    candidate_id: str
    asset_id: str
    target_entity: str
    catalog_id: str
    confidence: float
    evidence_ids: list[str] = field(default_factory=list)
    layer: CandidateLayer = CandidateLayer.MANUAL
    gate_reasons: list[str] = field(default_factory=list)
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "asset_id": self.asset_id,
            "target_entity": self.target_entity,
            "catalog_id": self.catalog_id,
            "confidence": self.confidence,
            "evidence_ids": list(self.evidence_ids),
            "layer": self.layer.value,
            "gate_reasons": list(self.gate_reasons),
            "created_at": self.created_at,
        }


@dataclass
class DecisionRecord:
    """裁决留痕：确认成功、幂等命中、互斥阻断、门槛不满足全部落账。"""

    request_id: str
    asset_id: str
    candidate_id: str
    action: str                        # confirmed / duplicate / blocked / rejected
    reviewer: str
    decided_at: str
    detail: str
    evidence_refs: list[str] = field(default_factory=list)
    gate_versions: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "asset_id": self.asset_id,
            "candidate_id": self.candidate_id,
            "action": self.action,
            "reviewer": self.reviewer,
            "decided_at": self.decided_at,
            "detail": self.detail,
            "evidence_refs": list(self.evidence_refs),
            "gate_versions": dict(self.gate_versions),
        }


@dataclass
class OfficialLink:
    """正式链接：同一资产有且仅有一条，由首个有效确认产生。"""

    asset_id: str
    candidate_id: str
    target_entity: str
    catalog_id: str
    request_id: str
    reviewer: str
    confirmed_at: str
    evidence_refs: list[str] = field(default_factory=list)
    gate_versions: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "candidate_id": self.candidate_id,
            "target_entity": self.target_entity,
            "catalog_id": self.catalog_id,
            "request_id": self.request_id,
            "reviewer": self.reviewer,
            "confirmed_at": self.confirmed_at,
            "evidence_refs": list(self.evidence_refs),
            "gate_versions": dict(self.gate_versions),
        }


@dataclass
class FusionTodo:
    """消岐融合待办：随分层候选证据数据更新，领取后按记录的目录证据判断可合并性。"""

    todo_id: str
    asset_id: str
    candidate_id: str
    target_entity: str
    state: FusionTodoState
    can_merge: bool
    reason: str
    catalog_id: str
    layer: str
    evidence_ids: list[str] = field(default_factory=list)
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "todo_id": self.todo_id,
            "asset_id": self.asset_id,
            "candidate_id": self.candidate_id,
            "target_entity": self.target_entity,
            "state": self.state.value,
            "can_merge": self.can_merge,
            "reason": self.reason,
            "catalog_id": self.catalog_id,
            "layer": self.layer,
            "evidence_ids": list(self.evidence_ids),
            "updated_at": self.updated_at,
        }
