"""实验室协作业务引擎。

把抽样计划、封签编号、检测方法版本、原始结果、复检材料接成一条可核验
记录，并落实四条关键规则：

* 结果上传按 upload_id 幂等，重试只受理一次；
* 报告签发时固化标准版本快照，标准更新不倒改旧报告；
* 同一生产批次跨地区抽到自动并案研判，不重复立案；
* 证据齐全前不得发布分级处置决定。
"""

from __future__ import annotations

import uuid
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
from .store import Store

LEVEL_RANK = {"delist": 1, "recall": 2}
SCOPE_RANK = {"batch": 1, "regional": 2, "all_market": 3}


class EngineError(Exception):
    """状态冲突类错误（重复操作、链路断裂、门禁不满足等）。"""


class ValidationError(EngineError):
    """输入不合法。"""


class NotFoundError(EngineError):
    """引用的资源不存在。"""


class ForbiddenError(EngineError):
    """角色或归属不允许该操作。"""


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _mask(value: str, head: int = 4) -> str:
    if len(value) <= head:
        return value[0] + "***"
    return value[:head] + "****" + value[-2:]


class CollabEngine:
    def __init__(self, store: Optional[Store] = None) -> None:
        self.store = store or Store()

    # ============================================================ 基础数据

    def register_actor(self, actor_id: str, name: str, role: str, org: str, region: str) -> Actor:
        if role not in {"regulator", "sampler", "lab", "merchant"}:
            raise ValidationError(f"未知角色 {role}")
        with self.store.lock:
            if actor_id in self.store.actors:
                raise EngineError(f"参与者 {actor_id} 已存在")
            actor = Actor(actor_id, name, role, org, region)
            self.store.actors[actor_id] = actor
            self.store.append_event("actor.registered", actor_id, actor.public_dict())
            return actor

    def _actor(self, actor_id: str, role: Optional[str] = None) -> Actor:
        actor = self.store.actors.get(actor_id)
        if actor is None:
            raise NotFoundError(f"参与者不存在：{actor_id}")
        if role and actor.role != role:
            raise ForbiddenError(f"需要 {role} 身份，实际为 {actor.role}")
        return actor

    def publish_standard(
        self,
        code: str,
        version: str,
        title: str,
        effective_at: str,
        limits: dict[str, dict[str, Any]],
        actor_id: str,
    ) -> Standard:
        self._actor(actor_id, "regulator")
        with self.store.lock:
            versions = self.store.standards.setdefault(code, {})
            if version in versions:
                raise EngineError(f"标准 {code}@{version} 已发布")
            standard = Standard(code, version, title, effective_at, limits)
            current = next((v for v in versions.values() if v.superseded_by is None), None)
            if current is not None:
                current.superseded_by = version
            versions[version] = standard
            self.store.append_event(
                "standard.published",
                actor_id,
                {"code": code, "version": version, "supersedes": current.version if current else None},
            )
            return standard

    def publish_method(
        self, method_code: str, version: str, title: str, item_key: str, effective_at: str, actor_id: str
    ) -> MethodVersion:
        self._actor(actor_id, "regulator")
        with self.store.lock:
            versions = self.store.methods.setdefault(method_code, {})
            if version in versions:
                raise EngineError(f"方法 {method_code}@{version} 已存在")
            method = MethodVersion(method_code, version, title, item_key, effective_at)
            versions[version] = method
            self.store.append_event("method.published", actor_id, method.as_dict())
            return method

    # ============================================================ 抽样与封签

    def create_plan(
        self, name: str, region: str, food_category: str, created_by: str, items: list[dict[str, str]]
    ) -> InspectionPlan:
        self._actor(created_by, "regulator")
        with self.store.lock:
            plan = InspectionPlan(
                plan_id=_new_id("plan"),
                name=name,
                region=region,
                food_category=food_category,
                created_by=created_by,
                created_at=utcnow(),
                items=items,
            )
            self.store.plans[plan.plan_id] = plan
            self.store.append_event("plan.created", created_by, plan.as_dict())
            return plan

    def register_sample(
        self,
        plan_id: str,
        region: str,
        merchant_id: str,
        product_name: str,
        producer: str,
        batch_no: str,
        location: str,
        sampled_by: str,
        seal_no: str,
        sampled_at: Optional[str] = None,
    ) -> Sample:
        """抽样落地即封签：样品与封签编号一次性绑定，写入首条保管事件。"""
        plan = self.store.plans.get(plan_id)
        if plan is None:
            raise NotFoundError(f"抽样计划不存在：{plan_id}")
        sampler = self._actor(sampled_by, "sampler")
        self._actor(merchant_id, "merchant")
        if seal_no in self.store.seals:
            raise EngineError(f"封签编号已使用：{seal_no}")
        with self.store.lock:
            sample = Sample(
                sample_id=_new_id("smp"),
                plan_id=plan_id,
                region=region or sampler.region,
                merchant_id=merchant_id,
                product_name=product_name,
                producer=producer,
                batch_no=batch_no,
                sampled_at=sampled_at or utcnow(),
                sampled_by=sampled_by,
                location=location,
                current_holder=sampled_by,
            )
            seal = Seal(seal_no=seal_no, sample_id=sample.sample_id, sealed_by=sampled_by, sealed_at=sample.sampled_at)
            self.store.samples[sample.sample_id] = sample
            self.store.seals[seal_no] = seal
            event = self._add_custody(
                sample, "seal", sampled_by, note=f"抽样现场封签 {seal_no}"
            )
            self.store.append_event(
                "sample.registered",
                sampled_by,
                {"sample": sample.as_dict(), "seal": seal.as_dict(), "event_id": event.event_id},
            )
            return sample

    def _add_custody(
        self,
        sample: Sample,
        action: str,
        actor_id: str,
        from_holder: Optional[str] = None,
        to_holder: Optional[str] = None,
        note: str = "",
    ) -> CustodyEvent:
        actor = self._actor(actor_id)
        seq = self.store.next_seq(f"custody:{sample.sample_id}")
        event = CustodyEvent(
            event_id=_new_id("ctx"),
            sample_id=sample.sample_id,
            seq=seq,
            action=action,
            actor_id=actor_id,
            org=actor.org,
            at=utcnow(),
            from_holder=from_holder,
            to_holder=to_holder,
            note=note,
        )
        self.store.custody.setdefault(sample.sample_id, []).append(event)
        return event

    def handover_sample(self, sample_id: str, actor_id: str, to_holder: str, note: str = "") -> CustodyEvent:
        """运输交接：必须由当前保管人发起。"""
        self._actor(to_holder)
        with self.store.lock:
            sample = self._require_sample(sample_id)
            if sample.current_holder != actor_id:
                raise EngineError(f"样品当前保管人为 {sample.current_holder}，{actor_id} 无权交接")
            event = self._add_custody(sample, "handover", actor_id, actor_id, to_holder, note)
            sample.current_holder = to_holder
            sample.status = "in_transit"
            self.store.append_event("sample.handover", actor_id, event.as_dict())
            return event

    def receive_sample(self, sample_id: str, receiver_id: str, note: str = "") -> CustodyEvent:
        """实验室签收。"""
        receiver = self._actor(receiver_id)
        with self.store.lock:
            sample = self._require_sample(sample_id)
            if sample.current_holder != receiver_id:
                raise EngineError(f"样品不在 {receiver_id} 名下，无法签收")
            event = self._add_custody(sample, "receive", receiver_id, None, receiver_id, note)
            sample.status = "at_lab"
            self.store.append_event("sample.received", receiver_id, event.as_dict())
            return event

    def open_seal(self, sample_id: str, actor_id: str, reason: str) -> Seal:
        """启封检验：仅当前持有实验室可启，封签必须完好。"""
        with self.store.lock:
            sample = self._require_sample(sample_id)
            if sample.current_holder != actor_id:
                raise EngineError("仅当前保管实验室可以启封")
            seal = self._seal_of(sample.sample_id)
            if not seal.intact:
                raise EngineError("封签已破损或被启过，证据链断裂，不得检验")
            seal.intact = False
            seal.opened_by = actor_id
            seal.opened_at = utcnow()
            seal.open_reason = reason
            event = self._add_custody(sample, "open_seal", actor_id, note=reason)
            self.store.append_event("seal.opened", actor_id, {"seal": seal.as_dict(), "event_id": event.event_id})
            return seal

    def _require_sample(self, sample_id: str) -> Sample:
        sample = self.store.samples.get(sample_id)
        if sample is None:
            raise NotFoundError(f"样品不存在：{sample_id}")
        return sample

    def _seal_of(self, sample_id: str) -> Seal:
        return next(s for s in self.store.seals.values() if s.sample_id == sample_id)

    # ============================================================ 结果上传

    def upload_result(
        self,
        upload_id: str,
        sample_id: str,
        lab_id: str,
        item_key: str,
        value: Optional[float],
        unit: str,
        method_version: str,
        detected: bool = True,
    ) -> LabResult:
        """受理一次原始结果。

        同一 upload_id 的网络重试不再受理：返回首次记录并标记 duplicate_of，
        防止重试造成重复数据与重复立案研判。
        """
        self._actor(lab_id, "lab")
        with self.store.lock:
            existing = self.store.results.get(upload_id)
            if existing is not None:
                if not existing.accepted or existing.duplicate_of is None:
                    existing.duplicate_of = existing.duplicate_of or existing.upload_id
                duplicate = LabResult(
                    upload_id=upload_id,
                    sample_id=existing.sample_id,
                    lab_id=existing.lab_id,
                    item_key=existing.item_key,
                    value=existing.value,
                    unit=existing.unit,
                    method_version=existing.method_version,
                    detected=existing.detected,
                    uploaded_at=utcnow(),
                    accepted=False,
                    duplicate_of=existing.upload_id,
                )
                self.store.append_event(
                    "result.duplicate_rejected",
                    lab_id,
                    {"upload_id": upload_id, "sample_id": existing.sample_id},
                )
                return duplicate

            sample = self._require_sample(sample_id)
            if sample.current_holder != lab_id:
                raise EngineError("实验室未持有该样品，不能上传结果")
            try:
                self.store.get_method(method_version)
            except KeyError:
                raise ValidationError(f"检测方法版本未登记：{method_version}")
            result = LabResult(
                upload_id=upload_id,
                sample_id=sample_id,
                lab_id=lab_id,
                item_key=item_key,
                value=value,
                unit=unit,
                method_version=method_version,
                detected=detected,
                uploaded_at=utcnow(),
            )
            self.store.results[upload_id] = result
            self.store.results_by_sample.setdefault(sample_id, []).append(upload_id)
            self.store.append_event("result.uploaded", lab_id, result.as_dict())
            return result

    # ============================================================ 报告签发

    def issue_report(
        self,
        sample_id: str,
        lab_id: str,
        standard_code: str,
        appeal_id: Optional[str] = None,
        issued_at: Optional[str] = None,
        upload_ids: Optional[list[str]] = None,
    ) -> Report:
        """按签发当时生效的标准版本作出判定并固化快照。

        报告一经签发不可变；之后标准换版只影响新报告，不倒改本报告。
        appeal_id 非空时为复检报告：只判定本次复检新上传的结果
        （upload_ids 必传），结论变化会置原报告 superseded 标记。
        """
        self._actor(lab_id, "lab")
        with self.store.lock:
            sample = self._require_sample(sample_id)
            results = [
                r for r in self.store.results_for_sample(sample_id) if r.accepted and r.lab_id == lab_id
            ]
            if upload_ids is not None:
                selected = {r.upload_id: r for r in results}
                missing = [u for u in upload_ids if u not in selected]
                if missing:
                    raise ValidationError(f"指定的原始结果不存在或未受理：{missing}")
                results = [selected[u] for u in upload_ids]
            if appeal_id and not upload_ids:
                raise ValidationError("复检报告必须指定本次复检的原始结果 upload_ids")
            if not results:
                raise EngineError("没有本实验室已受理的原始结果，无法签发报告")
            seal = self._seal_of(sample_id)
            if seal.intact:
                raise EngineError("封签未启封，不得出具报告")

            at = issued_at or utcnow()
            try:
                standard = self.store.effective_standard(standard_code, at)
            except KeyError as exc:
                raise ValidationError(str(exc)) from exc

            conclusions: list[dict[str, Any]] = []
            findings: list[dict[str, Any]] = []
            for result in results:
                rule = standard.limits.get(result.item_key)
                if rule is None:
                    raise ValidationError(f"标准 {standard.versioned_code} 未覆盖项目 {result.item_key}")
                verdict, basis = self._judge(result, rule)
                conclusion = {
                    "item_key": result.item_key,
                    "value": result.value,
                    "unit": result.unit,
                    "verdict": verdict,
                    "basis": basis,
                    "method_version": result.method_version,
                }
                conclusions.append(conclusion)
                if verdict == "unqualified":
                    findings.append({**conclusion, "severity": self._severity(result, rule)})

            report = Report(
                report_id=_new_id("rpt"),
                sample_id=sample_id,
                lab_id=lab_id,
                issued_at=at,
                standard_version=standard.versioned_code,
                standard_snapshot=standard.as_dict(),
                conclusions=conclusions,
                overall_verdict="unqualified" if findings else "qualified",
                findings=findings,
            )
            self.store.reports[report.report_id] = report
            self.store.reports_by_sample.setdefault(sample_id, []).append(report.report_id)
            self.store.append_event("report.issued", lab_id, {"report_id": report.report_id,
                                                              "sample_id": sample_id,
                                                              "standard_version": report.standard_version,
                                                              "overall_verdict": report.overall_verdict,
                                                              "appeal_id": appeal_id})

            if appeal_id:
                self._settle_reinspection(appeal_id, report)

            if report.overall_verdict == "unqualified":
                case = self._open_or_merge_case(sample, report, opened_by=lab_id)
            else:
                case = self.store.live_case_for_batch(sample.batch_no)
                # 复检合格报告同样挂入案件，才能使旧报告失效、风险解除
                if case is not None and report.report_id not in case.report_ids:
                    case.report_ids.append(report.report_id)
            if case is not None:
                self._refresh_case(case.case_id)
            return report

    @staticmethod
    def _judge(result: LabResult, rule: dict[str, Any]) -> tuple[str, str]:
        if not result.detected:
            return "qualified", "not_detected"
        if rule.get("limit") is None:
            return "unqualified", "out_of_scope"
        if result.value is not None and result.value > rule["limit"]:
            return "unqualified", "exceeds_limit"
        return "qualified", "within_limit"

    @staticmethod
    def _severity(result: LabResult, rule: dict[str, Any]) -> str:
        if rule.get("limit") is None:
            return "high"  # 超范围使用添加剂，直接高风险
        ratio = (result.value or 0) / rule["limit"]
        return "high" if ratio > 2 else "medium"

    # ============================================================ 案件合并

    def _open_or_merge_case(self, sample: Sample, report: Report, opened_by: str) -> Case:
        """同批次已有存活案件则并入（跨地区），否则新立案件。"""
        existing = self.store.live_case_for_batch(sample.batch_no)
        if existing is not None:
            if sample.sample_id not in existing.sample_ids:
                existing.sample_ids.append(sample.sample_id)
            if report.report_id not in existing.report_ids:
                existing.report_ids.append(report.report_id)
            existing.regions.add(sample.region)
            self.store.append_event(
                "case.merged",
                opened_by,
                {"case_id": existing.case_id, "batch_no": sample.batch_no,
                 "sample_id": sample.sample_id, "report_id": report.report_id, "region": sample.region},
            )
            return existing

        case = Case(
            case_id=_new_id("case"),
            batch_no=sample.batch_no,
            producer=sample.producer,
            product_name=sample.product_name,
            opened_at=utcnow(),
            opened_by=opened_by,
            regions={sample.region},
            sample_ids=[sample.sample_id],
            report_ids=[report.report_id],
        )
        self.store.cases[case.case_id] = case
        self.store.case_by_batch[sample.batch_no] = case.case_id
        self.store.append_event("case.opened", opened_by, {"case_id": case.case_id, "batch_no": sample.batch_no})
        return case

    def _require_case(self, case_id: str) -> Case:
        case = self.store.cases.get(case_id)
        if case is None or case.merged_into:
            raise NotFoundError(f"案件不存在或已并入其他案件：{case_id}")
        return case

    def merge_cases(self, surviving_case_id: str, other_case_id: str, actor_id: str) -> Case:
        """人工并案入口（例如批次号别名核实后）。"""
        self._actor(actor_id, "regulator")
        with self.store.lock:
            surviving = self._require_case(surviving_case_id)
            other = self._require_case(other_case_id)
            if surviving.batch_no != other.batch_no:
                raise ValidationError("仅同一生产批次可以并案")
            if other.merged_into:
                raise EngineError("该案件已被合并")
            for sample_id in other.sample_ids:
                if sample_id not in surviving.sample_ids:
                    surviving.sample_ids.append(sample_id)
            for report_id in other.report_ids:
                if report_id not in surviving.report_ids:
                    surviving.report_ids.append(report_id)
            for appeal_id in other.appeal_ids:
                if appeal_id not in surviving.appeal_ids:
                    surviving.appeal_ids.append(appeal_id)
            surviving.regions.update(other.regions)
            surviving.consolidated_from.append(other.case_id)
            other.merged_into = surviving.case_id
            self.store.case_by_batch[other.batch_no] = surviving.case_id
            self.store.append_event(
                "case.merged_manual",
                actor_id,
                {"surviving": surviving.case_id, "merged": other.case_id},
            )
            self._refresh_case(surviving.case_id)
            return surviving

    # ============================================================ 复检与补件

    def apply_appeal(self, case_id: str, sample_id: str, merchant_id: str, reason: str) -> Appeal:
        self._actor(merchant_id, "merchant")
        with self.store.lock:
            case = self.store.cases.get(case_id)
            if case is None or case.merged_into:
                raise NotFoundError(f"案件不存在：{case_id}")
            sample = self._require_sample(sample_id)
            if sample.merchant_id != merchant_id:
                raise ForbiddenError("商户只能对自己的样品申请复检")
            if sample_id not in case.sample_ids:
                raise ValidationError("样品不在该案件下")
            appeal = Appeal(
                appeal_id=_new_id("apl"),
                case_id=case_id,
                sample_id=sample_id,
                merchant_id=merchant_id,
                reason=reason,
                status="submitted",
                created_at=utcnow(),
            )
            self.store.appeals[appeal.appeal_id] = appeal
            case.appeal_ids.append(appeal.appeal_id)
            self.store.append_event("appeal.applied", merchant_id, appeal.as_dict())
            self._refresh_case(case_id)
            return appeal

    def request_supplement(self, appeal_id: str, regulator_id: str, supplement_due: str, note: str = "") -> Appeal:
        """监管人员要求补件并给出整改/补件期限。"""
        self._actor(regulator_id, "regulator")
        with self.store.lock:
            appeal = self.store.appeals[appeal_id]
            appeal.status = "supplementing"
            appeal.supplement_due = supplement_due
            appeal.decision_note = note
            self.store.append_event(
                "appeal.supplement_requested",
                regulator_id,
                {"appeal_id": appeal_id, "supplement_due": supplement_due, "note": note},
            )
            self._refresh_case(appeal.case_id)
            return appeal

    def add_appeal_document(self, appeal_id: str, merchant_id: str, doc_name: str) -> dict[str, Any]:
        """商户补件：仅本人案件的复检申请可传。"""
        self._actor(merchant_id, "merchant")
        with self.store.lock:
            appeal = self.store.appeals[appeal_id]
            if appeal.merchant_id != merchant_id:
                raise ForbiddenError("只能查看/补充自己的复检材料")
            if appeal.status not in {"submitted", "supplementing"}:
                raise EngineError(f"复检申请已{appeal.status}，不能再补件")
            doc = {"doc_id": _new_id("doc"), "name": doc_name, "uploaded_by": merchant_id, "at": utcnow()}
            appeal.documents.append(doc)
            self.store.append_event("appeal.document_added", merchant_id,
                                    {"appeal_id": appeal_id, "doc_id": doc["doc_id"]})
            return doc

    def decide_appeal(self, appeal_id: str, regulator_id: str, accept: bool, note: str = "") -> Appeal:
        self._actor(regulator_id, "regulator")
        with self.store.lock:
            appeal = self.store.appeals[appeal_id]
            if appeal.status not in {"submitted", "supplementing"}:
                raise EngineError("复检申请已有结论")
            appeal.decided_at = utcnow()
            appeal.decision_note = note
            if accept:
                appeal.status = "accepted"
                sample = self._require_sample(appeal.sample_id)
                event = self._add_custody(
                    sample, "transfer_to_reinspection", regulator_id,
                    sample.current_holder, regulator_id, "复检受理，样品转送",
                )
                sample.current_holder = regulator_id
                sample.status = "under_reinspection"
                self.store.append_event("appeal.accepted", regulator_id,
                                        {"appeal_id": appeal_id, "event_id": event.event_id})
            else:
                appeal.status = "rejected"
                self.store.append_event("appeal.rejected", regulator_id, {"appeal_id": appeal_id, "note": note})
            self._refresh_case(appeal.case_id)
            return appeal

    def deliver_reinspection_sample(self, appeal_id: str, regulator_id: str, lab_id: str) -> CustodyEvent:
        """监管把复检样品送交复检实验室。"""
        self._actor(regulator_id, "regulator")
        self._actor(lab_id, "lab")
        with self.store.lock:
            appeal = self.store.appeals[appeal_id]
            if appeal.status != "accepted":
                raise EngineError("复检未受理，不能送检")
            sample = self._require_sample(appeal.sample_id)
            event = self._add_custody(
                sample, "handover", regulator_id, regulator_id, lab_id, "复检样品交付实验室"
            )
            sample.current_holder = lab_id
            sample.status = "at_lab"
            self.store.append_event("appeal.sample_delivered", regulator_id,
                                    {"appeal_id": appeal_id, "event_id": event.event_id})
            return event

    def _settle_reinspection(self, appeal_id: str, report: Report) -> None:
        appeal = self.store.appeals[appeal_id]
        sample = self._require_sample(report.sample_id)
        if appeal.sample_id != report.sample_id:
            raise ValidationError("复检报告样品与复检申请不一致")
        appeal.reinspection_report_id = report.report_id
        appeal.status = "concluded"
        appeal.decided_at = report.issued_at
        prior = [r for r in self.store.reports_for_sample(report.sample_id) if r.report_id != report.report_id]
        for old in prior:
            if old.overall_verdict != report.overall_verdict:
                # 只追加"被复检推翻"的标记，报告正文与标准快照保持原样
                old.superseded = True
                old.reinspection_report_id = report.report_id
        sample.status = "retained"
        self.store.append_event(
            "appeal.concluded", report.lab_id,
            {"appeal_id": appeal_id, "report_id": report.report_id, "verdict": report.overall_verdict},
        )

    # ============================================================ 证据门禁与处置

    def _active_reports(self, case: Case) -> list[Report]:
        """研判只采用未被复检推翻的报告。"""
        return [self.store.reports[r] for r in case.report_ids if not self.store.reports[r].superseded]

    def _refresh_case(self, case_id: str) -> Case:
        """重算证据清单与案件状态；并案后风险随之合并升级。"""
        case = self.store.cases[case_id]
        checklist: dict[str, bool] = {}
        for sample_id in case.sample_ids:
            sample = self.store.samples[sample_id]
            chain = self.store.custody_chain(sample_id)
            reports = self.store.reports_for_sample(sample_id)
            actions = {e.action for e in chain}
            checklist[sample_id] = {
                "plan": sample.plan_id in self.store.plans,
                "seal_record": True,
                "custody_complete": {"handover", "receive"} <= actions,
                "seal_opened": "open_seal" in actions,
                "raw_result": any(r.accepted for r in self.store.results_for_sample(sample_id)),
                "report": bool(reports),
            }
        for appeal_id in case.appeal_ids:
            appeal = self.store.appeals[appeal_id]
            checklist[f"appeal:{appeal_id}"] = {
                "reinspection_settled": appeal.status in {"rejected", "concluded"},
                "status": appeal.status,
            }
        case.evidence_checklist = checklist
        sample_sections = [v for k, v in checklist.items() if not k.startswith("appeal:")]
        appeal_sections = [v for k, v in checklist.items() if k.startswith("appeal:")]
        samples_ok = all(all(section.values()) for section in sample_sections)
        appeals_settled = all(section["reinspection_settled"] for section in appeal_sections)
        case.evidence_complete = samples_ok and appeals_settled
        if case.evidence_complete and case.status == "investigating":
            case.status = "pending_decision"
        return case

    def recommended_grade(self, case_id: str) -> dict[str, Any]:
        """风险规则：超范围添加剂 -> 全域召回；过氧化值超标 2 倍内 -> 批次下架，
        超 2 倍 -> 召回；多地区命中自动上调范围。复检合格报告使风险解除。"""
        with self.store.lock:
            case = self._require_case(case_id)
            active = self._active_reports(case)
            findings = [f for r in active for f in r.findings]
            if not findings:
                return {"level": None, "scope": None, "reason": "现行有效报告均为合格，可恢复销售"}
            levels = {("recall" if f["severity"] == "high" else "delist") for f in findings}
            level = "recall" if "recall" in levels else "delist"
            if any(f["basis"] == "out_of_scope" for f in findings):
                scope = "all_market"
            else:
                scope = "regional" if len(case.regions) > 1 else "batch"
            return {"level": level, "scope": scope, "reason": self._grade_reason(findings, case)}

    @staticmethod
    def _grade_reason(findings: list[dict[str, Any]], case: Case) -> str:
        parts = []
        for f in findings:
            if f["basis"] == "out_of_scope":
                parts.append(f"{f['item_key']} 超范围使用")
            else:
                parts.append(f"{f['item_key']} 超标({f['basis']})")
        where = "、".join(sorted(case.regions))
        return f"{'；'.join(parts)}；涉及地区：{where}"

    def issue_decision(
        self, case_id: str, regulator_id: str, level: Optional[str] = None, scope: Optional[str] = None,
        reason: str = "",
    ) -> Decision:
        """证据齐全后发布分级决定。不指定级别时按风险规则推荐；
        指定级别不得低于风险规则要求（避免该召回只下架）。"""
        self._actor(regulator_id, "regulator")
        with self.store.lock:
            case = self._require_case(case_id)
            self._refresh_case(case_id)
            if not case.evidence_complete:
                raise EngineError("证据尚未齐全，不能发布执法决定")
            grade = self.recommended_grade(case_id)
            if level == "restore" or (level is None and grade["level"] is None):
                return self._restore(case, regulator_id, reason or grade["reason"])
            level = level or grade["level"]
            scope = scope or grade["scope"]
            if LEVEL_RANK[level] < LEVEL_RANK[grade["level"]]:
                raise EngineError(f"风险规则要求至少 {grade['level']}，不能仅作 {level}")
            if SCOPE_RANK[scope] < SCOPE_RANK[grade["scope"]]:
                raise EngineError(f"风险规则要求范围至少 {grade['scope']}，不能缩为 {scope}")
            decision = Decision(
                decision_id=_new_id("dec"),
                case_id=case_id,
                level=level,
                scope=scope,
                reason=reason or grade["reason"],
                decided_by=regulator_id,
                at=utcnow(),
                basis_report_ids=[r.report_id for r in self._active_reports(case)],
            )
            self.store.decisions[decision.decision_id] = decision
            case.decision_ids.append(decision.decision_id)
            case.status = "enforcing"
            self.store.append_event("decision.issued", regulator_id, decision.as_dict())
            return decision

    def _restore(self, case: Case, regulator_id: str, reason: str) -> Decision:
        decision = Decision(
            decision_id=_new_id("dec"),
            case_id=case.case_id,
            level="restore",
            scope="batch",
            reason=reason,
            decided_by=regulator_id,
            at=utcnow(),
        )
        for old_id in case.decision_ids:
            old = self.store.decisions[old_id]
            if old.level in {"delist", "recall"} and old.effective:
                old.effective = False
        self.store.decisions[decision.decision_id] = decision
        case.decision_ids.append(decision.decision_id)
        case.status = "closed"
        for sample_id in case.sample_ids:
            sample = self.store.samples[sample_id]
            event = self._add_custody(sample, "return", regulator_id, sample.current_holder,
                                      sample.merchant_id, "复检合格，恢复销售")
            sample.current_holder = sample.merchant_id
            sample.status = "returned"
        self.store.append_event("decision.restore", regulator_id, decision.as_dict())
        return decision

    # ============================================================ 视图

    def merchant_portal(self, merchant_id: str) -> dict[str, Any]:
        """商户视图：仅自己的补件材料、补件/整改期限与对己决定，看不到他人案件。"""
        self._actor(merchant_id, "merchant")
        with self.store.lock:
            appeals = [a.as_dict() for a in self.store.appeals.values() if a.merchant_id == merchant_id]
            my_cases = {a.case_id for a in self.store.appeals.values() if a.merchant_id == merchant_id}
            my_samples = {s.sample_id for s in self.store.samples.values() if s.merchant_id == merchant_id}
            for case in self.store.cases.values():
                if set(case.sample_ids) & my_samples:
                    my_cases.add(case.case_id)
            decisions = [
                self.store.decisions[d].as_dict()
                for cid in my_cases for d in self.store.cases[cid].decision_ids
            ]
            return {"merchant_id": merchant_id, "appeals": appeals, "decisions": decisions}

    def public_query(self, batch_no: str) -> dict[str, Any]:
        """公众查询：只呈现脱敏结论与日期，不含商户身份、经手人与证据细节。"""
        with self.store.lock:
            case = self.store.live_case_for_batch(batch_no)
            if case is None:
                return {"found": False}
            active = self._active_reports(case)
            if not active:
                return {"found": False}
            latest = max(active, key=lambda r: r.issued_at)
            effective = [self.store.decisions[d] for d in case.decision_ids
                         if self.store.decisions[d].effective]
            latest_decision = effective[-1] if effective else None
            return {
                "found": True,
                "batch_no": _mask(case.batch_no),
                "product_name": case.product_name,
                "producer": _mask(case.producer, head=2),
                "conclusion": latest.overall_verdict,
                "decision_level": latest_decision.level if latest_decision else None,
                "report_date": latest.issued_at[:10],
                "decision_date": latest_decision.at[:10] if latest_decision else None,
            }

    def trace_report(self, report_id: str) -> dict[str, Any]:
        """内部追溯：任一报告 -> 样品当前去向、每次经手人、最终执法动作 + 链哈希。"""
        with self.store.lock:
            report = self.store.reports.get(report_id)
            if report is None:
                raise NotFoundError(f"报告不存在：{report_id}")
            sample = self._require_sample(report.sample_id)
            case = self.store.live_case_for_batch(sample.batch_no)
            decisions = [self.store.decisions[d].as_dict() for d in case.decision_ids] if case else []
            view = {
                "report_id": report_id,
                "report_verdict": report.overall_verdict,
                "standard_version": report.standard_version,
                "superseded": report.superseded,
                "sample": sample.as_dict(),
                "seal": self._seal_of(sample.sample_id).as_dict(),
                "custody": [e.as_dict() for e in self.store.custody_chain(sample.sample_id)],
                "case": case.as_dict() if case else None,
                "decisions": decisions,
                "current_holder": sample.current_holder,
                "chain_head": self.store.chain_head(),
            }
            return view
