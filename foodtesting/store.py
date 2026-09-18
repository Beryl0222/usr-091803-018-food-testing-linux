"""内存仓储与可核验事件日志。

所有写操作在追加业务实体的同时记录一条哈希链事件，
抽样、封签、检测、复检、立案、决定由此接成一条可核验记录。
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections import defaultdict
from typing import Optional

from .auth import seed_users
from .models import Event, iso, utcnow

GENESIS_HASH = "0" * 64


def _event_payload(seq, type_, actor_id, entity_type, entity_id, summary, at_iso, prev_hash):
    return json.dumps(
        {
            "seq": seq,
            "type": type_,
            "actor_id": actor_id,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "summary": summary,
            "at": at_iso,
            "prev_hash": prev_hash,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.users = {}
        self.users_by_token = {}
        self.standards = {}
        self.plans = {}
        self.samples = {}
        self.reports = {}
        self.retest_apps = {}
        self.cases = {}
        self.decisions = {}
        self.events: list[Event] = []
        # upload_key -> {"report_id", "attempts", "fingerprint"}，实现“重试只受理一次”
        self.upload_index: dict[str, dict] = {}
        self._seq = defaultdict(int)
        seed_users(self)

    def next_id(self, prefix: str) -> str:
        self._seq[prefix] += 1
        return f"{prefix}-{self._seq[prefix]:04d}"

    # ---- 事件链 ----

    def record(self, type_: str, actor_id: str, entity_type: str, entity_id: str, summary: str) -> Event:
        prev_hash = self.events[-1].hash if self.events else GENESIS_HASH
        seq = len(self.events) + 1
        at = utcnow()
        payload = _event_payload(seq, type_, actor_id, entity_type, entity_id, summary, iso(at), prev_hash)
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        event = Event(seq, type_, actor_id, entity_type, entity_id, summary, at, prev_hash, digest)
        self.events.append(event)
        return event

    def verify_chain(self) -> tuple[bool, Optional[int]]:
        prev_hash = GENESIS_HASH
        for event in self.events:
            payload = _event_payload(
                event.seq,
                event.type,
                event.actor_id,
                event.entity_type,
                event.entity_id,
                event.summary,
                iso(event.at),
                event.prev_hash,
            )
            if event.prev_hash != prev_hash or hashlib.sha256(payload.encode("utf-8")).hexdigest() != event.hash:
                return False, event.seq
            prev_hash = event.hash
        return True, None

    # ---- 常用查询 ----

    def effective_standard(self, code: str):
        for std in self.standards.values():
            if std.code == code and std.status == "effective":
                return std
        return None

    def case_for_batch(self, batch_no: str):
        for case in self.cases.values():
            if case.batch_no == batch_no:
                return case
        return None

    def reports_for_sample(self, sample_id: str) -> list:
        return sorted(
            (r for r in self.reports.values() if r.sample_id == sample_id),
            key=lambda r: r.uploaded_at,
        )

    def sample_involves_user(self, sample, user_id: str) -> bool:
        if any(ev.to_user_id == user_id for ev in sample.custody):
            return True
        return any(r.lab_id == user_id for r in self.reports_for_sample(sample.id))
