"""分层候选证据裁决引擎。

职责：
1. 按候选的置信字段与所属目录的回查门槛校验证据，生成分层候选证据数据；
2. 维护仅追加的证据台账，裁决所用证据永久留存来源目录与版本；
3. 复核请求按资产加锁、按 request_id 幂等，互斥候选只允许一条正式链接；
4. 分层结果驱动消岐融合待办，领取候选后按其记录的目录证据判断目标可否合并。

本模块只依赖标准库，可在 FastAPI 进程与离线脚本中复用。
"""
from __future__ import annotations

import threading
from collections import defaultdict
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Callable

from app.services.candidate_evidence.models import (
    CandidateLayer,
    CatalogGate,
    DecisionRecord,
    EvidenceRecord,
    FusionTodo,
    FusionTodoState,
    LinkCandidate,
    OfficialLink,
)


class EvidenceError(ValueError):
    """证据/候选数据不合法。"""


class CandidateEvidenceService:
    def __init__(self, gates: list[CatalogGate], now_fn: Callable[[], str] | None = None) -> None:
        self._gates: dict[str, CatalogGate] = {gate.catalog_id: gate for gate in gates}
        self._evidence: dict[str, EvidenceRecord] = {}
        self._candidates: dict[str, LinkCandidate] = {}
        self._asset_candidates: dict[str, list[str]] = defaultdict(list)
        self._decisions: list[DecisionRecord] = []
        self._links: dict[str, OfficialLink] = {}
        self._todos: dict[str, FusionTodo] = {}
        self._asset_locks: dict[str, threading.Lock] = {}
        self._global_lock = threading.Lock()
        self._now = now_fn or (lambda: datetime.now(timezone.utc).isoformat())

    # ---------- 目录门槛 ----------
    def register_gate(self, gate: CatalogGate) -> None:
        with self._global_lock:
            self._gates[gate.catalog_id] = gate

    def gates_snapshot(self) -> list[dict[str, Any]]:
        return [gate.to_dict() for gate in self._gates.values()]

    def _gate_of(self, catalog_id: str) -> CatalogGate:
        gate = self._gates.get(catalog_id)
        if gate is None:
            raise EvidenceError(f"目录 {catalog_id} 未登记回查门槛，无法裁决候选")
        return gate

    def _asset_lock(self, asset_id: str) -> threading.Lock:
        # 全局锁内惰性建锁，避免两个线程为同一资产各建一把互不互斥的锁。
        with self._global_lock:
            lock = self._asset_locks.get(asset_id)
            if lock is None:
                lock = threading.Lock()
                self._asset_locks[asset_id] = lock
            return lock

    # ---------- 证据台账（仅追加） ----------
    def add_evidence(self, record: EvidenceRecord) -> EvidenceRecord:
        with self._global_lock:
            if record.evidence_id in self._evidence:
                raise EvidenceError(
                    f"证据 {record.evidence_id} 已入台账，台账仅追加不可覆盖；"
                    "补证请使用新的 evidence_id"
                )
            if record.catalog_id not in self._gates:
                raise EvidenceError(f"证据 {record.evidence_id} 所属目录 {record.catalog_id} 未登记门槛")
            self._evidence[record.evidence_id] = record
            return record

    def get_evidence(self, evidence_id: str) -> EvidenceRecord:
        if evidence_id not in self._evidence:
            raise EvidenceError(f"证据 {evidence_id} 不在台账中")
        return self._evidence[evidence_id]

    def audit_evidence(self, evidence_id: str) -> dict[str, Any]:
        """回查单条证据：所属目录、门槛版本与逐条门槛结论。"""
        record = self.get_evidence(evidence_id)
        gate = self._gate_of(record.catalog_id)
        passed, reasons = gate.check(record)
        return {
            "evidence": record.to_dict(),
            "catalog_gate": gate.to_dict(),
            "gate_passed": passed,
            "gate_reasons": reasons,
        }

    # ---------- 候选准入与分层 ----------
    def ingest_candidate(
        self,
        *,
        candidate_id: str,
        asset_id: str,
        target_entity: str,
        catalog_id: str,
        confidence: float,
        evidence_ids: list[str],
    ) -> dict[str, Any]:
        """送审一个互斥候选，立即按目录门槛分层并生成/更新融合待办。"""
        with self._asset_lock(asset_id):
            if candidate_id in self._candidates:
                raise EvidenceError(f"候选 {candidate_id} 已送审，不可重复登记")
            gate = self._gate_of(catalog_id)
            for evidence_id in evidence_ids:
                if evidence_id not in self._evidence:
                    raise EvidenceError(f"候选 {candidate_id} 引用了台账外证据 {evidence_id}")
            candidate = LinkCandidate(
                candidate_id=candidate_id,
                asset_id=asset_id,
                target_entity=target_entity,
                catalog_id=catalog_id,
                confidence=float(confidence),
                evidence_ids=list(evidence_ids),
                created_at=self._now(),
            )
            self._classify(candidate, gate)
            self._candidates[candidate_id] = candidate
            self._asset_candidates[asset_id].append(candidate_id)
            self._sync_todo(candidate)
            return self.layered_evidence(asset_id)

    def _classify(self, candidate: LinkCandidate, gate: CatalogGate) -> None:
        """置信字段 × 目录证据门槛 → 四层裁决（门槛按候选证据集合并校验）。"""
        bundle = [self._evidence[eid] for eid in candidate.evidence_ids]
        _, reasons = gate.check_bundle(bundle)
        confidence_ok = candidate.confidence >= gate.auto_confirm_threshold
        gate_ok = not reasons
        if confidence_ok and gate_ok:
            candidate.layer = CandidateLayer.AUTO_CONFIRM
        elif confidence_ok:
            candidate.layer = CandidateLayer.SUPPLEMENT
        elif gate_ok:
            candidate.layer = CandidateLayer.MANUAL
        else:
            candidate.layer = CandidateLayer.REJECTED
        candidate.gate_reasons = reasons

    # ---------- 补证 ----------
    def supplement_evidence(self, candidate_id: str, new_record: EvidenceRecord) -> dict[str, Any]:
        """为补证层候选追加证据并重新分层；非补证层候选拒绝补证。"""
        candidate = self._require_candidate(candidate_id)
        with self._asset_lock(candidate.asset_id):
            if candidate.layer != CandidateLayer.SUPPLEMENT:
                raise EvidenceError(
                    f"候选 {candidate_id} 当前分层为 {candidate.layer.value}，"
                    "仅 supplement（补证）层候选具备补证资格"
                )
            if new_record.catalog_id != candidate.catalog_id:
                raise EvidenceError(
                    f"补证证据须来自候选所属目录 {candidate.catalog_id}，"
                    f"实际来自 {new_record.catalog_id}"
                )
            self.add_evidence(new_record)
            candidate.evidence_ids.append(new_record.evidence_id)
            gate = self._gate_of(candidate.catalog_id)
            self._classify(candidate, gate)
            self._sync_todo(candidate)
            return self.layered_evidence(candidate.asset_id)

    # ---------- 复核裁决（幂等 + 互斥） ----------
    def confirm(
        self,
        *,
        request_id: str,
        candidate_id: str,
        reviewer: str,
    ) -> DecisionRecord:
        """处理一条复核请求。

        - 同一 request_id 只记一次有效确认，重复/并发请求命中幂等；
        - 同一资产已有正式链接时，其他互斥候选一律阻断，不能同时成为正式链接；
        - 补证层/驳回层候选证据不满足回查标准，确认请求被拒绝并留痕。
        """
        candidate = self._require_candidate(candidate_id)
        asset_id = candidate.asset_id
        with self._asset_lock(asset_id):
            existed = self._decision_by_request(request_id)
            if existed is not None:
                # 并发或重放：不重复落账；返回标注 duplicate 的副本，原始台账记录不变。
                echo = replace(
                    existed,
                    action="duplicate",
                    detail=f"请求 {request_id} 已处理过（幂等命中，原裁决动作 {existed.action}）",
                )
                return echo
            record = self._confirm_locked(request_id, candidate, reviewer)
            self._decisions.append(record)
            return record

    def _confirm_locked(
        self, request_id: str, candidate: LinkCandidate, reviewer: str
    ) -> DecisionRecord:
        asset_id = candidate.asset_id
        gate = self._gate_of(candidate.catalog_id)
        evidence_refs = list(candidate.evidence_ids)
        gate_versions = {eid: self._evidence[eid].catalog_gate_version for eid in evidence_refs}
        now = self._now()

        official = self._links.get(asset_id)
        if (
            official is not None
            and official.candidate_id == candidate.candidate_id
            and candidate.layer in (CandidateLayer.AUTO_CONFIRM, CandidateLayer.MANUAL)
        ):
            return DecisionRecord(
                request_id=request_id, asset_id=asset_id, candidate_id=candidate.candidate_id,
                action="duplicate", reviewer=reviewer, decided_at=now,
                detail=f"资产 {asset_id} 的正式链接已由请求 {official.request_id} 建立",
                evidence_refs=evidence_refs, gate_versions=gate_versions,
            )

        if candidate.layer == CandidateLayer.SUPPLEMENT:
            record = DecisionRecord(
                request_id=request_id, asset_id=asset_id, candidate_id=candidate.candidate_id,
                action="rejected", reviewer=reviewer, decided_at=now,
                detail="置信度达标但证据不满足目录回查标准，候选仅具备补证资格，确认被拒绝",
                evidence_refs=evidence_refs, gate_versions=gate_versions,
            )
            self._sync_todo(candidate)
            return record
        if candidate.layer == CandidateLayer.REJECTED:
            record = DecisionRecord(
                request_id=request_id, asset_id=asset_id, candidate_id=candidate.candidate_id,
                action="rejected", reviewer=reviewer, decided_at=now,
                detail="置信度与目录证据均不达标，候选处于驳回层",
                evidence_refs=evidence_refs, gate_versions=gate_versions,
            )
            self._sync_todo(candidate)
            return record

        if official is not None:
            return DecisionRecord(
                request_id=request_id, asset_id=asset_id, candidate_id=candidate.candidate_id,
                action="blocked", reviewer=reviewer, decided_at=now,
                detail=(
                    f"互斥冲突：资产 {asset_id} 已链接到 {official.target_entity}"
                    f"（候选 {official.candidate_id}），本候选不能同时成为正式链接"
                ),
                evidence_refs=evidence_refs, gate_versions=gate_versions,
            )

        # AUTO_CONFIRM 自动确认与 MANUAL 人工裁决在此汇合，全资产唯一正式链接
        link = OfficialLink(
            asset_id=asset_id, candidate_id=candidate.candidate_id,
            target_entity=candidate.target_entity, catalog_id=candidate.catalog_id,
            request_id=request_id, reviewer=reviewer, confirmed_at=now,
            evidence_refs=evidence_refs, gate_versions=dict(gate_versions),
        )
        self._links[asset_id] = link
        decision = DecisionRecord(
            request_id=request_id, asset_id=asset_id, candidate_id=candidate.candidate_id,
            action="confirmed", reviewer=reviewer, decided_at=now,
            detail=f"正式链接建立：{asset_id} -> {candidate.target_entity}"
            f"（分层 {candidate.layer.value}）",
            evidence_refs=evidence_refs, gate_versions=dict(gate_versions),
        )
        for other_id in self._asset_candidates[asset_id]:
            self._sync_todo(self._candidates[other_id])
        return decision

    def _decision_by_request(self, request_id: str) -> DecisionRecord | None:
        for record in self._decisions:
            if record.request_id == request_id:
                return record
        return None

    # ---------- 消岐融合待办 ----------
    def _can_merge(self, candidate: LinkCandidate) -> tuple[bool, str]:
        """按候选记录的目录证据判断目标是否可合并。"""
        official = self._links.get(candidate.asset_id)
        if official is not None and official.candidate_id != candidate.candidate_id:
            return False, f"互斥候选 {official.candidate_id} 已成为正式链接，目标不可合并"
        if candidate.layer == CandidateLayer.SUPPLEMENT:
            return False, "证据未通过目录回查门槛，等待补证，暂不可合并"
        if candidate.layer == CandidateLayer.REJECTED:
            return False, "候选处于驳回层，不可合并"
        if candidate.gate_reasons:
            return False, "目录证据门槛未全部通过"
        if candidate.layer == CandidateLayer.MANUAL:
            return True, "证据可回查，人工裁决确认后可合并"
        return True, "置信度达标且证据通过目录回查门槛，可合并"

    def _sync_todo(self, candidate: LinkCandidate) -> None:
        """待办随分层候选证据数据更新（调用方须持有资产锁）。"""
        can_merge, reason = self._can_merge(candidate)
        official = self._links.get(candidate.asset_id)
        if official is not None and official.candidate_id != candidate.candidate_id:
            state = FusionTodoState.BLOCKED
        elif candidate.layer == CandidateLayer.SUPPLEMENT:
            state = FusionTodoState.SUPPLEMENTING
        elif candidate.layer == CandidateLayer.REJECTED:
            state = FusionTodoState.BLOCKED
        elif official is not None and official.candidate_id == candidate.candidate_id:
            state = FusionTodoState.MERGEABLE
        elif candidate.layer == CandidateLayer.AUTO_CONFIRM:
            state = FusionTodoState.MERGEABLE
        else:
            state = FusionTodoState.PENDING
        todo = self._todos.get(candidate.candidate_id)
        if todo is None:
            todo = FusionTodo(
                todo_id=f"TODO-{candidate.candidate_id}",
                asset_id=candidate.asset_id,
                candidate_id=candidate.candidate_id,
                target_entity=candidate.target_entity,
                state=state,
                can_merge=can_merge,
                reason=reason,
                catalog_id=candidate.catalog_id,
                layer=candidate.layer.value,
                evidence_ids=list(candidate.evidence_ids),
                updated_at=self._now(),
            )
            self._todos[candidate.candidate_id] = todo
        else:
            todo.state = state
            todo.can_merge = can_merge
            todo.reason = reason
            todo.layer = candidate.layer.value
            todo.evidence_ids = list(candidate.evidence_ids)
            todo.updated_at = self._now()

    def list_todos(self, *, state: FusionTodoState | None = None) -> list[dict[str, Any]]:
        items = [todo.to_dict() for todo in self._todos.values()]
        if state is not None:
            items = [item for item in items if item["state"] == state.value]
        return items

    def claim_todo(self, candidate_id: str) -> dict[str, Any]:
        """消岐员领取待办：返回候选与其记录的目录证据、可合并性判断。"""
        candidate = self._require_candidate(candidate_id)
        todo = self._todos[candidate_id]
        return {
            "todo": todo.to_dict(),
            "candidate": candidate.to_dict(),
            "merge_assessment": {
                "can_merge": todo.can_merge,
                "reason": todo.reason,
                "evidence_audit": [self.audit_evidence(eid) for eid in candidate.evidence_ids],
            },
        }

    def mark_fused(self, candidate_id: str) -> FusionTodo:
        candidate = self._require_candidate(candidate_id)
        with self._asset_lock(candidate.asset_id):
            todo = self._todos[candidate_id]
            if not todo.can_merge:
                raise EvidenceError(f"候选 {candidate_id} 当前不可合并：{todo.reason}")
            todo.state = FusionTodoState.FUSED
            todo.updated_at = self._now()
            return todo

    # ---------- 分层候选证据数据 ----------
    def layered_evidence(self, asset_id: str) -> dict[str, Any]:
        """输出某资产的分层候选证据数据：四层分桶 + 每条证据的目录门槛回查。"""
        layers: dict[str, list[dict[str, Any]]] = {
            layer.value: [] for layer in CandidateLayer
        }
        candidates_payload: list[dict[str, Any]] = []
        for candidate_id in self._asset_candidates.get(asset_id, []):
            candidate = self._candidates[candidate_id]
            layers[candidate.layer.value].append(candidate.to_dict())
            audit = [self.audit_evidence(eid) for eid in candidate.evidence_ids]
            can_merge, reason = self._can_merge(candidate)
            candidates_payload.append({
                "candidate": candidate.to_dict(),
                "can_merge": can_merge,
                "merge_reason": reason,
                "todo": self._todos[candidate_id].to_dict(),
                "evidence_audit": audit,
            })
        official = self._links.get(asset_id)
        return {
            "asset_id": asset_id,
            "official_link": official.to_dict() if official else None,
            "layers": layers,
            "candidates": candidates_payload,
            "generated_at": self._now(),
        }

    def decisions(self, asset_id: str | None = None) -> list[dict[str, Any]]:
        items = [record.to_dict() for record in self._decisions]
        if asset_id is not None:
            items = [item for item in items if item["asset_id"] == asset_id]
        return items

    def official_link(self, asset_id: str) -> dict[str, Any] | None:
        link = self._links.get(asset_id)
        return link.to_dict() if link else None

    # ---------- 内部工具 ----------
    def _require_candidate(self, candidate_id: str) -> LinkCandidate:
        candidate = self._candidates.get(candidate_id)
        if candidate is None:
            raise EvidenceError(f"候选 {candidate_id} 不存在")
        return candidate
