"""风险研判与证据规则。

- 同一生产批次的样品跨地区汇入同一案件，按风险规则分级；
- 证据齐全才允许发布分级执法决定；
- 结果判定以报告出具时钉死的标准版本为准。
"""

from __future__ import annotations

from typing import Optional

from .errors import ApiError

RISK_RANK = {"high": 3, "medium": 2, "cleared": 1}
PENDING_RETEST_STATUSES = ("submitted", "supplement_requested", "accepted", "in_retest")

RETEST_STATUS_LABELS = {
    "submitted": "待审核",
    "supplement_requested": "待补件",
    "accepted": "已受理",
    "in_retest": "复检中",
    "completed": "已完成",
    "rejected": "已驳回",
}

_SUGGESTED_ACTION = {"high": "recall", "medium": "delist", "cleared": "restore"}


def evaluate_items(limits: dict, items) -> tuple[list[dict], str]:
    """按标准限值判定检测项目，返回（带结论的项目列表, 总结论）。"""
    if not isinstance(items, list) or not items:
        raise ApiError(400, "检测项目不能为空", "bad_items")
    checked = []
    for raw in items:
        if not isinstance(raw, dict):
            raise ApiError(400, "检测项目格式不正确", "bad_items")
        name = raw.get("name")
        if not name or name not in limits:
            raise ApiError(400, f"标准中未定义检测项目：{name}", "unknown_item")
        spec = limits[name]
        try:
            value = float(raw.get("value"))
        except (TypeError, ValueError):
            raise ApiError(400, f"检测项目 {name} 的结果值必须是数值", "bad_value")
        rule = spec.get("rule", "max")
        limit = spec.get("limit")
        if rule == "forbidden":
            ok = value <= 0  # 超范围使用的添加剂：不得检出
        else:
            ok = value <= float(limit)
        checked.append(
            {
                "name": name,
                "value": value,
                "unit": spec.get("unit", ""),
                "limit": limit,
                "rule": rule,
                "verdict": "合格" if ok else "不合格",
            }
        )
    conclusion = "不合格" if any(it["verdict"] == "不合格" for it in checked) else "合格"
    return checked, conclusion


def final_reports(store, case) -> dict:
    """每个样品的最终报告：复检结论优先于初检结论。"""
    finals = {}
    for sample_id in case.sample_ids:
        reports = store.reports_for_sample(sample_id)
        if not reports:
            finals[sample_id] = None
            continue
        retests = [r for r in reports if r.kind == "retest"]
        pool = retests if retests else reports
        finals[sample_id] = max(pool, key=lambda r: r.uploaded_at)
    return finals


def assess_case(store, case) -> tuple[str, list[str]]:
    """按风险规则给出案件风险等级与理由。"""
    finals = final_reports(store, case)
    bad = [(sid, r) for sid, r in finals.items() if r is not None and r.conclusion == "不合格"]
    if not bad:
        return "cleared", ["全部样品最终结论为合格"]
    regions = {store.samples[sid].region for sid, _ in bad}
    reasons = []
    if len(regions) >= 2:
        reasons.append(f"不合格样品覆盖 {len(regions)} 个地区")
    if any(r.kind == "retest" for _, r in bad):
        reasons.append("复检结论维持不合格")
    if len(bad) >= 2:
        reasons.append(f"{len(bad)} 份样品最终结论不合格")
    high = len(regions) >= 2 or any(r.kind == "retest" for _, r in bad) or len(bad) >= 2
    if not reasons:
        reasons.append("单一地区单一样品不合格")
    return ("high" if high else "medium"), reasons


def evidence_status(store, case) -> tuple[bool, list[str]]:
    """证据齐全：批次内全部样品均有检测报告，且没有未办结的复检申请。"""
    missing = []
    for sample_id in case.sample_ids:
        if not store.reports_for_sample(sample_id):
            missing.append(f"样品 {sample_id} 尚无检测报告")
    case_samples = set(case.sample_ids)
    for app in store.retest_apps.values():
        if app.sample_id in case_samples and app.status in PENDING_RETEST_STATUSES:
            label = RETEST_STATUS_LABELS.get(app.status, app.status)
            missing.append(f"复检申请 {app.id} 未办结（{label}）")
    return (not missing), missing


def suggested_action(risk_level: str) -> Optional[str]:
    return _SUGGESTED_ACTION.get(risk_level)


def check_decision(level: str, risk_level: str, complete: bool, missing: list[str]) -> Optional[str]:
    """校验分级决定是否允许发布，返回错误消息或 None。"""
    if not complete:
        return "证据未齐全：" + "；".join(missing)
    if level == "recall" and risk_level != "high":
        return "召回决定要求风险等级为 high（跨地区不合格或复检维持不合格）"
    if level == "delist" and risk_level not in ("medium", "high"):
        return "下架决定要求至少存在一份不合格结论"
    if level == "restore" and risk_level != "cleared":
        return "恢复销售要求全部样品最终结论为合格"
    return None
