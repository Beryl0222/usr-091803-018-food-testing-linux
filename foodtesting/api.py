"""HTTP 接口层：路由、鉴权、序列化与业务编排。

面向四类调用方：
- 监管人员：抽样计划、样品登记、流转、案件研判、发布分级决定、审计追溯；
- 实验室：上传检测结果（重试只受理一次）、查看本人相关样品与报告；
- 商户：申请复检、补件，仅查看自己的补件要求与整改期限；
- 公众：无需令牌，只查脱敏结论与日期。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from http.server import BaseHTTPRequestHandler
from urllib.parse import unquote, urlsplit

from . import health_payload
from .auth import authenticate
from .errors import ApiError
from .models import (
    Case,
    CustodyEvent,
    Decision,
    Report,
    RetestApplication,
    Sample,
    SamplingPlan,
    StandardVersion,
    iso,
    utcnow,
)
from .rules import (
    PENDING_RETEST_STATUSES,
    RETEST_STATUS_LABELS,
    RISK_RANK,
    assess_case,
    check_decision,
    evaluate_items,
    evidence_status,
    final_reports,
    suggested_action,
)
from .store import Store

LEVEL_LABELS = {"delist": "下架", "recall": "召回", "restore": "恢复销售"}
KIND_LABELS = {"initial": "初检", "retest": "复检"}
CUSTODY_ACTIONS = {"ship": "运输交接", "receive": "签收", "seal": "封存", "return": "退还", "dispose": "处置"}
STATUS_BY_ACTION = {"ship": "运输中", "receive": "已接收", "seal": "已封存", "return": "已退还", "dispose": "已处置"}


# ---------------------------------------------------------------- 工具


def require(body: dict, *fields: str) -> None:
    missing = [f for f in fields if body.get(f) in (None, "")]
    if missing:
        raise ApiError(400, f"缺少必填字段：{', '.join(missing)}", "missing_field")


def parse_date(value, field: str) -> str:
    if not isinstance(value, str):
        raise ApiError(400, f"{field}必须是 YYYY-MM-DD 日期字符串", "bad_date")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise ApiError(400, f"{field}必须是 YYYY-MM-DD 日期", "bad_date")
    return value


def _get(mapping: dict, key, what: str):
    obj = mapping.get(key)
    if obj is None:
        raise ApiError(404, f"{what}不存在：{key}", "not_found")
    return obj


# ---------------------------------------------------------------- 视图


def standard_view(std: StandardVersion) -> dict:
    return {
        "id": std.id,
        "code": std.code,
        "title": std.title,
        "version": std.version,
        "limits": std.limits,
        "effective_from": std.effective_from,
        "status": std.status,
        "created_by": std.created_by,
        "created_at": iso(std.created_at),
    }


def plan_view(plan: SamplingPlan) -> dict:
    return {
        "id": plan.id,
        "title": plan.title,
        "region": plan.region,
        "category": plan.category,
        "note": plan.note,
        "created_by": plan.created_by,
        "created_at": iso(plan.created_at),
    }


def custody_view(ev: CustodyEvent) -> dict:
    return {
        "seq": ev.seq,
        "action": ev.action,
        "action_label": CUSTODY_ACTIONS.get(ev.action, "抽样登记"),
        "from_holder": ev.from_holder,
        "to_holder": ev.to_holder,
        "handler": ev.handler,
        "note": ev.note,
        "at": iso(ev.at),
    }


def sample_view(sample: Sample) -> dict:
    return {
        "id": sample.id,
        "plan_id": sample.plan_id,
        "seal_no": sample.seal_no,
        "product_name": sample.product_name,
        "batch_no": sample.batch_no,
        "merchant_id": sample.merchant_id,
        "region": sample.region,
        "production_date": sample.production_date,
        "sampled_at": sample.sampled_at,
        "status": sample.status,
        "current_holder": sample.current_holder,
        "registered_by": sample.registered_by,
        "created_at": iso(sample.created_at),
    }


def report_view(report: Report) -> dict:
    return {
        "id": report.id,
        "sample_id": report.sample_id,
        "lab_id": report.lab_id,
        "kind": report.kind,
        "kind_label": KIND_LABELS.get(report.kind, report.kind),
        "conclusion": report.conclusion,
        "items": report.items,
        "standard": report.standard,
        "raw_ref": report.raw_ref,
        "retest_application_id": report.retest_application_id,
        "uploaded_at": iso(report.uploaded_at),
    }


def retest_view(app: RetestApplication) -> dict:
    return {
        "id": app.id,
        "report_id": app.report_id,
        "sample_id": app.sample_id,
        "merchant_id": app.merchant_id,
        "reason": app.reason,
        "materials": list(app.materials),
        "status": app.status,
        "status_label": RETEST_STATUS_LABELS.get(app.status, app.status),
        "supplement_request": app.supplement_request,
        "supplement_deadline": app.supplement_deadline,
        "assigned_lab_id": app.assigned_lab_id,
        "created_at": iso(app.created_at),
        "updated_at": iso(app.updated_at),
    }


def decision_view(decision: Decision) -> dict:
    return {
        "id": decision.id,
        "case_id": decision.case_id,
        "level": decision.level,
        "level_label": LEVEL_LABELS.get(decision.level, decision.level),
        "scope_note": decision.scope_note,
        "rectification": decision.rectification,
        "published_by": decision.published_by,
        "published_at": iso(decision.published_at),
        "superseded_by": decision.superseded_by,
    }


def case_view(store: Store, case: Case, detail: bool = False) -> dict:
    complete, missing = evidence_status(store, case)
    action = suggested_action(case.risk_level)
    view = {
        "id": case.id,
        "batch_no": case.batch_no,
        "product_name": case.product_name,
        "status": case.status,
        "risk_level": case.risk_level,
        "risk_reasons": list(case.risk_reasons),
        "suggested_action": action,
        "suggested_action_label": LEVEL_LABELS.get(action),
        "regions": sorted({store.samples[sid].region for sid in case.sample_ids}),
        "sample_ids": list(case.sample_ids),
        "report_ids": list(case.report_ids),
        "evidence": {"complete": complete, "missing": missing},
        "decision_ids": list(case.decision_ids),
        "created_at": iso(case.created_at),
        "updated_at": iso(case.updated_at),
    }
    if detail:
        finals = final_reports(store, case)
        view["samples"] = [sample_view(store.samples[sid]) for sid in case.sample_ids]
        view["reports"] = [report_view(store.reports[rid]) for rid in case.report_ids]
        view["decisions"] = [decision_view(store.decisions[did]) for did in case.decision_ids]
        view["final_conclusions"] = {
            sid: (rep.conclusion if rep else None) for sid, rep in finals.items()
        }
    return view


def event_view(event) -> dict:
    return {
        "seq": event.seq,
        "type": event.type,
        "actor_id": event.actor_id,
        "entity_type": event.entity_type,
        "entity_id": event.entity_id,
        "summary": event.summary,
        "at": iso(event.at),
        "prev_hash": event.prev_hash,
        "hash": event.hash,
    }


# ---------------------------------------------------------------- 业务编排


def create_standard(store: Store, user, body: dict):
    require(body, "code", "title", "version", "effective_from", "limits")
    parse_date(body["effective_from"], "实施日期")
    limits = body["limits"]
    if not isinstance(limits, dict) or not limits:
        raise ApiError(400, "限值表不能为空", "bad_limits")
    normalized = {}
    for name, spec in limits.items():
        if not isinstance(spec, dict):
            raise ApiError(400, f"检测项目 {name} 的限值格式不正确", "bad_limits")
        rule = spec.get("rule", "max")
        if rule not in ("max", "forbidden"):
            raise ApiError(400, f"检测项目 {name} 的判定规则须为 max 或 forbidden", "bad_limits")
        limit = spec.get("limit")
        if isinstance(limit, bool) or not isinstance(limit, (int, float)):
            raise ApiError(400, f"检测项目 {name} 的限值必须是数值", "bad_limits")
        normalized[name] = {"limit": float(limit), "unit": str(spec.get("unit", "")), "rule": rule}
    existing = store.effective_standard(body["code"])
    if existing and existing.version == body["version"]:
        raise ApiError(409, f"标准 {body['code']} 版本 {body['version']} 已存在", "standard_exists")
    if existing:
        # 标准更新：旧版本标记作废，但历史报告仍钉在旧版本上，不倒改
        existing.status = "superseded"
    std = StandardVersion(
        id=store.next_id("STD"),
        code=body["code"],
        title=body["title"],
        version=body["version"],
        limits=normalized,
        effective_from=body["effective_from"],
        created_by=user.id,
    )
    store.standards[std.id] = std
    summary = f"标准 {std.code} 版本 {std.version} 生效"
    if existing:
        summary += f"，替代版本 {existing.version}（历史报告仍按原版本留存）"
    store.record("standard.created", user.id, "standard", std.id, summary)
    return 201, {"standard": standard_view(std)}


def create_plan(store: Store, user, body: dict):
    require(body, "title", "region", "category")
    plan = SamplingPlan(
        id=store.next_id("PLN"),
        title=body["title"],
        region=body["region"],
        category=body["category"],
        note=body.get("note", ""),
        created_by=user.id,
    )
    store.plans[plan.id] = plan
    store.record("plan.created", user.id, "plan", plan.id, f"抽样计划《{plan.title}》")
    return 201, {"plan": plan_view(plan)}


def _attach_to_case(store: Store, case: Case, sample: Sample, actor_id: str) -> None:
    if sample.id in case.sample_ids:
        return
    case.sample_ids.append(sample.id)
    case.updated_at = utcnow()
    if case.status == "decided":
        case.status = "open"  # 批次内新增样品，需重新研判
    store.record(
        "case.merged",
        actor_id,
        "case",
        case.id,
        f"样品 {sample.id}（封签 {sample.seal_no}）并入批次 {case.batch_no} 统一研判，不重复立案",
    )


def register_sample(store: Store, user, body: dict):
    require(body, "plan_id", "seal_no", "product_name", "batch_no", "merchant_id", "region", "production_date", "sampled_at")
    _get(store.plans, body["plan_id"], "抽样计划")
    merchant = store.users.get(body["merchant_id"])
    if merchant is None or merchant.role != "merchant":
        raise ApiError(400, f"商户不存在：{body['merchant_id']}", "unknown_merchant")
    if any(s.seal_no == body["seal_no"] for s in store.samples.values()):
        raise ApiError(409, f"封签编号已存在：{body['seal_no']}", "seal_exists")
    parse_date(body["production_date"], "生产日期")
    parse_date(body["sampled_at"], "抽样日期")
    sample = Sample(
        id=store.next_id("SMP"),
        plan_id=body["plan_id"],
        seal_no=body["seal_no"],
        product_name=body["product_name"],
        batch_no=body["batch_no"],
        merchant_id=body["merchant_id"],
        region=body["region"],
        production_date=body["production_date"],
        sampled_at=body["sampled_at"],
        registered_by=user.id,
        current_holder=user.org or user.name,
        current_holder_id=user.id,
    )
    first = CustodyEvent(
        seq=1,
        action="register",
        from_holder="抽样现场",
        to_holder=sample.current_holder,
        handler=body.get("handler") or user.name,
        note="抽样登记",
        to_user_id=user.id,
    )
    sample.custody.append(first)
    store.samples[sample.id] = sample
    store.record(
        "sample.registered",
        user.id,
        "sample",
        sample.id,
        f"样品 {sample.id}（封签 {sample.seal_no}，批次 {sample.batch_no}，{sample.region}）登记",
    )
    case = store.case_for_batch(sample.batch_no)
    if case is not None:
        _attach_to_case(store, case, sample, user.id)
    return 201, {"sample": sample_view(sample), "custody": custody_view(first)}


def transfer_custody(store: Store, user, sample_id: str, body: dict):
    sample = _get(store.samples, sample_id, "样品")
    require(body, "action", "to_holder", "handler")
    action = body["action"]
    if action not in CUSTODY_ACTIONS:
        raise ApiError(400, f"流转动作须为：{', '.join(sorted(CUSTODY_ACTIONS))}", "bad_action")
    if user.role == "lab" and sample.current_holder_id != user.id:
        raise ApiError(403, "样品不在当前实验室手中，不得流转", "not_holder")
    to_user_id = body.get("to_user_id")
    if to_user_id is not None and to_user_id not in store.users:
        raise ApiError(400, f"接收人不存在：{to_user_id}", "unknown_recipient")
    ev = CustodyEvent(
        seq=len(sample.custody) + 1,
        action=action,
        from_holder=sample.current_holder,
        to_holder=body["to_holder"],
        handler=body["handler"],
        note=body.get("note", ""),
        to_user_id=to_user_id,
    )
    sample.custody.append(ev)
    sample.current_holder = ev.to_holder
    sample.current_holder_id = to_user_id
    sample.status = STATUS_BY_ACTION[action]
    store.record(
        "custody.transferred",
        user.id,
        "sample",
        sample.id,
        f"{CUSTODY_ACTIONS[action]}：{ev.from_holder} → {ev.to_holder}（经手人 {ev.handler}）",
    )
    return 201, {"sample": sample_view(sample), "custody": custody_view(ev)}


def _upload_fingerprint(body: dict, kind: str) -> str:
    payload = json.dumps(
        {
            "sample_id": body.get("sample_id"),
            "kind": kind,
            "standard_code": body.get("standard_code"),
            "standard_version": body.get("standard_version"),
            "items": body.get("items"),
            "raw_ref": body.get("raw_ref"),
            "retest_application_id": body.get("retest_application_id"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _update_case_for_report(store: Store, sample: Sample, report: Report, actor_id: str):
    """同一批次跨地区合并研判：报告归入批次案件并重新评估风险。"""
    case = store.case_for_batch(sample.batch_no)
    if case is None:
        if report.conclusion != "不合格":
            return None
        case = Case(id=store.next_id("CAS"), batch_no=sample.batch_no, product_name=sample.product_name)
        store.cases[case.id] = case
        store.record("case.created", actor_id, "case", case.id, f"批次 {case.batch_no} 出现不合格报告，立案研判")
    for other in store.samples.values():
        if other.batch_no == case.batch_no:
            _attach_to_case(store, case, other, actor_id)
    if report.id not in case.report_ids:
        case.report_ids.append(report.id)
    if report.conclusion == "不合格" and case.status == "decided":
        case.status = "open"
        store.record("case.reopened", actor_id, "case", case.id, "出现新的不合格证据，案件重新研判")
    old_level = case.risk_level
    case.risk_level, case.risk_reasons = assess_case(store, case)
    case.updated_at = utcnow()
    if case.risk_level != old_level:
        store.record(
            "case.reassessed",
            actor_id,
            "case",
            case.id,
            f"风险等级 {old_level} → {case.risk_level}（{'；'.join(case.risk_reasons)}）",
        )
    return case


def upload_result(store: Store, user, body: dict):
    require(body, "sample_id", "upload_key", "standard_code", "standard_version", "items", "raw_ref")
    sample = _get(store.samples, body["sample_id"], "样品")
    kind = body.get("kind", "initial")
    if kind not in ("initial", "retest"):
        raise ApiError(400, "报告类型须为 initial 或 retest", "bad_kind")

    retest_app = None
    if kind == "initial":
        if not any(ev.to_user_id == user.id for ev in sample.custody):
            raise ApiError(403, "实验室未接收该样品，不得上传结果", "not_involved")
    else:
        require(body, "retest_application_id")
        retest_app = _get(store.retest_apps, body["retest_application_id"], "复检申请")
        if retest_app.sample_id != sample.id:
            raise ApiError(400, "复检申请与样品不匹配", "retest_mismatch")
        if retest_app.status not in ("accepted", "in_retest"):
            raise ApiError(409, "复检申请未受理或已办结，不得上传复检报告", "retest_not_accepted")
        if retest_app.assigned_lab_id != user.id:
            raise ApiError(403, "复检须由受理时指定的实验室执行", "not_assigned_lab")

    # 幂等与“重试只受理一次”：同一上传键首次受理、第二次视为重试返回原报告、第三次起拒绝
    fingerprint = _upload_fingerprint(body, kind)
    entry = store.upload_index.get(body["upload_key"])
    if entry is not None:
        if entry["fingerprint"] != fingerprint:
            raise ApiError(409, "上传键已被其他内容占用", "upload_key_conflict")
        entry["attempts"] += 1
        if entry["attempts"] > 2:
            store.record(
                "report.upload_rejected",
                user.id,
                "report",
                entry["report_id"],
                f"上传键 {body['upload_key']} 第 {entry['attempts']} 次提交被拒：重试只受理一次",
            )
            raise ApiError(409, "结果上传重试仅受理一次，请勿重复提交", "retry_exhausted")
        report = store.reports[entry["report_id"]]
        return 200, {"report": report_view(report), "deduplicated": True, "attempt": entry["attempts"]}

    if kind == "initial":
        duplicate = any(
            r.sample_id == sample.id and r.lab_id == user.id and r.kind == "initial"
            for r in store.reports.values()
        )
        if duplicate:
            raise ApiError(409, "该样品的初检报告已存在", "report_exists")

    std = store.effective_standard(body["standard_code"])
    if std is None:
        raise ApiError(400, f"未知检测标准：{body['standard_code']}", "unknown_standard")
    if body["standard_version"] != std.version:
        raise ApiError(
            409,
            f"标准 {std.code} 现行版本为 {std.version}，请按现行版本出具新报告；历史报告仍按原版本留存",
            "standard_superseded",
        )
    checked, conclusion = evaluate_items(std.limits, body["items"])
    snapshot = {
        "code": std.code,
        "title": std.title,
        "version": std.version,
        "items": {it["name"]: {"limit": it["limit"], "unit": it["unit"], "rule": it["rule"]} for it in checked},
    }
    report = Report(
        id=store.next_id("RPT"),
        sample_id=sample.id,
        lab_id=user.id,
        kind=kind,
        upload_key=body["upload_key"],
        standard=snapshot,
        items=checked,
        conclusion=conclusion,
        raw_ref=body["raw_ref"],
        retest_application_id=retest_app.id if retest_app else None,
    )
    store.reports[report.id] = report
    store.upload_index[body["upload_key"]] = {"report_id": report.id, "attempts": 1, "fingerprint": fingerprint}
    if retest_app is not None:
        retest_app.status = "completed"
        retest_app.updated_at = utcnow()
        store.record(
            "retest.completed",
            user.id,
            "retest_application",
            retest_app.id,
            f"复检报告 {report.id} 上传，结论{conclusion}",
        )
    store.record(
        "report.uploaded",
        user.id,
        "report",
        report.id,
        f"样品 {sample.id}（封签 {sample.seal_no}）{KIND_LABELS[kind]}结论：{conclusion}，标准 {std.code} 版本 {std.version}",
    )
    case = _update_case_for_report(store, sample, report, user.id)
    payload = {"report": report_view(report), "deduplicated": False, "attempt": 1}
    if case is not None:
        payload["case"] = case_view(store, case)
    return 201, payload


def apply_retest(store: Store, user, report_id: str, body: dict):
    report = _get(store.reports, report_id, "检测报告")
    sample = store.samples[report.sample_id]
    if sample.merchant_id != user.id:
        raise ApiError(404, "未找到相关报告", "not_found")
    require(body, "reason")
    materials = body.get("materials") or []
    if not isinstance(materials, list) or any(not isinstance(m, str) for m in materials):
        raise ApiError(400, "复检材料须为字符串列表", "bad_materials")
    pending = any(
        a.report_id == report.id and a.status in PENDING_RETEST_STATUSES for a in store.retest_apps.values()
    )
    if pending:
        raise ApiError(409, "该报告已存在未办结的复检申请", "retest_pending")
    app = RetestApplication(
        id=store.next_id("RTA"),
        report_id=report.id,
        sample_id=sample.id,
        merchant_id=user.id,
        reason=body["reason"],
        materials=list(materials),
    )
    store.retest_apps[app.id] = app
    store.record("retest.submitted", user.id, "retest_application", app.id, f"商户对报告 {report.id} 申请复检")
    return 201, {"application": retest_view(app)}


def review_retest(store: Store, user, app_id: str, body: dict):
    app = _get(store.retest_apps, app_id, "复检申请")
    require(body, "action")
    action = body["action"]
    if app.status != "submitted":
        raise ApiError(409, f"当前状态（{RETEST_STATUS_LABELS.get(app.status)}）不可审核", "bad_state")
    if action == "accept":
        require(body, "lab_id")
        lab = store.users.get(body["lab_id"])
        if lab is None or lab.role != "lab":
            raise ApiError(400, f"指定实验室不存在：{body['lab_id']}", "unknown_lab")
        app.status = "accepted"
        app.assigned_lab_id = lab.id
        summary = f"复检申请 {app.id} 受理，指定 {lab.name}"
    elif action == "request_supplement":
        require(body, "supplement_request", "deadline")
        parse_date(body["deadline"], "补件期限")
        app.status = "supplement_requested"
        app.supplement_request = body["supplement_request"]
        app.supplement_deadline = body["deadline"]
        summary = f"复检申请 {app.id} 要求补件，期限 {app.supplement_deadline}"
    elif action == "reject":
        app.status = "rejected"
        summary = f"复检申请 {app.id} 被驳回"
    else:
        raise ApiError(400, "审核动作须为 accept / request_supplement / reject", "bad_action")
    app.handled_by = user.id
    app.updated_at = utcnow()
    store.record("retest.reviewed", user.id, "retest_application", app.id, summary)
    return 200, {"application": retest_view(app)}


def submit_materials(store: Store, user, app_id: str, body: dict):
    app = _get(store.retest_apps, app_id, "复检申请")
    if app.merchant_id != user.id:
        raise ApiError(404, "未找到相关复检申请", "not_found")
    if app.status != "supplement_requested":
        raise ApiError(409, "当前无需补件", "bad_state")
    materials = body.get("materials")
    if not isinstance(materials, list) or not materials or any(not isinstance(m, str) for m in materials):
        raise ApiError(400, "补件材料须为非空字符串列表", "bad_materials")
    app.materials.extend(materials)
    app.status = "submitted"
    app.updated_at = utcnow()
    store.record("retest.materials_submitted", user.id, "retest_application", app.id, f"商户补交 {len(materials)} 份材料")
    return 200, {"application": retest_view(app)}


def publish_decision(store: Store, user, case_id: str, body: dict):
    case = _get(store.cases, case_id, "案件")
    require(body, "level")
    level = body["level"]
    if level not in LEVEL_LABELS:
        raise ApiError(400, f"决定级别须为：{', '.join(sorted(LEVEL_LABELS))}", "bad_level")
    rectification = None
    if body.get("rectification") is not None:
        rect = body["rectification"]
        require(rect, "requirement", "deadline")
        parse_date(rect["deadline"], "整改期限")
        merchant_ids = sorted({store.samples[sid].merchant_id for sid in case.sample_ids})
        rectification = {
            "requirement": rect["requirement"],
            "deadline": rect["deadline"],
            "merchant_ids": merchant_ids,
        }
    complete, missing = evidence_status(store, case)
    error = check_decision(level, case.risk_level, complete, missing)
    if error:
        raise ApiError(409, error, "decision_blocked")
    decision = Decision(
        id=store.next_id("DEC"),
        case_id=case.id,
        level=level,
        scope_note=body.get("scope_note", ""),
        rectification=rectification,
        published_by=user.id,
    )
    for did in case.decision_ids:
        prev = store.decisions[did]
        if prev.superseded_by is None:
            prev.superseded_by = decision.id
    store.decisions[decision.id] = decision
    case.decision_ids.append(decision.id)
    case.status = "decided"
    case.updated_at = utcnow()
    store.record(
        "decision.published",
        user.id,
        "case",
        case.id,
        f"批次 {case.batch_no} 发布{LEVEL_LABELS[level]}决定（{decision.id}）",
    )
    return 201, {"decision": decision_view(decision), "case": case_view(store, case)}


# ---------------------------------------------------------------- 查询编排


def list_samples(store: Store, user):
    if user.role == "regulator":
        samples = list(store.samples.values())
    else:
        samples = [s for s in store.samples.values() if store.sample_involves_user(s, user.id)]
    samples.sort(key=lambda s: s.created_at)
    return 200, {"samples": [sample_view(s) for s in samples]}


def get_sample(store: Store, user, sample_id: str):
    sample = _get(store.samples, sample_id, "样品")
    if user.role == "lab" and not store.sample_involves_user(sample, user.id):
        raise ApiError(404, "未找到相关样品", "not_found")
    return 200, {"sample": sample_view(sample), "custody": [custody_view(ev) for ev in sample.custody]}


def list_custody(store: Store, user, sample_id: str):
    sample = _get(store.samples, sample_id, "样品")
    if user.role == "lab" and not store.sample_involves_user(sample, user.id):
        raise ApiError(404, "未找到相关样品", "not_found")
    return 200, {"sample_id": sample.id, "custody": [custody_view(ev) for ev in sample.custody]}


def get_report(store: Store, user, report_id: str):
    report = _get(store.reports, report_id, "检测报告")
    if user.role == "lab" and report.lab_id != user.id:
        raise ApiError(404, "未找到相关报告", "not_found")
    return 200, {"report": report_view(report)}


def trace_report(store: Store, report_id: str):
    """内部追溯：从任一报告追到样品去向、每次经手人与最终执法动作。"""
    report = _get(store.reports, report_id, "检测报告")
    sample = store.samples[report.sample_id]
    case = store.case_for_batch(sample.batch_no)
    decisions = []
    final_action = None
    if case is not None:
        decisions = [store.decisions[did] for did in case.decision_ids]
        active = [d for d in decisions if d.superseded_by is None]
        if active:
            final_action = active[-1]
    last = sample.custody[-1]
    return 200, {
        "report": report_view(report),
        "sample": sample_view(sample),
        "custody": [custody_view(ev) for ev in sample.custody],
        "current_whereabouts": {
            "holder": sample.current_holder,
            "status": sample.status,
            "since": iso(last.at),
            "last_handler": last.handler,
        },
        "case": case_view(store, case) if case else None,
        "decisions": [decision_view(d) for d in decisions],
        "final_action": decision_view(final_action) if final_action else None,
    }


def get_retest_app(store: Store, user, app_id: str):
    app = _get(store.retest_apps, app_id, "复检申请")
    if user.role == "merchant" and app.merchant_id != user.id:
        raise ApiError(404, "未找到相关复检申请", "not_found")
    if user.role == "lab" and app.assigned_lab_id != user.id:
        raise ApiError(404, "未找到相关复检申请", "not_found")
    return 200, {"application": retest_view(app)}


def list_cases(store: Store):
    cases = sorted(
        store.cases.values(),
        key=lambda c: (RISK_RANK.get(c.risk_level, 0), c.updated_at),
        reverse=True,
    )
    return 200, {"cases": [case_view(store, c) for c in cases]}


def get_case(store: Store, case_id: str):
    case = _get(store.cases, case_id, "案件")
    return 200, {"case": case_view(store, case, detail=True)}


def merchant_matters(store: Store, user):
    """商户视角：仅自己的补件要求与整改期限。"""
    supplements = []
    for app in store.retest_apps.values():
        if app.merchant_id == user.id and app.supplement_request:
            supplements.append(
                {
                    "retest_application_id": app.id,
                    "report_id": app.report_id,
                    "request": app.supplement_request,
                    "deadline": app.supplement_deadline,
                    "status": app.status,
                    "status_label": RETEST_STATUS_LABELS.get(app.status, app.status),
                }
            )
    rectifications = []
    for case in store.cases.values():
        for did in case.decision_ids:
            decision = store.decisions[did]
            if decision.superseded_by is not None or not decision.rectification:
                continue
            if user.id in decision.rectification["merchant_ids"]:
                rectifications.append(
                    {
                        "decision_id": decision.id,
                        "case_id": case.id,
                        "level": decision.level,
                        "level_label": LEVEL_LABELS.get(decision.level, decision.level),
                        "product_name": case.product_name,
                        "batch_no": case.batch_no,
                        "requirement": decision.rectification["requirement"],
                        "deadline": decision.rectification["deadline"],
                    }
                )
    return 200, {"merchant_id": user.id, "supplements": supplements, "rectifications": rectifications}


def public_batch_view(store: Store, batch_no: str):
    """公众视图：只呈现脱敏结论与日期，不含商户、实验室、封签与原始数值。"""
    samples = [s for s in store.samples.values() if s.batch_no == batch_no]
    if not samples:
        raise ApiError(404, "未查询到该批次信息", "not_found")
    conclusions = []
    for sample in sorted(samples, key=lambda s: s.id):
        reports = store.reports_for_sample(sample.id)
        if not reports:
            continue
        retests = [r for r in reports if r.kind == "retest"]
        final = (retests or reports)[-1]
        conclusions.append(
            {
                "conclusion": final.conclusion,
                "kind": final.kind,
                "kind_label": KIND_LABELS.get(final.kind, final.kind),
                "reported_at": iso(final.uploaded_at),
            }
        )
    decision_payload = None
    case = store.case_for_batch(batch_no)
    if case is not None:
        active = [store.decisions[did] for did in case.decision_ids if store.decisions[did].superseded_by is None]
        if active:
            latest = active[-1]
            decision_payload = {
                "level": latest.level,
                "level_label": LEVEL_LABELS.get(latest.level, latest.level),
                "published_at": iso(latest.published_at),
            }
    return 200, {
        "batch_no": batch_no,
        "product_name": samples[0].product_name,
        "conclusions": conclusions,
        "decision": decision_payload,
    }


# ---------------------------------------------------------------- 路由


class Route:
    def __init__(self, method: str, pattern: str, handler, roles):
        self.method = method
        self.pattern = re.compile(pattern)
        self.handler = handler
        self.roles = roles  # None 表示公众接口


class App:
    def __init__(self, store: Store | None = None):
        self.store = store or Store()
        self.routes = [
            Route("GET", r"/health", lambda app, user, p, b: (200, health_payload()), None),
            Route("GET", r"/api/standards", lambda app, user, p, b: (200, {"standards": [standard_view(s) for s in sorted(app.store.standards.values(), key=lambda x: (x.code, x.created_at))]}), ("regulator", "lab")),
            Route("POST", r"/api/standards", lambda app, user, p, b: create_standard(app.store, user, b), ("regulator",)),
            Route("POST", r"/api/plans", lambda app, user, p, b: create_plan(app.store, user, b), ("regulator",)),
            Route("GET", r"/api/plans", lambda app, user, p, b: (200, {"plans": [plan_view(x) for x in app.store.plans.values()]}), ("regulator",)),
            Route("POST", r"/api/samples", lambda app, user, p, b: register_sample(app.store, user, b), ("regulator",)),
            Route("GET", r"/api/samples", lambda app, user, p, b: list_samples(app.store, user), ("regulator", "lab")),
            Route("GET", r"/api/samples/(?P<sample_id>[^/]+)", lambda app, user, p, b: get_sample(app.store, user, p["sample_id"]), ("regulator", "lab")),
            Route("POST", r"/api/samples/(?P<sample_id>[^/]+)/custody", lambda app, user, p, b: transfer_custody(app.store, user, p["sample_id"], b), ("regulator", "lab")),
            Route("GET", r"/api/samples/(?P<sample_id>[^/]+)/custody", lambda app, user, p, b: list_custody(app.store, user, p["sample_id"]), ("regulator", "lab")),
            Route("POST", r"/api/results", lambda app, user, p, b: upload_result(app.store, user, b), ("lab",)),
            Route("GET", r"/api/reports/(?P<report_id>[^/]+)", lambda app, user, p, b: get_report(app.store, user, p["report_id"]), ("regulator", "lab")),
            Route("GET", r"/api/reports/(?P<report_id>[^/]+)/trace", lambda app, user, p, b: trace_report(app.store, p["report_id"]), ("regulator",)),
            Route("POST", r"/api/reports/(?P<report_id>[^/]+)/retest-applications", lambda app, user, p, b: apply_retest(app.store, user, p["report_id"], b), ("merchant",)),
            Route("GET", r"/api/retest-applications/(?P<app_id>[^/]+)", lambda app, user, p, b: get_retest_app(app.store, user, p["app_id"]), ("regulator", "merchant", "lab")),
            Route("POST", r"/api/retest-applications/(?P<app_id>[^/]+)/review", lambda app, user, p, b: review_retest(app.store, user, p["app_id"], b), ("regulator",)),
            Route("POST", r"/api/retest-applications/(?P<app_id>[^/]+)/materials", lambda app, user, p, b: submit_materials(app.store, user, p["app_id"], b), ("merchant",)),
            Route("GET", r"/api/cases", lambda app, user, p, b: list_cases(app.store), ("regulator",)),
            Route("GET", r"/api/cases/(?P<case_id>[^/]+)", lambda app, user, p, b: get_case(app.store, p["case_id"]), ("regulator",)),
            Route("POST", r"/api/cases/(?P<case_id>[^/]+)/decisions", lambda app, user, p, b: publish_decision(app.store, user, p["case_id"], b), ("regulator",)),
            Route("GET", r"/api/merchant/matters", lambda app, user, p, b: merchant_matters(app.store, user), ("merchant",)),
            Route("GET", r"/api/audit/events", lambda app, user, p, b: (200, {"events": [event_view(e) for e in app.store.events]}), ("regulator",)),
            Route("GET", r"/api/audit/verify", self._verify, ("regulator",)),
            Route("GET", r"/api/public/batches/(?P<batch_no>[^/]+)", lambda app, user, p, b: public_batch_view(app.store, p["batch_no"]), None),
        ]

    @staticmethod
    def _verify(app, user, params, body):
        ok, bad_seq = app.store.verify_chain()
        return 200, {"ok": ok, "events": len(app.store.events), "first_bad_seq": bad_seq}


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def _dispatch(self, method: str):
            try:
                path = unquote(urlsplit(self.path).path)
                for route in app.routes:
                    if route.method != method:
                        continue
                    match = route.pattern.fullmatch(path)
                    if not match:
                        continue
                    user = None
                    if route.roles is not None:
                        user = authenticate(app.store, self.headers.get("X-Auth-Token"))
                        if user is None:
                            raise ApiError(401, "未认证或令牌无效", "unauthorized")
                        if user.role not in route.roles:
                            raise ApiError(403, "当前角色无权访问该接口", "forbidden")
                    body = self._read_json() if method == "POST" else {}
                    with app.store.lock:
                        status, payload = route.handler(app, user, match.groupdict(), body)
                    self._send(status, payload)
                    return
                raise ApiError(404, "接口不存在", "not_found")
            except ApiError as error:
                self._send(error.status, {"error": error.code, "message": error.message})
            except BrokenPipeError:
                pass
            except Exception as error:  # noqa: BLE001 - 兜底，避免连接悬挂
                self._send(500, {"error": "internal_error", "message": str(error)})

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise ApiError(400, "请求体不是合法 JSON", "bad_json")
            if not isinstance(data, dict):
                raise ApiError(400, "请求体必须是 JSON 对象", "bad_json")
            return data

        def _send(self, status: int, payload: dict):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    return Handler
