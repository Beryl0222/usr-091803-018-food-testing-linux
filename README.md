# 糕点抽检实验室协作

服务于中秋等节前糕点抽检的跨实验室协作后端。把**抽样计划、封签编号、检测方法版本、原始结果、复检材料**接成一条带哈希链的可核验记录；同一生产批次在多个地区被抽到时**自动并案研判**而不重复立案；证据齐全后监管人员发布**分级下架 / 召回 / 恢复销售**决定。

## 核心规则

| 规则 | 落地方式 |
| --- | --- |
| 结果上传重试只受理一次 | 原始结果以 `upload_id` 为幂等键，重复上传返回 `accepted=false` + `duplicate_of`，不产生重复数据 |
| 标准更新不倒改旧报告 | 报告签发时固化 `standard_snapshot`；换版只影响新报告。复检推翻仅给旧报告加 `superseded` 标记 |
| 同批次跨地区不重复立案 | 案件按生产批次（`batch_no`）唯一；异地不合格报告自动并入，地区集合与风险合并升级 |
| 证据齐全才能处置 | 计划/封签/交接/签收/启封/原始结果/报告齐备且复检全部了结，才允许发布决定；并禁止低于风险规则的降级处置 |
| 风险分级 | 添加剂**超范围使用** → 全域召回；过氧化值超标 ≤2 倍 → 下架，>2 倍 → 召回；多地区命中自动扩大范围；复检合格 → 恢复销售 |
| 商户隔离 | 商户只能查看/补充自己的复检材料与补件（整改）期限，看不到其他商户与案件 |
| 公众脱敏 | 公众查询只返回脱敏批次号/厂名、结论、报告与决定日期，不含任何身份与证据细节 |
| 全程可追溯 | 任一报告可追到样品当前去向、每次经手人（封签→交接→签收→启封→复检→退还）和最终执法动作 |
| 防篡改 | 所有动作写入只追加的哈希链事件日志，`GET /api/journal/verify` 可巡检 |

## 运行

```bash
python3 service.py --check       # 基础配置 + 种子数据事件链自检
python3 service.py --port 8000   # 启动服务（默认播种演示角色/标准/方法）
curl http://127.0.0.1:8000/health
npm test                         # 契约测试 + 端到端协作流程测试
```

## 模块

```
foodtesting/
  models.py     领域对象：计划、样品、封签、流转事件、标准/方法版本、结果、报告、复检、案件、决定
  store.py      线程安全内存存储 + 哈希链事件日志（深拷贝快照，杜绝状态回写污染历史）
  engine.py     业务规则：证据链、幂等、版本快照、并案、证据门禁、分级处置、视图与追溯
  bootstrap.py  种子数据（GB 2716 / GB 2760、GB 5009.227 多版本方法、两地监管/抽样/实验室/商户）
  api.py        JSON HTTP 接口（X-Actor-Id 标识调用方）
service.py      运行入口，保持 /health 稳定身份契约
```

## 接口（节选）

- `POST /api/plans`、`POST /api/samples`（抽样即封签）
- `POST /api/samples/{id}/handover | receive | open-seal`
- `POST /api/results`（幂等）、`POST /api/reports`（复检传 `appeal_id` + `upload_ids`）
- `POST /api/appeals`、`.../supplement-request`、`.../documents`、`.../decision`、`.../deliver-reinspection`
- `GET  /api/cases`、`GET /api/cases/{id}/grade`（风险研判建议）
- `POST /api/cases/{id}/decisions`（证据门禁后的分级决定）、`POST /api/cases/{id}/merge`
- `GET  /api/portal/{merchant_id}`（商户视图）、`GET /api/public/query?batch_no=`（公众脱敏）
- `GET  /api/reports/{id}/trace`（内部追溯）、`GET /api/journal/verify`（链校验）
- `POST /api/standards`、`POST /api/methods`（标准/方法版本管理）

## 场景走查

端到端测试 `test_collaboration.py` 完整演练：杭州实验室检出批次 `B2026-M001` 过氧化值 0.40（限 0.25），宁波实验室在两地不同门店抽到**同一批次**月饼并检出超范围使用山梨酸 → 系统只生成一个跨杭州/宁波的案件，风险研判为**全域召回**；杭州商户复检申请被驳回、补件隔离生效后，监管发布召回；公众仅见脱敏不合格结论，内部可从杭州报告一路追到召回决定。第二个用例演练复检合格后旧报告留痕、下架失效、样品退还、恢复销售；第三个用例验证标准换版（限量 0.25→0.15）不倒改旧报告。
