# 不可变汇率观测（RateObservation）与时点换算 - 产品需求文档

## Overview
- **Summary**: 将现有可变的 `ExchangeRate` 单例式汇率模型重构为不可变的 `RateObservation` 观测模型：每次抓取、手工录入或修订都新增一行，记录提供方、抓取批次、抓取时间、有效时点、币种方向和修订来源；换算按"不晚于目标日期的最近观测"（point-in-time, as-of）选取，支持可配置最大陈旧期、显式缺失状态与可审计回退；报表换算结果携带观测标识；提供数据迁移、审计基线与校验机制。
- **Purpose**: 消除"未来汇率用于过去交易"和"同一历史数据随后续抓取变化"的问题，使任何历史报表数值都可追溯到具体提供方、批次、有效时点和修订，且重看已结算月份/净资产/交易折算时数值保持稳定。
- **Target Users**: 拥有多币种账户（含加密资产、外币、市场工具计价）的最终用户；需要核对历史数据口径的用户本人；协助排查的管理员。

## Goals
- 每次抓取产生不可变观测，任何后续抓取/修订都不修改或删除既有行。
- 默认时点策略只选取不晚于目标日期的最近观测，严格禁止未来值。
- 最大陈旧期可配置（全局默认 7 天），超期与完全无观测均返回显式缺失状态。
- 现有 `ExchangeRateService.singleton` 仅指向"当前观测"，不再覆写历史行的日期与数值。
- 报表（交易行、月度/年度/净资产、API、换算工具）返回或保存所采用的观测标识及来源元数据。
- 直接换算与反向换算共用同一观测（反向由数学倒数派生）。
- 迁移旧数据为观测，定义可审计基线，保证迁移前后"当前余额/当前折算展示金额"一致。
- 任何换算缓存键必须包含观测修订号，修订后旧缓存自动失效。

## Non-Goals
- 不提供历史汇率回填（不调用提供方历史接口按交易日期批量补数据）；早于首个观测的月份按显式缺失处理。
- 不删除 `ExchangeRate` 旧表：迁移后冻结为只读遗留表作为审计/回滚证据，清理时机另行决策。
- 不做按提供方/按服务的陈旧期覆盖配置（v2 候选）；本期仅全局单一策略。
- 不改变账户原币余额计算口径（income − expense），不重算交易本身金额。
- 不引入汇率预测、远期汇率等未来值能力。
- 不做报表页面的整体视觉重设计，仅增加来源信息展示。

## Background & Context
- 现状代码证据：
  - [models.py](file:///Users/kkcarrot/swe-project/WYGIWYH_fork3/app/apps/currencies/models.py#L60-L96)：`ExchangeRate` 为可变模型，`unique_together=(from_currency, to_currency, date)`，存在编辑视图允许直接改日期与数值。
  - [convert.py](file:///Users/kkcarrot/swe-project/WYGIWYH_fork3/app/apps/currencies/utils/convert.py#L11-L41)：选取逻辑按 `ABS(date − 目标日期)` 排序取第一条，不区分过去/未来；正反向在同一查询里用 `CASE` 决定 `rate` 或 `1/rate`。
  - [fetcher.py](file:///Users/kkcarrot/swe-project/WYGIWYH_fork3/app/apps/currencies/exchange_rates/fetcher.py#L200-L257)：`singleton=True` 时取同币对最新自动记录并原地覆写 `rate` 和 `date`；提供方返回三元组不含有效日期，抓取器统一盖 `timezone.now()`。
  - 多数报表换算调用不传日期（[calculations.py](file:///Users/kkcarrot/swe-project/WYGIWYH_fork3/app/apps/transactions/utils/calculations.py#L171-L175)、[month_by_month.py](file:///Users/kkcarrot/swe-project/WYGIWYH_fork3/app/apps/insights/utils/month_by_month.py#L190-L194)），即已结算月份每次都按"当前"汇率折算；仅 [Transaction.exchanged_amount()](file:///Users/kkcarrot/swe-project/WYGIWYH_fork3/app/apps/transactions/models.py#L464-L494) 传交易日期。
  - [net_worth/views.py](file:///Users/kkcarrot/swe-project/WYGIWYH_fork3/app/apps/net_worth/views.py#L117-L134)：合并口径用当前单一汇率乘到全部历史月份，历史月份随后续抓取漂移。
  - 系统 `USE_TZ=True`、`TIME_ZONE` 默认 UTC；提供方时间语义各异：Frankfurter/FXMacroData 为日度定盘（含周末/节假日缺口，FXMacroData 无定盘日 `val` 为 null），CoinGecko/TwelveData 近实时，YFinance 取 1 小时 K 线收盘（带时间戳）。
- 经用户跳过澄清环节，按推荐默认决策固化：全局默认陈旧期 7 天；默认严格缺失并提供可配置回退；旧 `ExchangeRate` 行迁移为观测、更早月份接受缺失；持久化基线快照并提供校验命令。

## Functional Requirements

### 观测模型与生命周期
- **FR-1**: 新增不可变模型 `RateObservation`，字段至少包含：币对方向（from/to）、`rate`、提供方标识、所属服务（可空）、抓取批次（可空）、`fetched_at`（系统抓取时间）、`effective_at`（有效时点，时区敏感，UTC 存储）、`effective_date`（日度定盘的日历日期）、时间类型（实时/日度定盘/传递推导）、来源（自动抓取/手工录入/修订/遗留迁移/传递）、修订链（`revision_of` 自引用、修订序号、修订原因）、推导明细 JSON（传递路径及组成观测 ID 等）。
- **FR-2**: 观测一经创建不可修改、不可删除：模型层禁止已有主键的 `save` 与 `delete`；修正只能以新增修订观测的方式表达（`revision_of` 指向前序观测，修订序号递增）。
- **FR-3**: 新增 `RateFetchBatch`，每次抓取运行（定时/手动）生成批次记录：触发方式、开始/结束时间、状态、收到与落库条数、错误摘要；同批次所有观测共享批次标识。
- **FR-4**: 提供方契约返回有效时点信息：Frankfurter 使用响应中的 `date`；FXMacroData 使用所选最新非空行的 `date`；YFinance 使用最后一根 K 线时间戳；CoinGecko/TwelveData 无有效时间时以抓取时刻为准；提供方声明时间类型与日历口径。
- **FR-5**: 传递型提供方改基于观测构建路径，产出的观测在推导明细中记录路径与各组成观测 ID。

### 时点选取策略
- **FR-6**: 默认选取规则：候选为"不晚于目标时点"的观测——日度定盘按 `effective_date ≤ 目标本地日期`，实时类按 `effective_at ≤ 目标时点`；在候选中取有效时点最近者，同点按修订序号降序、ID 降序确定。
- **FR-7**: 陈旧期：目标时点与观测有效时点之差超过配置的最大陈旧天数（默认 7）的观测不得作为正常结果；默认配置由单例模型 `RatePolicyConfig` 提供（字段：`max_staleness_days`、`allow_closest_fallback`），无配置行时回落到 settings 默认值。
- **FR-8**: 显式结果状态：换算返回结构化结果，状态至少包括 `ok`、`same_currency`、`zero`、`missing_no_observation`（无任何不晚于目标的观测）、`missing_stale`（有观测但全部过旧）；调用方据状态展示"汇率缺失"标记并保留原币金额，不得以 0 静默混入汇总折算列。
- **FR-9**: 可配置回退：`allow_closest_fallback=True` 时，允许选取绝对距离最近（含未来）的观测，但结果必须带 `fallback=True` 标记及来源信息；默认关闭。
- **FR-10**: 聚合报表的目标时点：交易级折算以交易日期为目标；月度汇总/净资产月度快照以该月最后一个日历日为目标；年度行以当年 12 月 31 日为目标；当前视图与实时工具以当前时刻为目标。净资产合并口径必须逐月使用各自目标时点的观测，禁止单一汇率贯穿全部月份。

### 报表溯源与缓存
- **FR-11**: 所有换算结果携带：观测 ID、修订序号、币向（直接/反向）、提供方、批次 ID、抓取时间、有效时点/日期、回退标记。
- **FR-12**: `Transaction.exchanged_amount()` 及其 API 序列化返回观测标识与来源；交易模板展示有效日期/提供方等溯源信息（如悬浮提示）。
- **FR-13**: 月度/年度/净资产/账户汇总中每个折算值可定位到所采用观测；重算已结算期间时数值与观测标识保持不变。
- **FR-14**: 直接与反向换算共用同一观测：反向值按高精度十进制倒数派生，结果记录同一观测 ID 与 `inverted` 方向标记。
- **FR-15**: 换算解析的缓存键必须包含观测修订号（及币对、目标时点）；观测出现新修订后相关缓存键自然改变并触发重算，缓存结果同时保留观测 ID。
- **FR-16**: `ExchangeRateService` 新增"当前观测"指针（`current_observation`）；抓取成功后只更新该指针与 `last_fetch`，`singleton` 标志保留但不再触发任何覆写逻辑。
- **FR-17**: 旧 `ExchangeRate` 迁移后冻结：应用代码不再写入，编辑入口停用，管理后台只读。

### 迁移与基线
- **FR-18**: 提供 Django 数据迁移：先按旧逻辑生成基线快照（每个账户当前原币余额、当前折算值、所用旧汇率 ID/日期/值），再将每条 `ExchangeRate` 行转为一条 `RateObservation`（`effective_at`/`effective_date` 取旧日期；抓取时间不可考则与有效时点相同并标注；自动行来源=自动抓取，非自动行=手工录入；提供方不可考标注为遗留来源），并为服务的"当前观测"指针尽力匹配最新观测。
- **FR-19**: 提供管理命令按新逻辑重算当前折算并与基线比对：原币余额必须完全一致；折算展示金额按目标币种小数位取整后必须一致（倒数运算允许不超过取整单位的误差），不一致时输出差异报告。
- **FR-20**: singleton 历史只有最后状态、无法还原其覆写前历史，该限制在迁移说明与观测来源中显式标注；早于首个观测的期间显示缺失而非使用未来值。

## Non-Functional Requirements
- **NFR-1（可审计性）**: 任一历史折算值可在不依赖应用日志的情况下，仅凭数据库记录还原"提供方、批次、抓取时间、有效时点、币向、修订链/推导组成"。
- **NFR-2（稳定性）**: 已结算期间报表在任意后续抓取后重开，折算值与观测标识逐位一致。
- **NFR-3（正确性）**: 反向换算与正向换算基于同一观测；十进制精度不低于现状（rate 42,30；倒数使用高精度十进制上下文）。
- **NFR-4（迁移安全）**: 迁移可在常规 Django migration 流程中执行、可重复在备份上演练；基线快照在迁移同一事务窗口内生成；迁移失败不破坏旧表。
- **NFR-5（性能）**: 报表生成中观测解析按 (币对, 目标时点) 去重并使用带修订版本的请求级缓存，避免逐交易重复查询；常用选取路径具备数据库索引支撑。
- **NFR-6（时区正确）**: 日度定盘与实时报价的边界比较规则统一且可解释；跨时区日期边界（如欧央行 CET 发布日、K 线带时区时间戳）处理正确。

## Constraints
- **Technical**: Django + Postgres（现状 SQL 中 `1/rate` 与 Python 十进制倒数可能存在末位差异，基线比对采用取整后一致标准）；`USE_TZ=True`；抓取由 procrastinate 每小时任务驱动；现有调用点分散在 transactions、insights、net_worth、mini_tools、dca、api 六个应用。
- **Business**: 历史口径诚实优先于报表无缺失——宁可以显式缺失呈现，也不得把未来值或不可考值伪装成当时已知值；但通过回退配置允许用户在知情前提下选择可用性。
- **Dependencies**: 各提供方接口时间字段的完整性（Frankfurter/FXMacroData 有日期，其余仅能取抓取时刻）；数据迁移需在停写/低峰窗口执行基线快照。

## Assumptions
- 用户跳过澄清即表示接受固化的四项默认决策（7 天、严格+可回退、迁移旧行+更早缺失、持久化基线+校验命令）。
- 日度定盘观测在其 `effective_date` 当日全天适用（含发布时刻之后），周末/节假日缺口自然由"不晚于目标日期的最近观测"覆盖。
- 月度聚合采用期末时点汇率而非逐交易汇率（实现与性能平衡，且保证月内单一口径与期间稳定性）。
- 单例服务迁移前的历史明细在物理上已不可恢复，用户接受以缺失与标注呈现。

## Acceptance Criteria

### AC-1: 抓取只追加不可变观测
- **Type**: `rule`
- **Given**: 某币对已存在历史观测
- **When**: 对同一服务连续执行两次抓取
- **Then**: 观测总数增加；先前行所有字段（含日期、数值）保持不变；对已存在观测实例调用 `save()` 修改字段会被拒绝，调用 `delete()` 会被拒绝
- **Pass Condition**: 自动化测试覆盖"两次抓取计数增加+旧行字段未变+更新/删除抛错"
- **Evidence**: currencies 新增测试用例输出

### AC-2: 禁止未来观测用于过去目标日期
- **Type**: `rule`
- **Given**: 币对仅有一条有效日期晚于目标日期的观测，回退关闭
- **When**: 以该目标日期请求换算
- **Then**: 返回状态 `missing_no_observation`，不返回任何未来观测值
- **Pass Condition**: 测试构造未来观测断言状态且无数值返回
- **Evidence**: convert 模块测试

### AC-3: 陈旧期生效且可配置
- **Type**: `rule`
- **Given**: 最近观测距目标日期超过 `max_staleness_days`（默认 7）
- **When**: 请求换算
- **Then**: 返回状态 `missing_stale`；将配置调大到覆盖该间隔后再次请求返回 `ok`
- **Pass Condition**: 两种配置下的状态断言均通过
- **Evidence**: 策略/换算测试

### AC-4: 缺失状态显式呈现且不混入汇总
- **Type**: `rule`
- **Given**: 某币对折算缺失
- **When**: 渲染交易行与月度汇总
- **Then**: 折算区域出现明确的缺失标记，原币金额正常展示；汇总折算列不以 0 静默替代该行
- **Pass Condition**: 视图/模板渲染测试断言缺失标记存在且折算合计不含静默 0
- **Evidence**: 模板测试与 API 响应断言

### AC-5: 观测来源元数据完整
- **Type**: `rule`
- **Given**: 任一新产出的观测
- **When**: 读取该观测
- **Then**: 提供方标识、批次 ID、`fetched_at`、`effective_at`、`effective_date`、时间类型、来源、币向、修订序号均可读取；传递型观测含组成观测 ID
- **Pass Condition**: 测试逐字段断言
- **Evidence**: 模型/抓取测试

### AC-6: singleton 不再覆写历史
- **Type**: `rule`
- **Given**: `singleton=True` 的服务已有一条观测
- **When**: 再次抓取成功
- **Then**: 产生第二条新观测，旧行日期与数值不变；服务 `current_observation` 指向新观测
- **Pass Condition**: 测试断言旧行未变、指针更新
- **Evidence**: fetcher 测试

### AC-7: 报表返回观测标识与溯源信息
- **Type**: `rule`
- **Given**: 一笔多币种交易及其 API 访问
- **When**: 调用 `exchanged_amount()` 与交易序列化接口
- **Then**: 返回中包含 observation_id、revision、provider、batch_id、fetched_at、effective 信息；交易模板页面可见来源信息
- **Pass Condition**: 序列化字段与模板内容断言
- **Evidence**: API 测试、模板测试

### AC-8: 正反向换算共用同一观测
- **Type**: `rule`
- **Given**: 币对仅有一条 A→B 观测
- **When**: 分别请求 A→B 与 B→A 换算
- **Then**: 两结果引用同一 observation_id；反向结果方向标记 inverted 且数值为正数倒数派生
- **Pass Condition**: 同 ID 断言与数值精度断言
- **Evidence**: convert 测试

### AC-9: 已结算期间重开数值稳定
- **Type**: `rule`
- **Given**: 某已结算月份（及净资产该月快照）的报表结果
- **When**: 后续执行多次抓取后重新生成同一期间报表
- **Then**: 折算值与所采用观测标识与首次结果逐位一致
- **Pass Condition**: 测试保存首次结果，抓取后重算并全量比对
- **Evidence**: 月度/净资产回归测试

### AC-10: 迁移生成观测且基线校验通过
- **Type**: `rule`
- **Given**: 含若干 `ExchangeRate`（含自动/手工、含 singleton 服务）的迁移前库
- **When**: 执行数据迁移并运行基线校验命令
- **Then**: 每条旧汇率对应一条观测；基线记录生成；账户原币余额迁移前后完全相等；当前折算展示金额取整后一致，不一致有差异输出
- **Pass Condition**: 迁移后行数/字段断言 + 校验命令退出码与报告内容断言
- **Evidence**: 迁移测试中命令输出

### AC-11: 回退开关行为正确且带标记
- **Type**: `rule`
- **Given**: 仅有未来观测、目标日期早于该观测
- **When**: 分别在回退关闭/开启下请求换算
- **Then**: 关闭时 `missing_no_observation`；开启时返回最近观测且 `fallback=True`
- **Pass Condition**: 两种状态断言
- **Evidence**: 策略测试

### AC-12: 修订链可追溯，固定标识保持旧值
- **Type**: `rule`
- **Given**: 币对某日存在观测 v1
- **When**: 以修订方式登记修正值
- **Then**: 新增 v2（`revision_of=v1`，序号递增，原因可查），v1 仍可按 ID 读取且值不变；未固定观测的最新选取得到 v2；按 v1 ID 固定解析仍得旧值
- **Pass Condition**: 修订链字段、值不变、两种解析结果断言
- **Evidence**: 修订服务测试

### AC-13: 缓存键含修订号
- **Type**: `rule`
- **Given**: 某 (币对, 目标时点) 的换算解析结果已缓存
- **When**: 该币对出现新修订观测并再次解析
- **Then**: 缓存键改变、结果重算并指向新修订；无新修订时命中缓存
- **Pass Condition**: 测试断言缓存命中/未命中与结果观测 ID
- **Evidence**: 缓存工具测试

### AC-14: 跨提供方日期边界与时区处理质量
- **Type**: `rubric`
- **Dimension**: 日度定盘（周末/节假日缺口、发布日本地日期）、带时区 K 线时间戳、实时报价三类语义在选取中的正确性与规则一致性
- **Scale**: 1-5
- **Anchors**: 1 = 边界比较混用日期与时刻，周末/跨时区场景选错观测；3 = 常见边界正确但规则分散、部分场景靠隐式行为；5 = 统一可解释的候选规则，边界场景（周五定盘用于周末、CET 发布日、K 线时区、节假日长缺口）全部有测试锁定
- **Pass Threshold**: >= 4
- **Evidence**: 边界测试集与代码评审

### AC-15: 审计取证完整度
- **Type**: `rubric`
- **Dimension**: 仅凭库内记录还原任一历史折算值完整来源的能力
- **Scale**: 1-5
- **Anchors**: 1 = 仍无法说明提供方/批次/时点；3 = 新数据可溯源但迁移数据与传递推导链路缺失；5 = 新数据、迁移遗留、传递推导三类均可完整取证且修订链闭合
- **Pass Threshold**: >= 4
- **Evidence**: 溯源走查（随机抽样报表数值反查）记录

### AC-16: 迁移安全与回滚证据
- **Type**: `rubric`
- **Dimension**: 迁移可演练性、基线完整性、旧表冻结与回滚窗口证据
- **Scale**: 1-5
- **Anchors**: 1 = 无基线或迁移破坏旧数据；3 = 有基线但比对靠人工、旧表仍可被写入；5 = 一键校验、差异可报告、旧表只读冻结、迁移步骤可重复演练
- **Pass Threshold**: >= 4
- **Evidence**: 演练记录与命令输出

## Open Questions
- [ ] 旧 `ExchangeRate` 只读审计表保留多长时间、满足何种条件后可归档/删除（后续单独决策）。
- [ ] 是否需要按提供方/服务的陈旧期覆盖与多档时间类型默认值（列为 v2）。
- [ ] 月度聚合采用期末汇率（本期方案）是否长期满足口径要求，未来是否改为逐交易折算。
