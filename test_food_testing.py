"""糕点抽检实验室协作后端的全流程测试。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from foodtesting.api import App, make_handler

REG = "token-regulator"
LAB_A = "token-lab-a"
LAB_B = "token-lab-b"
MCH1 = "token-merchant-1"
MCH2 = "token-merchant-2"

PEROXIDE = {"code": "GB 5009.227", "title": "食品中过氧化值的测定", "version": "2023",
            "effective_from": "2024-03-01",
            "limits": {"过氧化值": {"limit": 0.25, "unit": "g/100g", "rule": "max"}}}
ADDITIVE = {"code": "GB 2760", "title": "食品添加剂使用标准", "version": "2024",
            "effective_from": "2025-02-08",
            "limits": {"脱氢乙酸": {"limit": 0, "unit": "g/kg", "rule": "forbidden"}}}


class Client:
    def __init__(self, base):
        self.base = base

    def call(self, method, path, token=None, body=None):
        req = Request(self.base + path, method=method)
        if token:
            req.add_header("X-Auth-Token", token)
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            req.add_header("Content-Type", "application/json")
        try:
            with urlopen(req, data=data, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except HTTPError as err:
            payload = json.loads(err.read().decode("utf-8"))
            err.close()
            return err.code, payload


class FoodTestingTest(unittest.TestCase):
    def setUp(self):
        self.app = App()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.api = Client(f"http://127.0.0.1:{self.server.server_port}")
        self.plan_id = self._bootstrap()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    # ---- 环境搭建 ----

    def _bootstrap(self):
        status, _ = self.api.call("POST", "/api/standards", REG, PEROXIDE)
        self.assertEqual(status, 201)
        status, _ = self.api.call("POST", "/api/standards", REG, ADDITIVE)
        self.assertEqual(status, 201)
        status, payload = self.api.call(
            "POST", "/api/plans", REG,
            {"title": "中秋前文旅区域糕点专项抽检", "region": "长三角", "category": "糕点"},
        )
        self.assertEqual(status, 201)
        return payload["plan"]["id"]

    def _register(self, seal, batch, region, merchant="MCH-01"):
        status, payload = self.api.call(
            "POST", "/api/samples", REG,
            {"plan_id": self.plan_id, "seal_no": seal, "product_name": "苏式月饼",
             "batch_no": batch, "merchant_id": merchant, "region": region,
             "production_date": "2026-09-01", "sampled_at": "2026-09-10"},
        )
        self.assertEqual(status, 201, payload)
        return payload["sample"]["id"]

    def _deliver(self, sample_id, lab_token, lab_id):
        status, payload = self.api.call(
            "POST", f"/api/samples/{sample_id}/custody", REG,
            {"action": "ship", "to_holder": lab_id, "to_user_id": lab_id, "handler": "李押运"},
        )
        self.assertEqual(status, 201, payload)
        status, payload = self.api.call(
            "POST", f"/api/samples/{sample_id}/custody", lab_token,
            {"action": "receive", "to_holder": lab_id, "to_user_id": lab_id, "handler": "周签收"},
        )
        self.assertEqual(status, 201, payload)

    def _upload(self, sample_id, key, items, token=LAB_A, code="GB 5009.227", version="2023", **extra):
        body = {"sample_id": sample_id, "upload_key": key, "standard_code": code,
                "standard_version": version, "items": items, "raw_ref": f"RAW-{key}"}
        body.update(extra)
        return self.api.call("POST", "/api/results", token, body)

    def _bad_sample(self, seal="SEAL-001", batch="B-001", region="杭州", merchant="MCH-01"):
        """登记并交付一份样品，上传过氧化值超标的不合格初检报告。"""
        sample_id = self._register(seal, batch, region, merchant)
        self._deliver(sample_id, LAB_A, "LAB-01")
        status, payload = self._upload(sample_id, f"UP-{seal}", [{"name": "过氧化值", "value": 0.31}])
        self.assertEqual(status, 201, payload)
        self.assertEqual(payload["report"]["conclusion"], "不合格")
        return sample_id, payload["report"]["id"]

    # ---- 主流程 ----

    def test_full_flow_delist_trace_and_public(self):
        sample_id, report_id = self._bad_sample()
        status, payload = self.api.call("GET", "/api/cases", REG)
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["cases"]), 1)
        case = payload["cases"][0]
        self.assertEqual(case["risk_level"], "medium")
        self.assertTrue(case["evidence"]["complete"])

        status, payload = self.api.call(
            "POST", f"/api/cases/{case['id']}/decisions", REG,
            {"level": "delist", "scope_note": "景区周边暂停销售",
             "rectification": {"requirement": "更换油脂供应商并复检", "deadline": "2026-09-30"}},
        )
        self.assertEqual(status, 201, payload)
        self.assertEqual(payload["decision"]["level_label"], "下架")

        # 内部追溯：报告 -> 样品去向、经手人、最终执法动作
        status, trace = self.api.call("GET", f"/api/reports/{report_id}/trace", REG)
        self.assertEqual(status, 200)
        self.assertEqual(len(trace["custody"]), 3)  # 登记、运输、签收
        self.assertEqual([c["handler"] for c in trace["custody"]], ["王执法", "李押运", "周签收"])
        self.assertEqual(trace["current_whereabouts"]["holder"], "LAB-01")
        self.assertEqual(trace["final_action"]["level"], "delist")

        # 公众查询：只有脱敏结论与日期
        status, public = self.api.call("GET", "/api/public/batches/B-001")
        self.assertEqual(status, 200)
        self.assertEqual(public["conclusions"][0]["conclusion"], "不合格")
        self.assertEqual(public["decision"]["level"], "delist")
        raw = json.dumps(public, ensure_ascii=False)
        for leaked in ("桂香斋", "MCH-01", "LAB-01", "SEAL-001", "0.31", "seal_no", "merchant"):
            self.assertNotIn(leaked, raw)

        # 商户看到自己的整改期限
        status, matters = self.api.call("GET", "/api/merchant/matters", MCH1)
        self.assertEqual(status, 200)
        self.assertEqual(len(matters["rectifications"]), 1)
        self.assertEqual(matters["rectifications"][0]["deadline"], "2026-09-30")

        status, audit = self.api.call("GET", "/api/audit/verify", REG)
        self.assertEqual(status, 200)
        self.assertTrue(audit["ok"])
        self.assertGreater(audit["events"], 5)

    # ---- 结果上传：重试只受理一次 ----

    def test_upload_retry_accepted_only_once(self):
        sample_id = self._register("SEAL-001", "B-001", "杭州")
        self._deliver(sample_id, LAB_A, "LAB-01")
        status, first = self._upload(sample_id, "KEY-1", [{"name": "过氧化值", "value": 0.2}])
        self.assertEqual(status, 201)
        self.assertEqual(first["attempt"], 1)

        status, retry = self._upload(sample_id, "KEY-1", [{"name": "过氧化值", "value": 0.2}])
        self.assertEqual(status, 200)
        self.assertTrue(retry["deduplicated"])
        self.assertEqual(retry["report"]["id"], first["report"]["id"])

        status, third = self._upload(sample_id, "KEY-1", [{"name": "过氧化值", "value": 0.2}])
        self.assertEqual(status, 409)
        self.assertEqual(third["error"], "retry_exhausted")

        status, fourth = self._upload(sample_id, "KEY-1", [{"name": "过氧化值", "value": 0.2}])
        self.assertEqual(status, 409)

    def test_same_key_with_different_content_conflicts(self):
        sample_id = self._register("SEAL-001", "B-001", "杭州")
        self._deliver(sample_id, LAB_A, "LAB-01")
        status, _ = self._upload(sample_id, "KEY-1", [{"name": "过氧化值", "value": 0.2}])
        self.assertEqual(status, 201)
        status, payload = self._upload(sample_id, "KEY-1", [{"name": "过氧化值", "value": 0.9}])
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "upload_key_conflict")

    def test_duplicate_initial_report_rejected(self):
        sample_id = self._register("SEAL-001", "B-001", "杭州")
        self._deliver(sample_id, LAB_A, "LAB-01")
        status, _ = self._upload(sample_id, "KEY-1", [{"name": "过氧化值", "value": 0.2}])
        self.assertEqual(status, 201)
        status, payload = self._upload(sample_id, "KEY-2", [{"name": "过氧化值", "value": 0.2}])
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "report_exists")

    # ---- 标准更新不倒改旧报告 ----

    def test_standard_update_keeps_old_reports_pinned(self):
        sample_id = self._register("SEAL-001", "B-001", "杭州")
        self._deliver(sample_id, LAB_A, "LAB-01")
        status, payload = self._upload(sample_id, "KEY-1", [{"name": "过氧化值", "value": 0.26}])
        self.assertEqual(status, 201)
        report_id = payload["report"]["id"]

        newer = dict(PEROXIDE, version="2026", effective_from="2026-09-01",
                     limits={"过氧化值": {"limit": 0.2, "unit": "g/100g", "rule": "max"}})
        status, _ = self.api.call("POST", "/api/standards", REG, newer)
        self.assertEqual(status, 201)

        # 旧报告仍钉在 2023 版与旧限值上
        status, payload = self.api.call("GET", f"/api/reports/{report_id}", REG)
        self.assertEqual(status, 200)
        self.assertEqual(payload["report"]["standard"]["version"], "2023")
        self.assertEqual(payload["report"]["standard"]["items"]["过氧化值"]["limit"], 0.25)
        self.assertEqual(payload["report"]["conclusion"], "不合格")

        # 新报告不得再使用旧版本
        sample2 = self._register("SEAL-002", "B-002", "杭州")
        self._deliver(sample2, LAB_A, "LAB-01")
        status, payload = self._upload(sample2, "KEY-2", [{"name": "过氧化值", "value": 0.22}])
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "standard_superseded")

        status, payload = self._upload(sample2, "KEY-2", [{"name": "过氧化值", "value": 0.22}], version="2026")
        self.assertEqual(status, 201)
        self.assertEqual(payload["report"]["conclusion"], "不合格")  # 按新限值 0.20 判定

    # ---- 跨地区合并研判 ----

    def test_cross_region_same_batch_merges_into_one_case(self):
        s1 = self._register("SEAL-001", "B-001", "杭州", "MCH-01")
        s2 = self._register("SEAL-002", "B-001", "苏州", "MCH-02")
        self._deliver(s1, LAB_A, "LAB-01")
        self._deliver(s2, LAB_B, "LAB-02")

        status, p1 = self._upload(s1, "KEY-1", [{"name": "过氧化值", "value": 0.31}])
        self.assertEqual(status, 201)
        status, p2 = self._upload(s2, "KEY-2", [{"name": "脱氢乙酸", "value": 0.4}],
                                  token=LAB_B, code="GB 2760", version="2024")
        self.assertEqual(status, 201)

        status, payload = self.api.call("GET", "/api/cases", REG)
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["cases"]), 1, "同一批次跨地区不得重复立案")
        case = payload["cases"][0]
        self.assertEqual(case["risk_level"], "high")
        self.assertEqual(case["regions"], ["杭州", "苏州"])
        self.assertEqual(sorted(case["sample_ids"]), [s1, s2])
        self.assertEqual(len(case["report_ids"]), 2)
        self.assertEqual(case["suggested_action"], "recall")

        status, payload = self.api.call(
            "POST", f"/api/cases/{case['id']}/decisions", REG, {"level": "recall"})
        self.assertEqual(status, 201, payload)
        self.assertEqual(payload["decision"]["level_label"], "召回")

    def test_recall_requires_high_risk(self):
        self._bad_sample()
        status, payload = self.api.call("GET", "/api/cases", REG)
        case = payload["cases"][0]
        self.assertEqual(case["risk_level"], "medium")
        status, payload = self.api.call("POST", f"/api/cases/{case['id']}/decisions", REG, {"level": "recall"})
        self.assertEqual(status, 409)
        status, payload = self.api.call("POST", f"/api/cases/{case['id']}/decisions", REG, {"level": "delist"})
        self.assertEqual(status, 201)

    # ---- 证据齐全才能发布决定 ----

    def test_decision_blocked_until_all_batch_samples_reported(self):
        s1 = self._register("SEAL-001", "B-001", "杭州")
        s2 = self._register("SEAL-002", "B-001", "苏州", "MCH-02")
        self._deliver(s1, LAB_A, "LAB-01")
        self._deliver(s2, LAB_B, "LAB-02")
        status, payload = self._upload(s1, "KEY-1", [{"name": "过氧化值", "value": 0.31}])
        self.assertEqual(status, 201)
        case_id = payload["case"]["id"]

        status, payload = self.api.call("POST", f"/api/cases/{case_id}/decisions", REG, {"level": "delist"})
        self.assertEqual(status, 409)
        self.assertIn("尚无检测报告", payload["message"])

        status, payload = self._upload(s2, "KEY-2", [{"name": "脱氢乙酸", "value": 0}],
                                       token=LAB_B, code="GB 2760", version="2024")
        self.assertEqual(status, 201)
        status, payload = self.api.call("POST", f"/api/cases/{case_id}/decisions", REG, {"level": "delist"})
        self.assertEqual(status, 201, payload)

    # ---- 复检流程 ----

    def test_retest_flow_ends_with_restore(self):
        sample_id, report_id = self._bad_sample()
        status, payload = self.api.call("GET", "/api/cases", REG)
        case_id = payload["cases"][0]["id"]

        # 商户申请复检 -> 监管要求补件 -> 商户补件 -> 受理并指定实验室
        status, payload = self.api.call(
            "POST", f"/api/reports/{report_id}/retest-applications", MCH1,
            {"reason": "对过氧化值结果有异议", "materials": ["营业执照"]},
        )
        self.assertEqual(status, 201, payload)
        app_id = payload["application"]["id"]

        status, payload = self.api.call(
            "POST", f"/api/retest-applications/{app_id}/review", REG,
            {"action": "request_supplement", "supplement_request": "请补充留样照片与进货票据",
             "deadline": "2026-09-25"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["application"]["status"], "supplement_requested")

        status, matters = self.api.call("GET", "/api/merchant/matters", MCH1)
        self.assertEqual(len(matters["supplements"]), 1)
        self.assertEqual(matters["supplements"][0]["deadline"], "2026-09-25")

        status, payload = self.api.call(
            "POST", f"/api/retest-applications/{app_id}/materials", MCH1,
            {"materials": ["留样照片.jpg", "进货票据.pdf"]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["application"]["status"], "submitted")

        status, payload = self.api.call(
            "POST", f"/api/retest-applications/{app_id}/review", REG,
            {"action": "accept", "lab_id": "LAB-02"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["application"]["assigned_lab_id"], "LAB-02")

        # 复检未办结时不得发布决定
        status, payload = self.api.call("POST", f"/api/cases/{case_id}/decisions", REG, {"level": "delist"})
        self.assertEqual(status, 409)
        self.assertIn("复检", payload["message"])

        # 复检合格 -> 案件风险解除 -> 恢复销售
        status, payload = self._upload(
            sample_id, "KEY-RETEST", [{"name": "过氧化值", "value": 0.1}],
            token=LAB_B, kind="retest", retest_application_id=app_id,
        )
        self.assertEqual(status, 201, payload)
        self.assertEqual(payload["case"]["risk_level"], "cleared")

        status, payload = self.api.call("POST", f"/api/cases/{case_id}/decisions", REG, {"level": "restore"})
        self.assertEqual(status, 201, payload)
        self.assertEqual(payload["decision"]["level_label"], "恢复销售")

        status, public = self.api.call("GET", "/api/public/batches/B-001")
        self.assertEqual(public["conclusions"][0]["conclusion"], "合格")
        self.assertEqual(public["conclusions"][0]["kind"], "retest")
        self.assertEqual(public["decision"]["level"], "restore")

    def test_retest_confirmed_raises_to_recall(self):
        sample_id, report_id = self._bad_sample()
        status, payload = self.api.call(
            "POST", f"/api/reports/{report_id}/retest-applications", MCH1, {"reason": "异议"})
        app_id = payload["application"]["id"]
        self.api.call("POST", f"/api/retest-applications/{app_id}/review", REG,
                      {"action": "accept", "lab_id": "LAB-02"})
        status, payload = self._upload(
            sample_id, "KEY-RETEST", [{"name": "过氧化值", "value": 0.4}],
            token=LAB_B, kind="retest", retest_application_id=app_id,
        )
        self.assertEqual(status, 201)
        self.assertEqual(payload["case"]["risk_level"], "high")
        case_id = payload["case"]["id"]
        status, payload = self.api.call("POST", f"/api/cases/{case_id}/decisions", REG, {"level": "recall"})
        self.assertEqual(status, 201, payload)

    def test_retest_upload_requires_assigned_lab(self):
        sample_id, report_id = self._bad_sample()
        status, payload = self.api.call(
            "POST", f"/api/reports/{report_id}/retest-applications", MCH1, {"reason": "异议"})
        app_id = payload["application"]["id"]
        self.api.call("POST", f"/api/retest-applications/{app_id}/review", REG,
                      {"action": "accept", "lab_id": "LAB-02"})
        status, payload = self._upload(
            sample_id, "KEY-RETEST", [{"name": "过氧化值", "value": 0.1}],
            token=LAB_A, kind="retest", retest_application_id=app_id,
        )
        self.assertEqual(status, 403)

    # ---- 商户隔离与权限 ----

    def test_merchant_sees_only_own_matters(self):
        sample_id, report_id = self._bad_sample(merchant="MCH-01")
        status, payload = self.api.call(
            "POST", f"/api/reports/{report_id}/retest-applications", MCH1, {"reason": "异议"})
        app_id = payload["application"]["id"]
        self.api.call("POST", f"/api/retest-applications/{app_id}/review", REG,
                      {"action": "request_supplement", "supplement_request": "补票据", "deadline": "2026-09-25"})
        # 办结复检（维持不合格）后才能发布带整改要求的决定
        self.api.call("POST", f"/api/retest-applications/{app_id}/materials", MCH1,
                      {"materials": ["票据.pdf"]})
        self.api.call("POST", f"/api/retest-applications/{app_id}/review", REG,
                      {"action": "accept", "lab_id": "LAB-02"})
        status, payload = self._upload(
            sample_id, "KEY-RETEST", [{"name": "过氧化值", "value": 0.4}],
            token=LAB_B, kind="retest", retest_application_id=app_id)
        self.assertEqual(status, 201, payload)
        case_id = payload["case"]["id"]
        status, payload = self.api.call("POST", f"/api/cases/{case_id}/decisions", REG,
                                        {"level": "recall",
                                         "rectification": {"requirement": "整改用油", "deadline": "2026-09-30"}})
        self.assertEqual(status, 201, payload)

        status, own = self.api.call("GET", "/api/merchant/matters", MCH1)
        self.assertEqual(status, 200)
        self.assertEqual(len(own["supplements"]), 1)
        self.assertEqual(len(own["rectifications"]), 1)

        status, other = self.api.call("GET", "/api/merchant/matters", MCH2)
        self.assertEqual(status, 200)
        self.assertEqual(other["supplements"], [])
        self.assertEqual(other["rectifications"], [])

        # 商户不得访问监管与样品接口，不得替他人申请复检
        status, _ = self.api.call("GET", "/api/samples", MCH1)
        self.assertEqual(status, 403)
        status, _ = self.api.call("GET", "/api/cases", MCH1)
        self.assertEqual(status, 403)
        status, _ = self.api.call("GET", "/api/audit/events", MCH1)
        self.assertEqual(status, 403)
        status, _ = self.api.call("POST", f"/api/reports/{report_id}/retest-applications", MCH2,
                                  {"reason": "越权"})
        self.assertEqual(status, 404)
        status, _ = self.api.call("GET", "/api/cases")
        self.assertEqual(status, 401)

    def test_lab_and_custody_permissions(self):
        sample_id = self._register("SEAL-001", "B-001", "杭州")
        # 未交付前实验室不得流转、不得上传
        status, _ = self.api.call("POST", f"/api/samples/{sample_id}/custody", LAB_A,
                                  {"action": "ship", "to_holder": "X", "handler": "X"})
        self.assertEqual(status, 403)
        status, payload = self._upload(sample_id, "KEY-1", [{"name": "过氧化值", "value": 0.2}])
        self.assertEqual(status, 403)

        self._deliver(sample_id, LAB_A, "LAB-01")
        # 另一实验室不得查看该样品
        status, _ = self.api.call("GET", f"/api/samples/{sample_id}", LAB_B)
        self.assertEqual(status, 404)
        # 持有样品的实验室可以继续流转
        status, _ = self.api.call("POST", f"/api/samples/{sample_id}/custody", LAB_A,
                                  {"action": "seal", "to_holder": "LAB-01", "handler": "周签收"})
        self.assertEqual(status, 201)

    # ---- 公众脱敏 ----

    def test_public_view_field_shape(self):
        self._bad_sample()
        status, payload = self.api.call("GET", "/api/cases", REG)
        case_id = payload["cases"][0]["id"]
        self.api.call("POST", f"/api/cases/{case_id}/decisions", REG, {"level": "delist"})

        status, public = self.api.call("GET", "/api/public/batches/B-001")
        self.assertEqual(status, 200)
        self.assertEqual(set(public), {"batch_no", "product_name", "conclusions", "decision"})
        self.assertEqual(set(public["conclusions"][0]), {"conclusion", "kind", "kind_label", "reported_at"})
        self.assertEqual(set(public["decision"]), {"level", "level_label", "published_at"})

        status, payload = self.api.call("GET", "/api/public/batches/NOPE")
        self.assertEqual(status, 404)

    # ---- 输入校验 ----

    def test_validation_errors(self):
        status, payload = self.api.call("POST", "/api/samples", REG, {"plan_id": self.plan_id})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "missing_field")

        self._register("SEAL-001", "B-001", "杭州")
        status, payload = self.api.call(
            "POST", "/api/samples", REG,
            {"plan_id": self.plan_id, "seal_no": "SEAL-001", "product_name": "苏式月饼",
             "batch_no": "B-002", "merchant_id": "MCH-01", "region": "杭州",
             "production_date": "2026-09-01", "sampled_at": "2026-09-10"})
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "seal_exists")

        sample_id = self._register("SEAL-002", "B-002", "杭州")
        self._deliver(sample_id, LAB_A, "LAB-01")
        status, payload = self._upload(sample_id, "KEY-1", [{"name": "甜蜜素", "value": 1}])
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "unknown_item")

        status, payload = self.api.call(
            "POST", "/api/samples", REG,
            {"plan_id": self.plan_id, "seal_no": "SEAL-003", "product_name": "苏式月饼",
             "batch_no": "B-003", "merchant_id": "MCH-01", "region": "杭州",
             "production_date": "2026-13-01", "sampled_at": "2026-09-10"})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "bad_date")

    def test_audit_events_form_verifiable_chain(self):
        self._bad_sample()
        status, payload = self.api.call("GET", "/api/audit/events", REG)
        self.assertEqual(status, 200)
        events = payload["events"]
        self.assertGreater(len(events), 0)
        self.assertEqual(events[0]["prev_hash"], "0" * 64)
        for prev, cur in zip(events, events[1:]):
            self.assertEqual(cur["prev_hash"], prev["hash"])
        types = {e["type"] for e in events}
        self.assertIn("sample.registered", types)
        self.assertIn("report.uploaded", types)
        self.assertIn("case.created", types)


if __name__ == "__main__":
    unittest.main()
