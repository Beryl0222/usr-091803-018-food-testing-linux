"""演示/联调引导数据。

播种中秋糕点抽检场景所需的标准、检测方法版本与各角色账号，
返回便于 API 测试与文档引用的 id 映射。
"""

from __future__ import annotations

from .engine import CollabEngine

# 食品安全国家标准：过氧化值限量与食品添加剂使用范围
GB2762_2021 = {
    "code": "GB 2716",
    "version": "2018",
    "title": "食品安全国家标准 植物油（过氧化值限量参照）",
    "effective_at": "2018-12-21T00:00:00Z",
    "limits": {
        "peroxide_value": {"limit": 0.25, "unit": "g/100g", "basis": "糕点判定参考限"},
    },
}

GB2760_2014 = {
    "code": "GB 2760",
    "version": "2014",
    "title": "食品安全国家标准 食品添加剂使用标准",
    "effective_at": "2015-05-24T00:00:00Z",
    "limits": {
        # limit=None 表示该添加剂不得用于糕点品类，检出即超范围使用
        "additive:sorbic_acid": {"limit": None, "unit": "g/kg", "basis": "糕点品类超范围使用判定"},
        "additive:dehydroacetic_acid": {"limit": None, "unit": "g/kg", "basis": "糕点品类超范围使用判定"},
    },
}

METHODS = [
    {
        "method_code": "GB 5009.227",
        "version": "2016",
        "title": "食品中过氧化值的测定",
        "item_key": "peroxide_value",
        "effective_at": "2017-03-01T00:00:00Z",
    },
    {
        "method_code": "GB 5009.227",
        "version": "2023",
        "title": "食品中过氧化值的测定（新版）",
        "item_key": "peroxide_value",
        "effective_at": "2025-01-01T00:00:00Z",
    },
    {
        "method_code": "GB 5009.28",
        "version": "2016",
        "title": "食品中苯甲酸、山梨酸和糖精钠的测定",
        "item_key": "additive:sorbic_acid",
        "effective_at": "2017-06-23T00:00:00Z",
    },
]


def bootstrap(engine: CollabEngine) -> dict[str, dict[str, str]]:
    """播种基础数据，返回所有种子 id 的映射。"""
    ids: dict[str, dict[str, str]] = {"actors": {}, "standards": {}, "methods": {}}

    # 监管人员（两地）
    reg_hz = engine.register_actor("reg-hz", "王监管（杭州）", "regulator", "杭州市市场监管局", "杭州")
    reg_nb = engine.register_actor("reg-nb", "李监管（宁波）", "regulator", "宁波市市场监管局", "宁波")
    reg_root = engine.register_actor("reg-admin", "总值班监管员", "regulator", "浙江省市场监管局", "杭州")
    ids["actors"]["reg_hz"] = reg_hz.actor_id
    ids["actors"]["reg_nb"] = reg_nb.actor_id
    ids["actors"]["reg_admin"] = reg_root.actor_id

    # 抽样人员
    smp_hz = engine.register_actor("smp-hz", "赵抽样（杭州）", "sampler", "杭州市食药检研院抽样科", "杭州")
    smp_nb = engine.register_actor("smp-nb", "钱抽样（宁波）", "sampler", "宁波市食药检研院抽样科", "宁波")
    ids["actors"]["smp_hz"] = smp_hz.actor_id
    ids["actors"]["smp_nb"] = smp_nb.actor_id

    # 实验室（两地）
    lab_hz = engine.register_actor("lab-hz", "杭州实验室", "lab", "杭州市食品药品检验研究院", "杭州")
    lab_nb = engine.register_actor("lab-nb", "宁波实验室", "lab", "宁波市食品药品检测院", "宁波")
    ids["actors"]["lab_hz"] = lab_hz.actor_id
    ids["actors"]["lab_nb"] = lab_nb.actor_id

    # 商户（中秋月饼销售方）
    m1 = engine.register_actor("mkt-hz-01", "杭州西湖月饼店", "merchant", "杭州西湖月饼店", "杭州")
    m2 = engine.register_actor("mkt-nb-01", "宁波鄞州糕点铺", "merchant", "宁波鄞州糕点铺", "宁波")
    ids["actors"]["merchant_hz"] = m1.actor_id
    ids["actors"]["merchant_nb"] = m2.actor_id

    # 标准与方法
    s1 = engine.publish_standard(actor_id=reg_root.actor_id, **GB2762_2021)
    s2 = engine.publish_standard(actor_id=reg_root.actor_id, **GB2760_2014)
    ids["standards"]["oil"] = s1.versioned_code
    ids["standards"]["additive"] = s2.versioned_code

    for spec in METHODS:
        m = engine.publish_method(actor_id=reg_root.actor_id, **spec)
        ids["methods"][spec["item_key"] + ":" + spec["version"]] = m.versioned_code

    return ids
