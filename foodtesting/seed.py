"""演示数据：中秋前文旅区域糕点专项抽检。

场景：同一生产批次的苏式月饼在杭州、苏州两地被抽到，
实验室甲检出过氧化值超标，实验室乙检出脱氢乙酸（添加剂超范围使用），
系统按风险规则合并为一个案件并评为高风险，等待监管人员发布决定。
"""

from __future__ import annotations

from .api import (
    create_plan,
    create_standard,
    register_sample,
    transfer_custody,
    upload_result,
)
from .store import Store


def seed_demo(store: Store) -> None:
    reg = store.users_by_token["token-regulator"]
    lab_a = store.users_by_token["token-lab-a"]
    lab_b = store.users_by_token["token-lab-b"]

    create_standard(
        store,
        reg,
        {
            "code": "GB 5009.227",
            "title": "食品安全国家标准 食品中过氧化值的测定",
            "version": "2023",
            "effective_from": "2024-03-01",
            "limits": {"过氧化值": {"limit": 0.25, "unit": "g/100g", "rule": "max"}},
        },
    )
    create_standard(
        store,
        reg,
        {
            "code": "GB 2760",
            "title": "食品安全国家标准 食品添加剂使用标准",
            "version": "2024",
            "effective_from": "2025-02-08",
            "limits": {"脱氢乙酸": {"limit": 0, "unit": "g/kg", "rule": "forbidden"}},
        },
    )
    _, plan_payload = create_plan(
        store,
        reg,
        {
            "title": "中秋前文旅区域糕点专项抽检",
            "region": "长三角文旅示范区",
            "category": "糕点",
            "note": "覆盖景区周边现制现售与预包装糕点",
        },
    )
    plan_id = plan_payload["plan"]["id"]

    _, s1 = register_sample(
        store,
        reg,
        {
            "plan_id": plan_id,
            "seal_no": "FQ-2026-0901",
            "product_name": "苏式鲜肉月饼",
            "batch_no": "2026-ZQ-0901",
            "merchant_id": "MCH-01",
            "region": "杭州西湖文旅区",
            "production_date": "2026-09-01",
            "sampled_at": "2026-09-10",
        },
    )
    _, s2 = register_sample(
        store,
        reg,
        {
            "plan_id": plan_id,
            "seal_no": "FQ-2026-0902",
            "product_name": "苏式鲜肉月饼",
            "batch_no": "2026-ZQ-0901",
            "merchant_id": "MCH-02",
            "region": "苏州山塘文旅区",
            "production_date": "2026-09-01",
            "sampled_at": "2026-09-11",
        },
    )
    id1 = s1["sample"]["id"]
    id2 = s2["sample"]["id"]

    transfer_custody(store, reg, id1, {"action": "ship", "to_holder": "实验室甲", "to_user_id": "LAB-01", "handler": "李押运", "note": "冷链运输"})
    transfer_custody(store, lab_a, id1, {"action": "receive", "to_holder": "实验室甲", "to_user_id": "LAB-01", "handler": "周签收"})
    transfer_custody(store, reg, id2, {"action": "ship", "to_holder": "实验室乙", "to_user_id": "LAB-02", "handler": "李押运"})
    transfer_custody(store, lab_b, id2, {"action": "receive", "to_holder": "实验室乙", "to_user_id": "LAB-02", "handler": "吴签收"})

    upload_result(
        store,
        lab_a,
        {
            "sample_id": id1,
            "upload_key": "LABA-2026-0001",
            "standard_code": "GB 5009.227",
            "standard_version": "2023",
            "items": [{"name": "过氧化值", "value": 0.32}],
            "raw_ref": "RAW-A-0901",
        },
    )
    upload_result(
        store,
        lab_b,
        {
            "sample_id": id2,
            "upload_key": "LABB-2026-0007",
            "standard_code": "GB 2760",
            "standard_version": "2024",
            "items": [{"name": "脱氢乙酸", "value": 0.4}],
            "raw_ref": "RAW-B-0902",
        },
    )
