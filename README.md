# 糕点抽检实验室协作

本项目服务于糕点抽检与监管处置。抽样、封签、实验室结果、复检和分级执法组成样品证据链。
系统把抽样计划、封签编号、检测方法版本、原始结果和复检材料接成一条可核验记录（哈希链事件日志），
同一批糕点在多个地区被抽到时按风险规则合并研判，而不是重复立案。

## 运行

```bash
python3 service.py --check              # 基础配置检查
python3 service.py --port 8000          # 启动服务，/health 确认服务身份
python3 service.py --seed --port 8000   # 启动并载入中秋糕点抽检演示数据
npm test                                # 运行全部测试（契约 + 业务流程）
```

数据保存在进程内存中，便于本地联调；重启即重置。

## 角色与令牌

通过请求头 `X-Auth-Token` 鉴权，预置账号：

| 令牌 | 角色 | 说明 |
| --- | --- | --- |
| `token-regulator` | 监管人员 | 计划、登记、流转、研判、发布决定、审计追溯 |
| `token-lab-a` / `token-lab-b` | 实验室 | 上传检测结果，查看与己相关的样品与报告 |
| `token-merchant-1` / `token-merchant-2` | 商户 | 申请复检、补件，仅查看自己的补件要求与整改期限 |
| 无令牌 | 公众 | 仅 `/api/public/batches/{批次}` 脱敏查询 |

## 接口一览

| 方法与路径 | 角色 | 说明 |
| --- | --- | --- |
| `GET /health` | 公开 | 健康检查 |
| `POST /api/standards` | 监管 | 发布检测标准版本（更新只新增版本，旧报告钉在原版本上，不倒改） |
| `GET /api/standards` | 监管/实验室 | 标准版本列表 |
| `POST /api/plans` · `GET /api/plans` | 监管 | 抽样计划 |
| `POST /api/samples` | 监管 | 样品登记（封签编号全局唯一，生成首条流转记录） |
| `GET /api/samples` · `GET /api/samples/{id}` | 监管/实验室 | 实验室只见与己相关的样品 |
| `POST /api/samples/{id}/custody` | 监管/当前持有方 | 运输交接、签收、封存、退还、处置，记录每次经手人 |
| `POST /api/results` | 实验室 | 上传检测结果（按标准限值判定结论；同一上传键重试只受理一次） |
| `GET /api/reports/{id}` | 监管/出具实验室 | 报告详情（含标准版本快照） |
| `GET /api/reports/{id}/trace` | 监管 | 从报告追到样品当前去向、每次经手人、最终执法动作 |
| `POST /api/reports/{id}/retest-applications` | 商户（仅自家样品） | 申请复检 |
| `POST /api/retest-applications/{id}/review` | 监管 | 受理（指定实验室）/ 要求补件（附期限）/ 驳回 |
| `POST /api/retest-applications/{id}/materials` | 商户（仅自家） | 提交补件材料 |
| `GET /api/cases` · `GET /api/cases/{id}` | 监管 | 研判案件，按风险等级排序，附建议处置方式 |
| `POST /api/cases/{id}/decisions` | 监管 | 证据齐全后发布分级决定：下架 / 召回 / 恢复销售 |
| `GET /api/merchant/matters` | 商户 | 仅自己的补件要求与整改期限 |
| `GET /api/audit/events` · `GET /api/audit/verify` | 监管 | 哈希链事件日志与完整性校验 |
| `GET /api/public/batches/{批次}` | 公开 | 脱敏结论与日期（不含商户、实验室、封签、原始数值） |

## 核心业务规则

- **合并研判**：同一生产批次的样品跨地区只立一案；批次内新登记样品自动并入，已决案件出现新证据自动重研。
- **风险分级**：不合格覆盖 ≥2 个地区、或复检维持不合格、或 ≥2 份样品不合格 → `high`（建议召回）；单一不合格 → `medium`（建议下架）；最终结论全部合格 → `cleared`（建议恢复销售）。
- **证据齐全才可决定**：批次内全部样品均有检测报告，且无未办结的复检申请；召回要求 `high`，恢复销售要求 `cleared`。
- **重试只受理一次**：结果上传以上传键幂等——首次受理、第二次视为重试返回原报告、第三次起拒绝；同一上传键内容不一致直接冲突。
- **标准不倒改**：报告出具时钉死标准版本快照；标准更新后，旧版本不得用于新报告，历史报告保持不变。
- **留痕可核验**：每个写操作追加一条哈希链事件，`/api/audit/verify` 可校验整条证据链未被篡改。

## 代码结构

```
service.py            服务入口（--check / --port / --seed）
foodtesting/
  models.py           领域对象：计划、样品、封签、报告、复检、案件、决定、事件
  store.py            内存仓储 + 哈希链事件日志
  rules.py            风险分级、证据齐全、决定级别、结果判定规则
  api.py              路由、鉴权、序列化、业务编排
  auth.py             角色与令牌
  seed.py             中秋糕点抽检演示数据
service_contract.py   健康检查契约测试
test_food_testing.py  全流程业务测试
```
