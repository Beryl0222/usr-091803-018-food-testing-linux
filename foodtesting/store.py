"""线程安全的内存存储与哈希链事件日志。

事件日志只追加、不改写：每条事件携带前一条哈希，任何环节的事后篡改
都会导致链哈希断裂，为"可核验记录"提供技术底座。生产环境可把 Store
换成数据库实现，接口保持一致。
"""

from __future__ import annotations

import copy
import hashlib
import json
import threading
from typing import Any, Optional

from .models import (
    Actor,
    Appeal,
    Case,
    CustodyEvent,
    Decision,
    InspectionPlan,
    LabResult,
    MethodVersion,
    Report,
    Sample,
    Seal,
    Standard,
    utcnow,
)

GENESIS_HASH = "0" * 16


def canonical(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class Store:
    """聚合根集合 + 事件溯源日志，全部访问在同一把锁内完成。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.actors: dict[str, Actor] = {}
        self.plans: dict[str, InspectionPlan] = {}
        self.samples: dict[str, Sample] = {}
        self.seals: dict[str, Seal] = {}
        self.custody: dict[str, list[CustodyEvent]] = {}
        self.standards: dict[str, dict[str, Standard]] = {}  # code -> version -> Standard
        self.methods: dict[str, dict[str, MethodVersion]] = {}  # method_code -> version
        self.results: dict[str, LabResult] = {}  # upload_id 幂等键
        self.results_by_sample: dict[str, list[str]] = {}
        self.reports: dict[str, Report] = {}
        self.reports_by_sample: dict[str, list[str]] = {}
        self.appeals: dict[str, Appeal] = {}
        self.cases: dict[str, Case] = {}
        self.case_by_batch: dict[str, str] = {}  # batch_no -> 当前存活案件
        self.decisions: dict[str, Decision] = {}
        self.journal: list[dict[str, Any]] = []
        self._seq_counters: dict[str, int] = {}

    # ------------------------------------------------------------- 基础工具

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def next_seq(self, key: str) -> int:
        """样品内严格递增序号（如交接链 seq）。"""
        value = self._seq_counters.get(key, 0) + 1
        self._seq_counters[key] = value
        return value

    def append_event(self, event_type: str, actor_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        prev_hash = self.journal[-1]["hash"] if self.journal else GENESIS_HASH
        # 深拷贝固化快照：领域对象之后的状态变化（补件、状态流转等）
        # 不得回写已入链的历史事件
        snapshot = copy.deepcopy(payload)
        body = {
            "seq": len(self.journal) + 1,
            "at": utcnow(),
            "type": event_type,
            "actor": actor_id,
            "payload": snapshot,
            "prev_hash": prev_hash,
        }
        body["hash"] = hashlib.sha256((prev_hash + canonical(body)).encode("utf-8")).hexdigest()[:16]
        self.journal.append(body)
        return body

    def chain_head(self) -> str:
        return self.journal[-1]["hash"] if self.journal else GENESIS_HASH

    def verify_chain(self) -> dict[str, Any]:
        """重放整条事件链，返回校验结果（供巡检/取证接口使用）。"""
        prev_hash = GENESIS_HASH
        for index, event in enumerate(self.journal):
            expected_prev = event["prev_hash"]
            if expected_prev != prev_hash:
                return {"ok": False, "broken_at": index + 1, "reason": "prev_hash_mismatch"}
            stored = event["hash"]
            body = {k: v for k, v in event.items() if k != "hash"}
            recomputed = hashlib.sha256((prev_hash + canonical(body)).encode("utf-8")).hexdigest()[:16]
            if stored != recomputed:
                return {"ok": False, "broken_at": index + 1, "reason": "hash_mismatch"}
            prev_hash = stored
        return {"ok": True, "events": len(self.journal), "head": prev_hash}

    # ------------------------------------------------------------- 查询助手

    def get_standard(self, code: str, version: Optional[str] = None) -> Standard:
        versions = self.standards[code]
        if version is None:
            return next(v for v in versions.values() if v.superseded_by is None)
        return versions[version]

    def effective_standard(self, code: str, at: str) -> Standard:
        """取 at 时点已生效的最新版本——报告签发后即以快照固化。"""
        candidates = [s for s in self.standards[code].values() if s.effective_at <= at]
        if not candidates:
            raise KeyError(f"标准 {code} 在 {at} 尚无生效版本")
        return max(candidates, key=lambda s: s.version)

    def get_method(self, versioned_code: str) -> MethodVersion:
        code, version = versioned_code.split("@", 1)
        return self.methods[code][version]

    def custody_chain(self, sample_id: str) -> list[CustodyEvent]:
        return list(self.custody.get(sample_id, []))

    def reports_for_sample(self, sample_id: str) -> list[Report]:
        return [self.reports[r] for r in self.reports_by_sample.get(sample_id, [])]

    def results_for_sample(self, sample_id: str) -> list[LabResult]:
        return [self.results[u] for u in self.results_by_sample.get(sample_id, [])]

    def live_case_for_batch(self, batch_no: str) -> Optional[Case]:
        case_id = self.case_by_batch.get(batch_no)
        if not case_id:
            return None
        case = self.cases[case_id]
        return None if case.merged_into else case
