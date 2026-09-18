"""领域对象：抽样计划、样品、封签、流转、标准、方法版本、结果、报告、复检、案件、决定。

所有对象在写入后即视为不可变快照（除案件聚合状态外）；字段直接对应
一条可核验记录上的各个环节，便于从任意报告回溯证据链。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


def utcnow() -> str:
    """统一的 UTC 时间戳（ISO8601，秒级）。"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------- 角色与组织

@dataclass
class Actor:
    """系统参与者：监管人员、抽样人员、实验室或商户。"""

    actor_id: str
    name: str
    role: str  # regulator | sampler | lab | merchant
    org: str
    region: str

    def public_dict(self) -> dict[str, Any]:
        return {"actor_id": self.actor_id, "name": self.name, "role": self.role, "org": self.org, "region": self.region}


# ---------------------------------------------------------------- 抽样与封签

@dataclass
class InspectionPlan:
    """抽样计划：节前专项的一次任务安排。"""

    plan_id: str
    name: str
    region: str
    food_category: str
    created_by: str
    created_at: str
    items: list[dict[str, str]] = field(default_factory=list)  # [{merchant_id, product_name, batch_no}]

    def as_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "name": self.name,
            "region": self.region,
            "food_category": self.food_category,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "items": self.items,
        }


@dataclass
class Sample:
    """样品：同一商户、同一生产批次的糕点在一次抽样中的实物。"""

    sample_id: str
    plan_id: str
    region: str
    merchant_id: str
    product_name: str
    producer: str
    batch_no: str  # 生产批次，跨地区合并研判的核心键
    sampled_at: str
    sampled_by: str
    location: str
    current_holder: str
    status: str = "sealed"  # sealed | in_transit | at_lab | under_reinspection | retained | disposed | returned

    def as_dict(self) -> dict[str, Any]:
        return {
            "sample_id": self.sample_id,
            "plan_id": self.plan_id,
            "region": self.region,
            "merchant_id": self.merchant_id,
            "product_name": self.product_name,
            "producer": self.producer,
            "batch_no": self.batch_no,
            "sampled_at": self.sampled_at,
            "sampled_by": self.sampled_by,
            "location": self.location,
            "current_holder": self.current_holder,
            "status": self.status,
        }


@dataclass
class Seal:
    """样品封签：编号唯一，记录封签与启封动作。"""

    seal_no: str
    sample_id: str
    sealed_by: str
    sealed_at: str
    intact: bool = True
    opened_by: Optional[str] = None
    opened_at: Optional[str] = None
    open_reason: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "seal_no": self.seal_no,
            "sample_id": self.sample_id,
            "sealed_by": self.sealed_by,
            "sealed_at": self.sealed_at,
            "intact": self.intact,
            "opened_by": self.opened_by,
            "opened_at": self.opened_at,
            "open_reason": self.open_reason,
        }


@dataclass
class CustodyEvent:
    """运输交接 / 保管流转事件，串成样品去向链。"""

    event_id: str
    sample_id: str
    seq: int
    action: str  # handover | receive | open_seal | transfer_to_reinspection | retain | dispose | return
    actor_id: str
    org: str
    at: str
    from_holder: Optional[str] = None
    to_holder: Optional[str] = None
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "sample_id": self.sample_id,
            "seq": self.seq,
            "action": self.action,
            "actor_id": self.actor_id,
            "org": self.org,
            "at": self.at,
            "from_holder": self.from_holder,
            "to_holder": self.to_holder,
            "note": self.note,
        }


# ---------------------------------------------------------------- 标准与方法版本

@dataclass
class Standard:
    """判定标准（限量指标 / 添加剂使用范围），带版本与生效日期。

    报告只引用签发当时的版本快照；新版本生效不倒改旧报告。
    """

    code: str
    version: str
    title: str
    effective_at: str
    limits: dict[str, dict[str, Any]]  # item_key -> {limit, unit, basis, additive_scope}
    superseded_by: Optional[str] = None

    @property
    def versioned_code(self) -> str:
        return f"{self.code}@{self.version}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "version": self.version,
            "versioned_code": self.versioned_code,
            "title": self.title,
            "effective_at": self.effective_at,
            "limits": self.limits,
            "superseded_by": self.superseded_by,
        }


@dataclass
class MethodVersion:
    """检测方法版本（如 GB 5009.227-2016 / 2023）。"""

    method_code: str
    version: str
    title: str
    item_key: str
    effective_at: str

    @property
    def versioned_code(self) -> str:
        return f"{self.method_code}@{self.version}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "method_code": self.method_code,
            "version": self.version,
            "versioned_code": self.versioned_code,
            "title": self.title,
            "item_key": self.item_key,
            "effective_at": self.effective_at,
        }


# ---------------------------------------------------------------- 结果与报告

@dataclass
class LabResult:
    """实验室原始结果上传。

    upload_id 由上传方生成，作为幂等键：同一 upload_id 的重试只受理一次。
    """

    upload_id: str
    sample_id: str
    lab_id: str
    item_key: str  # peroxide_value | additive:sorbic_acid ...
    value: Optional[float]
    unit: str
    method_version: str  # MethodVersion.versioned_code 快照
    detected: bool
    uploaded_at: str
    accepted: bool = True  # 被受理（非重复）才为 True
    duplicate_of: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "upload_id": self.upload_id,
            "sample_id": self.sample_id,
            "lab_id": self.lab_id,
            "item_key": self.item_key,
            "value": self.value,
            "unit": self.unit,
            "method_version": self.method_version,
            "detected": self.detected,
            "uploaded_at": self.uploaded_at,
            "accepted": self.accepted,
            "duplicate_of": self.duplicate_of,
        }


@dataclass
class Report:
    """检验报告：原始结果按签发时有效的标准版本作出的判定。

    报告一经签发不可变；标准更新后旧报告仍保留其 standard_snapshot。
    """

    report_id: str
    sample_id: str
    lab_id: str
    issued_at: str
    standard_version: str  # Standard.versioned_code 快照
    standard_snapshot: dict[str, Any]
    conclusions: list[dict[str, Any]]  # [{item_key, value, unit, verdict, basis}]
    overall_verdict: str  # qualified | unqualified
    findings: list[dict[str, Any]]  # 不合格项明细（风险等级等）
    superseded: bool = False  # 被复检报告推翻，仅置标记，原报告不删改
    reinspection_report_id: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "sample_id": self.sample_id,
            "lab_id": self.lab_id,
            "issued_at": self.issued_at,
            "standard_version": self.standard_version,
            "standard_snapshot": self.standard_snapshot,
            "conclusions": self.conclusions,
            "overall_verdict": self.overall_verdict,
            "findings": self.findings,
            "superseded": self.superseded,
            "reinspection_report_id": self.reinspection_report_id,
        }


@dataclass
class Appeal:
    """复检申请及补件材料。"""

    appeal_id: str
    case_id: str
    sample_id: str
    merchant_id: str
    reason: str
    status: str  # submitted | supplementing | accepted | rejected | concluded
    created_at: str
    decided_at: Optional[str] = None
    decision_note: str = ""
    supplement_due: Optional[str] = None  # 整改/补件期限
    documents: list[dict[str, Any]] = field(default_factory=list)  # [{doc_id, name, uploaded_by, at}]
    reinspection_report_id: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "appeal_id": self.appeal_id,
            "case_id": self.case_id,
            "sample_id": self.sample_id,
            "merchant_id": self.merchant_id,
            "reason": self.reason,
            "status": self.status,
            "created_at": self.created_at,
            "decided_at": self.decided_at,
            "decision_note": self.decision_note,
            "supplement_due": self.supplement_due,
            "documents": self.documents,
            "reinspection_report_id": self.reinspection_report_id,
        }


# ---------------------------------------------------------------- 案件与决定

@dataclass
class Case:
    """案件聚合：按生产批次跨地区合并，避免重复立案。"""

    case_id: str
    batch_no: str
    producer: str
    product_name: str
    opened_at: str
    opened_by: str
    regions: set[str] = field(default_factory=set)
    sample_ids: list[str] = field(default_factory=list)
    report_ids: list[str] = field(default_factory=list)
    appeal_ids: list[str] = field(default_factory=list)
    status: str = "investigating"  # investigating | pending_decision | enforcing | closed
    evidence_complete: bool = False
    evidence_checklist: dict[str, bool] = field(default_factory=dict)
    consolidated_from: list[str] = field(default_factory=list)  # 被合并进来的其他案件号
    merged_into: Optional[str] = None
    decision_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "batch_no": self.batch_no,
            "producer": self.producer,
            "product_name": self.product_name,
            "opened_at": self.opened_at,
            "opened_by": self.opened_by,
            "regions": sorted(self.regions),
            "sample_ids": self.sample_ids,
            "report_ids": self.report_ids,
            "appeal_ids": self.appeal_ids,
            "status": self.status,
            "evidence_complete": self.evidence_complete,
            "evidence_checklist": self.evidence_checklist,
            "consolidated_from": self.consolidated_from,
            "merged_into": self.merged_into,
            "decision_ids": self.decision_ids,
        }


@dataclass
class Decision:
    """分级执法决定：下架 / 召回 / 恢复销售。"""

    decision_id: str
    case_id: str
    level: str  # delist | recall | restore
    scope: str  # batch | regional | all_market
    reason: str
    decided_by: str
    at: str
    effective: bool = True
    basis_report_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "case_id": self.case_id,
            "level": self.level,
            "scope": self.scope,
            "reason": self.reason,
            "decided_by": self.decided_by,
            "at": self.at,
            "effective": self.effective,
            "basis_report_ids": self.basis_report_ids,
        }


# ---------------------------------------------------------------- 视图模型

@dataclass
class MerchantView:
    """商户视图：只能看自己的补件与整改期限。"""

    merchant_id: str
    appeals: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class PublicView:
    """公众查询视图：仅脱敏结论与日期。"""

    batch_no_masked: str
    product_name: str
    producer_masked: str
    conclusion: str
    decision_level: Optional[str]
    report_date: str
    decision_date: Optional[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "batch_no": self.batch_no_masked,
            "product_name": self.product_name,
            "producer": self.producer_masked,
            "conclusion": self.conclusion,
            "decision_level": self.decision_level,
            "report_date": self.report_date[:10],
            "decision_date": (self.decision_date or "")[:10] or None,
        }


@dataclass
class TraceView:
    """内部追溯视图：从报告追到样品去向、每次经手人和最终执法动作。"""

    report_id: str
    sample: dict[str, Any]
    seal: dict[str, Any]
    custody: list[dict[str, Any]]
    case: dict[str, Any]
    decisions: list[dict[str, Any]]
    chain_hash: str
