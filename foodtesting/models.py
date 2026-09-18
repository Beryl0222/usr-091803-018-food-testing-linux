"""领域对象：糕点抽检证据链中的核心实体。

抽样计划 -> 样品（封签）-> 检测报告（固定标准版本快照）-> 复检申请，
同一生产批次的样品跨地区汇入同一个研判案件，最终形成分级执法决定。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class User:
    id: str
    name: str
    role: str  # regulator / lab / merchant
    token: str
    org: str = ""


@dataclass
class StandardVersion:
    """检测标准的一个版本。标准更新只新增版本，旧版本随历史报告留存。"""

    id: str
    code: str
    title: str
    version: str
    limits: dict[str, dict[str, Any]]  # 项目 -> {"limit": float, "unit": str, "rule": "max"|"forbidden"}
    effective_from: str
    status: str = "effective"  # effective / superseded
    created_by: str = ""
    created_at: datetime = field(default_factory=utcnow)


@dataclass
class SamplingPlan:
    id: str
    title: str
    region: str
    category: str
    note: str
    created_by: str
    created_at: datetime = field(default_factory=utcnow)


@dataclass
class CustodyEvent:
    """样品流转记录：每次交接都留下经手人。"""

    seq: int
    action: str  # register / ship / receive / seal / return / dispose
    from_holder: str
    to_holder: str
    handler: str
    note: str = ""
    to_user_id: Optional[str] = None  # 接收方对应的系统账号（如有）
    at: datetime = field(default_factory=utcnow)


@dataclass
class Sample:
    id: str
    plan_id: str
    seal_no: str  # 封签编号，全局唯一
    product_name: str
    batch_no: str  # 生产批次，跨地区合并研判的键
    merchant_id: str
    region: str
    production_date: str
    sampled_at: str
    registered_by: str
    status: str = "已登记"
    current_holder: str = ""
    current_holder_id: Optional[str] = None
    custody: list[CustodyEvent] = field(default_factory=list)
    created_at: datetime = field(default_factory=utcnow)


@dataclass
class Report:
    """检测报告。standard 为出具报告时钉死的标准版本快照，不随后续标准更新改变。"""

    id: str
    sample_id: str
    lab_id: str
    kind: str  # initial / retest
    upload_key: str
    standard: dict[str, Any]
    items: list[dict[str, Any]]
    conclusion: str  # 合格 / 不合格
    raw_ref: str  # 原始结果记录号
    retest_application_id: Optional[str] = None
    uploaded_at: datetime = field(default_factory=utcnow)


@dataclass
class RetestApplication:
    id: str
    report_id: str
    sample_id: str
    merchant_id: str
    reason: str
    materials: list[str]
    status: str = "submitted"  # submitted / supplement_requested / accepted / completed / rejected
    supplement_request: Optional[str] = None
    supplement_deadline: Optional[str] = None
    assigned_lab_id: Optional[str] = None
    handled_by: Optional[str] = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class Decision:
    """分级执法决定：delist 下架 / recall 召回 / restore 恢复销售。"""

    id: str
    case_id: str
    level: str
    scope_note: str
    rectification: Optional[dict[str, Any]]  # {"requirement", "deadline", "merchant_ids"}
    published_by: str
    published_at: datetime = field(default_factory=utcnow)
    superseded_by: Optional[str] = None


@dataclass
class Case:
    """风险研判案件：同一生产批次跨地区只立一案。"""

    id: str
    batch_no: str
    product_name: str
    sample_ids: list[str] = field(default_factory=list)
    report_ids: list[str] = field(default_factory=list)
    status: str = "open"  # open / decided
    risk_level: str = "medium"  # high / medium / cleared
    risk_reasons: list[str] = field(default_factory=list)
    decision_ids: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class Event:
    """可核验记录：追加式事件，哈希链前后衔接。"""

    seq: int
    type: str
    actor_id: str
    entity_type: str
    entity_id: str
    summary: str
    at: datetime
    prev_hash: str
    hash: str
