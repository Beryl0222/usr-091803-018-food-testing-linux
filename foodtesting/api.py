"""HTTP API：把协作引擎暴露为 JSON 接口。

鉴权说明：演示环境用 X-Actor-Id 请求头标识调用方；POST 也可在 body 中
显式给出 actor_id。真实部署应替换为带签名的身份令牌。
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlparse

from .engine import (
    CollabEngine,
    EngineError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
)


def build_handler(engine: CollabEngine):
    routes: list[tuple[str, str, Callable[..., Any]]] = []

    def route(method: str, pattern: str):
        def register(func: Callable[..., Any]):
            routes.append((method, pattern, func))
            return func
        return register

    # ------------------------------------------------------------ 写接口

    @route("POST", "/api/plans")
    def create_plan(body, _query, actor):
        return 201, engine.create_plan(
            name=body["name"], region=body["region"], food_category=body["food_category"],
            created_by=body.get("created_by") or _require_actor(body, actor), items=body.get("items", []),
        ).as_dict()

    @route("POST", "/api/samples")
    def register_sample(body, _query, actor):
        return 201, engine.register_sample(
            plan_id=body["plan_id"], region=body.get("region", ""),
            merchant_id=body["merchant_id"], product_name=body["product_name"],
            producer=body["producer"], batch_no=body["batch_no"], location=body["location"],
            sampled_by=body.get("sampled_by") or _require_actor(body, actor),
            seal_no=body["seal_no"], sampled_at=body.get("sampled_at"),
        ).as_dict()

    @route("POST", "/api/samples/{id}/handover")
    def handover(body, query, actor):
        return 200, engine.handover_sample(
            query["id"], body.get("actor_id") or _require_actor(body, actor),
            body["to_holder"], body.get("note", ""),
        ).as_dict()

    @route("POST", "/api/samples/{id}/receive")
    def receive(body, query, actor):
        return 200, engine.receive_sample(
            query["id"], body.get("receiver_id") or _require_actor(body, actor),
            body.get("note", ""),
        ).as_dict()

    @route("POST", "/api/samples/{id}/open-seal")
    def open_seal(body, query, actor):
        return 200, engine.open_seal(
            query["id"], body.get("actor_id") or _require_actor(body, actor), body["reason"],
        ).as_dict()

    @route("POST", "/api/results")
    def upload_result(body, _query, _actor):
        # 重试受理语义在引擎内保证：同 upload_id 第二次返回 accepted=false，HTTP 仍为 200
        return 200, engine.upload_result(
            upload_id=body["upload_id"], sample_id=body["sample_id"], lab_id=body["lab_id"],
            item_key=body["item_key"], value=body.get("value"), unit=body["unit"],
            method_version=body["method_version"], detected=body.get("detected", True),
        ).as_dict()

    @route("POST", "/api/reports")
    def issue_report(body, _query, _actor):
        return 201, engine.issue_report(
            sample_id=body["sample_id"], lab_id=body["lab_id"],
            standard_code=body["standard_code"], appeal_id=body.get("appeal_id"),
            issued_at=body.get("issued_at"), upload_ids=body.get("upload_ids"),
        ).as_dict()

    @route("POST", "/api/appeals")
    def apply_appeal(body, _query, _actor):
        return 201, engine.apply_appeal(
            case_id=body["case_id"], sample_id=body["sample_id"],
            merchant_id=body["merchant_id"], reason=body["reason"],
        ).as_dict()

    @route("POST", "/api/appeals/{id}/supplement-request")
    def request_supplement(body, query, actor):
        return 200, engine.request_supplement(
            query["id"], body.get("regulator_id") or _require_actor(body, actor),
            body["supplement_due"], body.get("note", ""),
        ).as_dict()

    @route("POST", "/api/appeals/{id}/documents")
    def add_document(body, query, actor):
        return 201, engine.add_appeal_document(
            query["id"], body.get("merchant_id") or _require_actor(body, actor), body["doc_name"],
        )

    @route("POST", "/api/appeals/{id}/decision")
    def decide_appeal(body, query, actor):
        return 200, engine.decide_appeal(
            query["id"], body.get("regulator_id") or _require_actor(body, actor),
            accept=bool(body["accept"]), note=body.get("note", ""),
        ).as_dict()

    @route("POST", "/api/appeals/{id}/deliver-reinspection")
    def deliver_reinspection(body, query, actor):
        return 200, engine.deliver_reinspection_sample(
            query["id"], body.get("regulator_id") or _require_actor(body, actor), body["lab_id"],
        ).as_dict()

    @route("POST", "/api/cases/{id}/merge")
    def merge_case(body, query, actor):
        return 200, engine.merge_cases(
            query["id"], body["other_case_id"],
            body.get("actor_id") or _require_actor(body, actor),
        ).as_dict()

    @route("POST", "/api/cases/{id}/decisions")
    def issue_decision(body, query, actor):
        return 201, engine.issue_decision(
            query["id"], body.get("regulator_id") or _require_actor(body, actor),
            level=body.get("level"), scope=body.get("scope"), reason=body.get("note", ""),
        ).as_dict()

    @route("POST", "/api/standards")
    def publish_standard(body, _query, actor):
        return 201, engine.publish_standard(
            code=body["code"], version=body["version"], title=body["title"],
            effective_at=body["effective_at"], limits=body["limits"],
            actor_id=body.get("actor_id") or _require_actor(body, actor),
        ).as_dict()

    @route("POST", "/api/methods")
    def publish_method(body, _query, actor):
        return 201, engine.publish_method(
            method_code=body["method_code"], version=body["version"], title=body["title"],
            item_key=body["item_key"], effective_at=body["effective_at"],
            actor_id=body.get("actor_id") or _require_actor(body, actor),
        ).as_dict()

    # ------------------------------------------------------------ 读接口

    @route("GET", "/api/plans/{id}")
    def get_plan(_body, query, _actor):
        plan = engine.store.plans.get(query["id"])
        if plan is None:
            raise NotFoundError(f"抽样计划不存在：{query['id']}")
        return 200, plan.as_dict()

    @route("GET", "/api/samples/{id}")
    def get_sample(_body, query, _actor):
        sample = engine.store.samples.get(query["id"])
        if sample is None:
            raise NotFoundError(f"样品不存在：{query['id']}")
        seal = engine._seal_of(sample.sample_id)
        return 200, {
            "sample": sample.as_dict(),
            "seal": seal.as_dict(),
            "custody": [e.as_dict() for e in engine.store.custody_chain(sample.sample_id)],
        }

    @route("GET", "/api/reports/{id}")
    def get_report(_body, query, _actor):
        report = engine.store.reports.get(query["id"])
        if report is None:
            raise NotFoundError(f"报告不存在：{query['id']}")
        return 200, report.as_dict()

    @route("GET", "/api/reports/{id}/trace")
    def trace_report(_body, query, _actor):
        return 200, engine.trace_report(query["id"])

    @route("GET", "/api/cases")
    def list_cases(_body, _query, _actor):
        return 200, {
            "cases": [
                c.as_dict() for c in engine.store.cases.values() if not c.merged_into
            ]
        }

    @route("GET", "/api/cases/{id}")
    def get_case(_body, query, _actor):
        return 200, engine._require_case(query["id"]).as_dict()

    @route("GET", "/api/cases/{id}/grade")
    def get_grade(_body, query, _actor):
        grade = engine.recommended_grade(query["id"])
        grade["case_id"] = query["id"]
        return 200, grade

    @route("GET", "/api/cases/{id}/decisions")
    def list_decisions(_body, query, _actor):
        case = engine._require_case(query["id"])
        return 200, {
            "decisions": [engine.store.decisions[d].as_dict() for d in case.decision_ids]
        }

    @route("GET", "/api/appeals/{id}")
    def get_appeal(_body, query, _actor):
        appeal = engine.store.appeals.get(query["id"])
        if appeal is None:
            raise NotFoundError(f"复检申请不存在：{query['id']}")
        return 200, appeal.as_dict()

    @route("GET", "/api/portal/{merchant_id}")
    def merchant_portal(_body, query, _actor):
        return 200, engine.merchant_portal(query["merchant_id"])

    @route("GET", "/api/public/query")
    def public_query(_body, query, _actor):
        return 200, engine.public_query(query.get("batch_no", ""))

    @route("GET", "/api/journal/verify")
    def verify_chain(_body, _query, _actor):
        return 200, engine.store.verify_chain()

    def _require_actor(body: dict[str, Any], header_actor: Optional[str]) -> str:
        actor_id = body.get("actor_id") or header_actor
        if not actor_id:
            raise ValidationError("缺少操作者身份（X-Actor-Id 头或 actor_id 字段）")
        return actor_id

    def _match(method: str, path: str):
        for route_method, pattern, func in routes:
            if route_method != method:
                continue
            pattern_parts = pattern.strip("/").split("/")
            path_parts = path.strip("/").split("/")
            if len(pattern_parts) != len(path_parts):
                continue
            params: dict[str, str] = {}
            for pp, ap in zip(pattern_parts, path_parts):
                if pp.startswith("{") and pp.endswith("}"):
                    params[pp[1:-1]] = ap
                elif pp != ap:
                    break
            else:
                return func, params
        return None, None

    class Handler(BaseHTTPRequestHandler):
        def _send_json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _handle(self, method: str) -> None:
            parsed = urlparse(self.path)
            func, params = _match(method, parsed.path)
            if func is None:
                self.send_error(404)
                return
            try:
                body: dict[str, Any] = {}
                if method == "POST":
                    length = int(self.headers.get("Content-Length") or 0)
                    raw = self.rfile.read(length) if length else b""
                    if raw:
                        body = json.loads(raw.decode("utf-8"))
                        if not isinstance(body, dict):
                            raise ValidationError("请求体必须是 JSON 对象")
                query = {**params, **{k: v[0] for k, v in parse_qs(parsed.query).items()}}
                status, payload = func(body, query, self.headers.get("X-Actor-Id"))
                self._send_json(status, payload)
            except json.JSONDecodeError:
                self._send_json(400, {"error": "请求体不是合法 JSON"})
            except NotFoundError as exc:
                self._send_json(404, {"error": str(exc)})
            except ForbiddenError as exc:
                self._send_json(403, {"error": str(exc)})
            except ValidationError as exc:
                self._send_json(400, {"error": str(exc)})
            except EngineError as exc:
                self._send_json(409, {"error": str(exc)})

        def do_GET(self):
            if self.path == "/health":
                from .service_identity import health_payload
                self._send_json(200, health_payload())
                return
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def log_message(self, *_args):
            return

    return Handler
