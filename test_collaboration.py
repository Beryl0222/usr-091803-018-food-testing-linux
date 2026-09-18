"""端到端协作流程测试（HTTP 层 + 引擎层）。

场景：2026 年中秋前，杭州与宁波实验室分别发现同一批次月饼
过氧化值超标与添加剂超范围使用——系统应自动并案、分级处置，
并在复检合格后恢复销售，全程证据链可核验、对外仅披露脱敏结论。
"""

from __future__ import annotations

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from foodtesting import CollabEngine, Store
from foodtesting.bootstrap import bootstrap
from service import Handler

BATCH_MAIN = "B2026-M001"
BATCH_SOLO = "B2026-M002"


class HttpFixture:
    def __init__(self, handler_cls):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def call(self, method: str, path: str, body=None, actor=None):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if actor:
            headers["X-Actor-Id"] = actor
        request = Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, json.load(response)
        except HTTPError as exc:
            return exc.code, json.load(exc)


class CollaborationFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.http = HttpFixture(Handler)
        cls.api = cls.http.call

    @classmethod
    def tearDownClass(cls):
        cls.http.stop()

    # --------------------------------------------------------------- 流程搭建

    def _plan(self, name, region, actor):
        status, body = self.api("POST", "/api/plans", {
            "name": name, "region": region, "food_category": "糕点",
            "created_by": actor, "items": [],
        })
        self.assertEqual(status, 201, body)
        return body["plan_id"]

    def _sample_chain(self, plan_id, region, merchant, sampler, lab, seal_no, location):
        """抽样 -> 封签 -> 交接 -> 签收 -> 启封，返回 sample_id。"""
        status, sample = self.api("POST", "/api/samples", {
            "plan_id": plan_id, "region": region, "merchant_id": merchant,
            "product_name": "莲蓉月饼", "producer": "杭州家乐食品厂",
            "batch_no": BATCH_MAIN if seal_no != "SEAL-SOLO-1" else BATCH_SOLO,
            "location": location, "sampled_by": sampler, "seal_no": seal_no,
        })
        self.assertEqual(status, 201, sample)
        sample_id = sample["sample_id"]

        status, body = self.api("POST", f"/api/samples/{sample_id}/handover",
                                {"actor_id": sampler, "to_holder": lab, "note": "冷藏运输"})
        self.assertEqual(status, 200, body)
        status, body = self.api("POST", f"/api/samples/{sample_id}/receive",
                                {"receiver_id": lab})
        self.assertEqual(status, 200, body)
        status, body = self.api("POST", f"/api/samples/{sample_id}/open-seal",
                                {"actor_id": lab, "reason": "上机检测"})
        self.assertEqual(status, 200, body)
        return sample_id

    # --------------------------------------------------------------- 主场景

    def test_cross_region_same_batch_consolidates_and_enforces(self):
        plan_hz = self._plan("杭州中秋糕点专项", "杭州", "reg-hz")
        plan_nb = self._plan("宁波中秋糕点专项", "宁波", "reg-nb")

        # 杭州：过氧化值 0.40 g/100g，限量 0.25（超标但不足 2 倍）
        smp_hz = self._sample_chain(plan_hz, "杭州", "mkt-hz-01", "smp-hz",
                                    "lab-hz", "SEAL-HZ-001", "杭州西湖区某门店")
        status, first = self.api("POST", "/api/results", {
            "upload_id": "U-HZ-POX-1", "sample_id": smp_hz, "lab_id": "lab-hz",
            "item_key": "peroxide_value", "value": 0.40, "unit": "g/100g",
            "method_version": "GB 5009.227@2016",
        })
        self.assertEqual(status, 200, first)
        self.assertTrue(first["accepted"])

        # 网络重试：同一 upload_id 只受理一次
        status, retry = self.api("POST", "/api/results", {
            "upload_id": "U-HZ-POX-1", "sample_id": smp_hz, "lab_id": "lab-hz",
            "item_key": "peroxide_value", "value": 0.40, "unit": "g/100g",
            "method_version": "GB 5009.227@2016",
        })
        self.assertEqual(status, 200, retry)
        self.assertFalse(retry["accepted"])
        self.assertEqual(retry["duplicate_of"], "U-HZ-POX-1")

        status, rpt_hz = self.api("POST", "/api/reports", {
            "sample_id": smp_hz, "lab_id": "lab-hz", "standard_code": "GB 2716",
        })
        self.assertEqual(status, 201, rpt_hz)
        self.assertEqual(rpt_hz["overall_verdict"], "unqualified")
        self.assertEqual(rpt_hz["standard_version"], "GB 2716@2018")

        status, cases = self.api("GET", "/api/cases")
        self.assertEqual(status, 200)
        self.assertEqual(len(cases["cases"]), 1)
        case_id = cases["cases"][0]["case_id"]

        # 宁波：同一批次，糕点中检出山梨酸（超范围使用，高风险）
        smp_nb = self._sample_chain(plan_nb, "宁波", "mkt-nb-01", "smp-nb",
                                    "lab-nb", "SEAL-NB-001", "宁波鄞州区某商超")
        status, add = self.api("POST", "/api/results", {
            "upload_id": "U-NB-ADD-1", "sample_id": smp_nb, "lab_id": "lab-nb",
            "item_key": "additive:sorbic_acid", "value": 0.5, "unit": "g/kg",
            "method_version": "GB 5009.28@2016",
        })
        self.assertEqual(status, 200, add)
        status, rpt_nb = self.api("POST", "/api/reports", {
            "sample_id": smp_nb, "lab_id": "lab-nb", "standard_code": "GB 2760",
        })
        self.assertEqual(status, 201, rpt_nb)
        self.assertEqual(rpt_nb["findings"][0]["basis"], "out_of_scope")

        # 没有重复立案：两份报告并入同一案件、跨两地区
        status, cases = self.api("GET", "/api/cases")
        self.assertEqual(status, 200)
        self.assertEqual(len(cases["cases"]), 1)
        case = cases["cases"][0]
        self.assertEqual(case["case_id"], case_id)
        self.assertEqual(sorted(case["regions"]), ["宁波", "杭州"])
        self.assertEqual(len(case["sample_ids"]), 2)
        self.assertEqual(len(case["report_ids"]), 2)

        # 风险规则：超范围添加剂 -> 全域召回，而不是仅下架
        status, grade = self.api("GET", f"/api/cases/{case_id}/grade")
        self.assertEqual(status, 200, grade)
        self.assertEqual(grade["level"], "recall")
        self.assertEqual(grade["scope"], "all_market")

        # 复检申请未了结前证据不齐全，禁止发布决定
        status, appeal = self.api("POST", "/api/appeals", {
            "case_id": case_id, "sample_id": smp_hz,
            "merchant_id": "mkt-hz-01", "reason": "申请复检过氧化值",
        })
        self.assertEqual(status, 201, appeal)
        appeal_id = appeal["appeal_id"]
        status, blocked = self.api("POST", f"/api/cases/{case_id}/decisions",
                                   {"regulator_id": "reg-hz"})
        self.assertEqual(status, 409, blocked)

        # 监管人员要求补件并给出期限；商户补件；他商户不能代传
        status, supp = self.api("POST", f"/api/appeals/{appeal_id}/supplement-request", {
            "regulator_id": "reg-hz", "supplement_due": "2026-09-25T18:00:00Z",
            "note": "补充进货查验记录",
        })
        self.assertEqual(status, 200, supp)
        status, doc = self.api("POST", f"/api/appeals/{appeal_id}/documents", {
            "merchant_id": "mkt-nb-01", "doc_name": "别家材料.pdf",
        })
        self.assertEqual(status, 403, doc)
        status, doc = self.api("POST", f"/api/appeals/{appeal_id}/documents", {
            "merchant_id": "mkt-hz-01", "doc_name": "进货查验记录.pdf",
        })
        self.assertEqual(status, 201, doc)

        status, rejected = self.api("POST", f"/api/appeals/{appeal_id}/decision", {
            "regulator_id": "reg-admin", "accept": False, "note": "复检理由不充分",
        })
        self.assertEqual(status, 200, rejected)

        # 证据齐全 -> 分级处置发布
        status, decision = self.api("POST", f"/api/cases/{case_id}/decisions",
                                    {"regulator_id": "reg-admin"})
        self.assertEqual(status, 201, decision)
        self.assertEqual(decision["level"], "recall")
        self.assertEqual(decision["scope"], "all_market")
        self.assertEqual(set(decision["basis_report_ids"]), {rpt_hz["report_id"], rpt_nb["report_id"]})

        # 降级处置不允许：该召回不能只下架
        status, weak = self.api("POST", f"/api/cases/{case_id}/decisions",
                                {"regulator_id": "reg-admin", "level": "delist"})
        self.assertEqual(status, 409, weak)

        # 商户视图隔离
        status, portal_hz = self.api("GET", "/api/portal/mkt-hz-01")
        self.assertEqual(status, 200)
        self.assertEqual({a["appeal_id"] for a in portal_hz["appeals"]}, {appeal_id})
        self.assertTrue(all(a["merchant_id"] == "mkt-hz-01" for a in portal_hz["appeals"]))
        status, portal_nb = self.api("GET", "/api/portal/mkt-nb-01")
        self.assertEqual(status, 200)
        self.assertNotIn(appeal_id, [a["appeal_id"] for a in portal_nb["appeals"]])

        # 公众查询：只有脱敏结论与日期
        status, public = self.api("GET", f"/api/public/query?batch_no={BATCH_MAIN}")
        self.assertEqual(status, 200, public)
        self.assertTrue(public["found"])
        self.assertNotEqual(public["batch_no"], BATCH_MAIN)
        self.assertIn("****", public["batch_no"])
        self.assertEqual(public["conclusion"], "unqualified")
        self.assertEqual(public["decision_level"], "recall")
        raw = json.dumps(public, ensure_ascii=False)
        for secret in ("mkt-hz-01", "mkt-nb-01", "lab-hz", "smp-", "SEAL-", "U-HZ", case_id):
            self.assertNotIn(secret, raw)

        # 内部追溯：从杭州报告追到样品去向、全部经手人与最终召回决定
        status, trace = self.api("GET", f"/api/reports/{rpt_hz['report_id']}/trace")
        self.assertEqual(status, 200, trace)
        actions = [e["action"] for e in trace["custody"]]
        self.assertEqual(actions[:4], ["seal", "handover", "receive", "open_seal"])
        self.assertEqual({e["actor_id"] for e in trace["custody"]},
                         {"smp-hz", "lab-hz"})
        self.assertEqual(trace["current_holder"], "lab-hz")
        self.assertEqual(trace["decisions"][-1]["level"], "recall")
        self.assertEqual(trace["seal"]["seal_no"], "SEAL-HZ-001")
        self.assertTrue(trace["chain_head"])

        # 证据链哈希自检
        status, verify = self.api("GET", "/api/journal/verify")
        self.assertEqual(status, 200, verify)
        self.assertTrue(verify["ok"], verify)

    # ----------------------------------------------------- 复检合格 -> 恢复销售

    def test_reinspection_qualified_restores_sale(self):
        plan = self._plan("杭州专项二号", "杭州", "reg-hz")
        smp = self._sample_chain(plan, "杭州", "mkt-hz-01", "smp-hz",
                                 "lab-hz", "SEAL-SOLO-1", "杭州滨江区某门店")
        self.api("POST", "/api/results", {
            "upload_id": "U-SOLO-1", "sample_id": smp, "lab_id": "lab-hz",
            "item_key": "peroxide_value", "value": 0.30, "unit": "g/100g",
            "method_version": "GB 5009.227@2016",
        })
        status, rpt = self.api("POST", "/api/reports", {
            "sample_id": smp, "lab_id": "lab-hz", "standard_code": "GB 2716",
        })
        self.assertEqual(status, 201, rpt)
        status, cases = self.api("GET", "/api/cases")
        solo = next(c for c in cases["cases"] if BATCH_SOLO in (c["batch_no"],))
        case_id = solo["case_id"]

        status, grade = self.api("GET", f"/api/cases/{case_id}/grade")
        self.assertEqual(grade["level"], "delist")
        self.assertEqual(grade["scope"], "batch")
        status, first_decision = self.api("POST", f"/api/cases/{case_id}/decisions",
                                          {"regulator_id": "reg-hz"})
        self.assertEqual(first_decision["level"], "delist")

        # 复检受理 -> 样品转交监管 -> 送回实验室
        status, appeal = self.api("POST", "/api/appeals", {
            "case_id": case_id, "sample_id": smp, "merchant_id": "mkt-hz-01",
            "reason": "对过氧化值结果有异议",
        })
        appeal_id = appeal["appeal_id"]
        self.api("POST", f"/api/appeals/{appeal_id}/decision",
                 {"regulator_id": "reg-hz", "accept": True})
        self.api("POST", f"/api/appeals/{appeal_id}/deliver-reinspection",
                 {"regulator_id": "reg-hz", "lab_id": "lab-hz"})

        # 复检结果合格，出具复检报告
        self.api("POST", "/api/results", {
            "upload_id": "U-SOLO-RE-1", "sample_id": smp, "lab_id": "lab-hz",
            "item_key": "peroxide_value", "value": 0.10, "unit": "g/100g",
            "method_version": "GB 5009.227@2016",
        })
        status, re_rpt = self.api("POST", "/api/reports", {
            "sample_id": smp, "lab_id": "lab-hz", "standard_code": "GB 2716",
            "appeal_id": appeal_id, "upload_ids": ["U-SOLO-RE-1"],
        })
        self.assertEqual(status, 201, re_rpt)
        self.assertEqual(re_rpt["overall_verdict"], "qualified")

        # 原报告只加 superseded 标记，内容未被倒改
        status, old = self.api("GET", f"/api/reports/{rpt['report_id']}")
        self.assertTrue(old["superseded"])
        self.assertEqual(old["standard_version"], "GB 2716@2018")
        self.assertEqual(old["overall_verdict"], "unqualified")

        # 风险解除 -> 恢复销售，旧下架决定失效，样品退还商户
        status, restore = self.api("POST", f"/api/cases/{case_id}/decisions",
                                   {"regulator_id": "reg-hz", "level": "restore"})
        self.assertEqual(status, 201, restore)
        self.assertEqual(restore["level"], "restore")
        status, decisions = self.api("GET", f"/api/cases/{case_id}/decisions")
        self.assertFalse(decisions["decisions"][0]["effective"])
        self.assertTrue(decisions["decisions"][-1]["effective"])
        status, sample_view = self.api("GET", f"/api/samples/{smp}")
        self.assertEqual(sample_view["sample"]["status"], "returned")
        self.assertEqual(sample_view["sample"]["current_holder"], "mkt-hz-01")

        status, public = self.api("GET", f"/api/public/query?batch_no={BATCH_SOLO}")
        self.assertEqual(public["conclusion"], "qualified")
        self.assertEqual(public["decision_level"], "restore")


class ChainOfCustodyRuleTest(unittest.TestCase):
    """证据链防护规则（引擎层）。"""

    @classmethod
    def setUpClass(cls):
        cls.engine = CollabEngine(Store())
        bootstrap(cls.engine)

    def test_double_open_seal_and_wrong_handover_rejected(self):
        plan = self.engine.create_plan("封签测试", "杭州", "糕点", "reg-hz", [])
        sample = self.engine.register_sample(
            plan.plan_id, "杭州", "mkt-hz-01", "五仁月饼", "杭州家乐食品厂",
            "B-SEAL-T1", "杭州门店", "smp-hz", "SEAL-T-001",
        )
        # 非保管人不能交接
        with self.assertRaises(Exception):
            self.engine.handover_sample(sample.sample_id, "smp-nb", "lab-hz")
        self.engine.handover_sample(sample.sample_id, "smp-hz", "lab-hz")
        self.engine.receive_sample(sample.sample_id, "lab-hz")
        seal = self.engine.open_seal(sample.sample_id, "lab-hz", "检验")
        self.assertFalse(seal.intact)
        # 封签只能启一次
        with self.assertRaises(Exception):
            self.engine.open_seal(sample.sample_id, "lab-hz", "再次检验")
        # 未持有样品的实验室不能上传
        with self.assertRaises(Exception):
            self.engine.upload_result("U-X", sample.sample_id, "lab-nb",
                                      "peroxide_value", 0.1, "g/100g", "GB 5009.227@2016")


class StandardVersionSnapshotTest(unittest.TestCase):
    """标准更新不倒改旧报告。"""

    @classmethod
    def setUpClass(cls):
        cls.engine = CollabEngine(Store())
        bootstrap(cls.engine)

    def test_new_standard_version_does_not_mutate_old_report(self):
        plan = self.engine.create_plan("标准版本测试", "杭州", "糕点", "reg-hz", [])
        sample = self.engine.register_sample(
            plan.plan_id, "杭州", "mkt-hz-01", "豆沙月饼", "杭州家乐食品厂",
            "B-STD-T1", "杭州门店", "smp-hz", "SEAL-STD-001",
        )
        self.engine.handover_sample(sample.sample_id, "smp-hz", "lab-hz")
        self.engine.receive_sample(sample.sample_id, "lab-hz")
        self.engine.open_seal(sample.sample_id, "lab-hz", "检验")
        # 旧标准时限量 0.25，0.20 合格
        self.engine.upload_result(
            "U-STD-1", sample.sample_id, "lab-hz", "peroxide_value",
            0.20, "g/100g", "GB 5009.227@2016",
        )
        old_report = self.engine.issue_report(
            sample.sample_id, "lab-hz", "GB 2716", issued_at="2026-01-10T10:00:00Z",
        )
        self.assertEqual(old_report.standard_version, "GB 2716@2018")
        self.assertEqual(old_report.overall_verdict, "qualified")

        # 2026 年中标准换版，限量收紧到 0.15
        self.engine.publish_standard(
            "GB 2716", "2026", "植物油新版限量", "2026-06-01T00:00:00Z",
            {"peroxide_value": {"limit": 0.15, "unit": "g/100g", "basis": "新限量"}},
            "reg-admin",
        )
        # 旧报告结论与快照保持原样
        self.assertEqual(old_report.standard_version, "GB 2716@2018")
        self.assertEqual(old_report.standard_snapshot["limits"]["peroxide_value"]["limit"], 0.25)
        self.assertEqual(old_report.overall_verdict, "qualified")

        # 新样品在新版生效后判定，0.20 在新标准下不合格
        sample2 = self.engine.register_sample(
            plan.plan_id, "杭州", "mkt-hz-01", "豆沙月饼", "杭州家乐食品厂",
            "B-STD-T2", "杭州门店", "smp-hz", "SEAL-STD-002",
        )
        self.engine.handover_sample(sample2.sample_id, "smp-hz", "lab-hz")
        self.engine.receive_sample(sample2.sample_id, "lab-hz")
        self.engine.open_seal(sample2.sample_id, "lab-hz", "检验")
        self.engine.upload_result(
            "U-STD-2", sample2.sample_id, "lab-hz", "peroxide_value",
            0.20, "g/100g", "GB 5009.227@2023",
        )
        new_report = self.engine.issue_report(sample2.sample_id, "lab-hz", "GB 2716")
        self.assertEqual(new_report.standard_version, "GB 2716@2026")
        self.assertEqual(new_report.overall_verdict, "unqualified")
        # 旧报告依然不动
        self.assertEqual(self.engine.store.reports[old_report.report_id].overall_verdict, "qualified")


if __name__ == "__main__":
    unittest.main()
