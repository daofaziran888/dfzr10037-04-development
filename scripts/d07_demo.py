#!/usr/bin/env python3
"""D07 互斥候选全流程演示。

流程：
  阶段0 登记目录 A/B 的回查门槛；
  阶段1 送入 D07 两笔冲突候选，读出补证前的分层候选证据数据与融合待办；
  阶段2 并发复核：同 request_id 只记一次有效确认，补证层确认被拒，互斥候选被阻断；
  阶段3 目录 A 原文证据后补，读出补证后的分层证据数据与可融合判断；
  阶段4 逐条回查证据所属目录与门槛结论。

运行（隔离模式，路径显式注入）：
  python3 -I /workspace/scripts/d07_demo.py
"""
from __future__ import annotations

import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

from app.services.candidate_evidence import FusionTodoState  # noqa: E402
from app.services.candidate_evidence.scenario import (  # noqa: E402
    ASSET_D07,
    build_service,
    seed_d07,
    supplement_evidence_a,
)


def show(title: str, payload: object) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main() -> None:
    svc = seed_d07(build_service())

    # ---------- 阶段0：目录门槛 ----------
    show("阶段0 · 目录回查门槛", {"items": svc.gates_snapshot()})

    # ---------- 阶段1：补证前的分层候选证据数据 ----------
    before = svc.layered_evidence(ASSET_D07)
    show("阶段1 · D07 补证前分层候选证据数据", before)
    show("阶段1 · 消岐融合待办（随分层数据生成）", {"items": svc.list_todos()})

    # ---------- 阶段2：并发复核 ----------
    barrier = threading.Barrier(2)

    def review_b_first(request_id: str) -> dict:
        barrier.wait()  # 两个线程同时进入，制造真正的并发
        return svc.confirm(request_id=request_id, candidate_id="C-D07-B",
                           reviewer="消岐员-王").to_dict()

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(review_b_first, "REQ-B-01")
        f2 = pool.submit(review_b_first, "REQ-B-01")  # 同一请求重放/重复提交
        wave1 = [f1.result(), f2.result()]

    decision_a = svc.confirm(request_id="REQ-A-01", candidate_id="C-D07-A",
                             reviewer="自动确认通道").to_dict()
    decision_b_retry = svc.confirm(request_id="REQ-B-02", candidate_id="C-D07-B",
                                   reviewer="消岐员-王").to_dict()

    show(
        "阶段2 · 并发复核裁决留痕",
        {
            "同一request_id并发两次(B)": wave1,
            "补证层候选A请求确认": decision_a,
            "B换request_id再次确认(互斥)": decision_b_retry,
            "全资产唯一正式链接": svc.official_link(ASSET_D07),
            "裁决台账": svc.decisions(ASSET_D07),
        },
    )

    # ---------- 阶段3：A 的原文证据后补 ----------
    svc.supplement_evidence("C-D07-A", supplement_evidence_a())
    after = svc.layered_evidence(ASSET_D07)
    show("阶段3 · D07 补证后分层候选证据数据", after)

    decision_a_after = svc.confirm(request_id="REQ-A-02", candidate_id="C-D07-A",
                                   reviewer="自动确认通道").to_dict()
    show(
        "阶段3 · A 补证达标后再次确认（仍受互斥阻断）",
        {
            "decision": decision_a_after,
            "todo_A_领取即按目录证据评估可合并性": svc.claim_todo("C-D07-A"),
            "todo_B_领取": svc.claim_todo("C-D07-B"),
        },
    )

    svc.mark_fused("C-D07-B")
    show("阶段3 · 融合待办终态", {"items": svc.list_todos()})

    # ---------- 阶段4：逐条证据回查 ----------
    audits = [svc.audit_evidence(eid) for eid in ("EV-A1", "EV-A2", "EV-B1")]
    show("阶段4 · 每条证据所属目录与门槛回查", {"items": audits})

    # ---------- 补证前后可融合性对比 ----------
    def merge_view(snapshot: dict) -> list[dict]:
        return [
            {
                "candidate_id": item["candidate"]["candidate_id"],
                "catalog_id": item["candidate"]["catalog_id"],
                "confidence": item["candidate"]["confidence"],
                "layer": item["candidate"]["layer"],
                "can_merge": item["can_merge"],
                "merge_reason": item["merge_reason"],
                "todo_state": item["todo"]["state"],
            }
            for item in snapshot["candidates"]
        ]

    show(
        "汇总 · 补证前后可融合证据数据对比",
        {"补证前": merge_view(before), "补证后": merge_view(after)},
    )


if __name__ == "__main__":
    main()
