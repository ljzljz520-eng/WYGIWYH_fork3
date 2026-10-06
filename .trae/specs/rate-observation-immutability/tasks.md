# 不可变汇率观测（RateObservation）与时点换算 - 实施计划

测试约定：Django 原生命令，从仓库根执行 `python app/manage.py test <path>`；迁移逻辑抽为可单测的纯函数模块；所有新增测试不依赖外网（提供方全部 mock）。

## Task 1: 观测领域模型与结构迁移
- **Status**: `completed`
- **Completion Evidence**:
  - TR-1.1 pass：四个新模型（RateFetchBatch/RateObservation/RatePolicyConfig/RateMigrationBaseline）+ current_observation 指针落库，迁移 0026 apply 成功；字段读回测试通过。
  - TR-1.2 pass：已存在观测改字段 save 抛 ImmutableObservationError、delete 抛错；插入正常。
  - `apps.currencies` 测试 28/28 OK（基线 19 → 新增 9）。
- **Priority**: high
- **Depends On**: None
- **Description**:
  - 在 currencies 应用新增模型：`RateFetchBatch`（UUID 主键、服务 FK 可空、触发方式、started/finished_at、状态、收到/落库条数、错误摘要）、`RateObservation`（FR-1 全部字段与索引：币对+effective_at、provider 等）、`RatePolicyConfig`（单例：max_staleness_days 默认 7、allow_closest_fallback 默认 False）、`RateMigrationBaseline`（迁移标签、账户/币对 FK、原币余额、迁移前折算值与旧汇率快照字段、迁移后观测 ID/重算值、比对状态与 delta、created_at）。
  - `ExchangeRateService` 增加 `current_observation` FK（SET_NULL，related_name 唯一）。
  - 在 `RateObservation` 上实现不可变保护：已存在主键实例的字段更新/`save`/`delete` 抛出明确异常；仅允许插入。
  - 生成 schema 迁移（编号承接 currencies 0025 → 0026）。
- **Acceptance Criteria Addressed**: AC-1, AC-5
- **Test Requirements**:
  - `rule` TR-1.1: 各模型可按 FR-1/FR-3/FR-7 字段创建并读回；索引与约束存在（通过迁移状态与 ORM 建表验证）。证据：模型测试通过输出。
  - `rule` TR-1.2: 已创建的 RateObservation 修改任意字段后 `save()` 抛错、`delete()` 抛错；新建插入成功。证据：测试断言异常类型。
- **Notes**: RateObservation 不使用任何 auto_now 字段（与不可变语义冲突）；Meta 中不得出现会引发 ORM 隐式更新的唯一约束（允许同币对同 effective_at 存在多修订行）。

## Task 2: 时点选取策略、结构化换算结果与版本化缓存
- **Status**: `completed`
- **Completion Evidence**:
  - TR-2.1..2.5 pass：未来观测禁用、7 天边界（7 可用/8 stale/配置调大恢复）、正反向同 observation_id、回退两态、缓存键修订版本与不同金额复用，全部断言通过（test_rate_selection.py）。
  - TR-2.6 rubric 得分 4/5：候选规则统一（日度按日期、实时/传递按时刻），周五定盘用于周末、K 线时区边界、节假日长缺口、同点多修订均有锁定测试；未满分原因：提供方各自时区声明在 Task 3 落地、CET 发布时点尚未专门锁定。
  - 全量回归：除 34 个既有环境问题（Vite 清单/附件文件系统）外无新增失败；transactions 汇率测试夹具已改用 RateObservation。
- **Priority**: high
- **Depends On**: Task 1
- **Description**:
  - 定义 `ConversionResult`（值、状态 ok/same_currency/zero/missing_no_observation/missing_stale、方向 direct/inverted、observation_id、revision、provenance：provider/batch_id/fetched_at/effective_at/effective_date/fallback）。
  - 实现观测选取：日度定盘按 effective_date ≤ 目标本地日期、实时按 effective_at ≤ 目标时点；候选按 effective_at 降序、revision 降序、id 降序；陈旧期按 RatePolicyConfig（无行回落 settings 默认 7 天）过滤并区分两种缺失状态；回退开启时取绝对距离最近（含未来）并标 fallback。
  - 反向值按高精度 Decimal 上下文倒数派生，与正向返回同一 observation_id。
  - 缓存工具：缓存键包含 (规范化币对, 目标时点, 观测最大修订号)；请求级 memoization；确认 django-cachalot 对 observation 表的写入失效行为不被破坏（纯追加写入即触发表级失效）。
- **Acceptance Criteria Addressed**: AC-2, AC-3, AC-8, AC-11, AC-13, AC-14
- **Test Requirements**:
  - `rule` TR-2.1: 仅有未来观测、回退关闭 → missing_no_observation。证据：选择器测试。
  - `rule` TR-2.2: 观测间隔 7 天边界（恰好 7 天可用、第 8 天 missing_stale）；调大配置后恢复 ok。证据：参数化测试。
  - `rule` TR-2.3: 正反向请求返回同一 observation_id，反向为倒数且数值精度断言通过。证据：测试输出。
  - `rule` TR-2.4: 回退关闭/开启两态断言（缺失 vs fallback=True 且含未来观测）。证据：测试输出。
  - `rule` TR-2.5: 同一币对修订后缓存键变化、结果指向新修订；无修订时命中缓存。证据：缓存命中计数/结果 ID 断言。
  - `rubric` TR-2.6: 跨类型日期边界正确性；scale 1-5；anchors 同 AC-14；threshold >= 4；证据：边界用例集（周五定盘用于周末、CET 发布日、K 线时区、节假日长缺口、同点多修订）。
- **Notes**: 目标时点统一用 datetime 表达；日度比较的"本地日期"按用户 settings 时区计算。

## Task 3: 提供方契约升级（有效时点与传递推导）
- **Status**: `completed`
- **Completion Evidence**:
  - TR-3.1 pass：base.py 定义 ProviderRate（from/to/rate/effective_at/effective_date/temporal_kind/derivation），抽象契约升级。
  - TR-3.2 pass：Frankfurter/FXMacro 日度定盘按响应 date，发布时点 16:00 Europe/Berlin；YFinance 取最后 K线时间戳并转 UTC（带时区边界测试）；TwelveData/CoinGecko/Markets 取抓取时刻。
  - TR-3.3 pass：TransitiveRateProvider 改读 RateObservation 构图，每方向对取最新，derivation 带 path_codes/path_ids/observation_ids；3 个新测试全绿。
  - 全 42 个 currencies 测试通过（含旧 fetcher tuple-mock 用例）。
- **Priority**: high
- **Depends On**: Task 2
- **Description**:
  - 升级 `ExchangeRateProvider` 抽象：返回结构含 (from, to, rate, effective_at, effective_date, temporal_kind)；提供方声明时区/日历口径。
  - 逐提供方适配：Frankfurter 取响应 date（daily）；FXMacroData 取最新非空行 date（daily）；YFinance 取最后一根 K线时间戳；TwelveData/CoinGecko 无时间字段以抓取时刻（realtime）；TwelveDataMarkets 保留报价时点。
  - TransitiveRateProvider 改为读取 RateObservation 构图，产出 temporal_kind=transitive 结果，derivation 携带路径与组成观测 ID。
- **Acceptance Criteria Addressed**: AC-5, AC-14
- **Test Requirements**:
  - `rule` TR-3.1: 各提供方 mock 响应下返回的 effective 信息正确（Frankfurter/FXMacro 日期、YFinance 时间戳、实时类抓取时刻）。证据：扩展现有 test_fxmacrodata_provider/test_yfinance_provider 及新增用例。
  - `rule` TR-3.2: 传递型结果 derivation 中路径与组成 observation_id 完整，数值沿路径连乘正确。证据：测试断言。
- **Notes**: 现有 provider 测试必须继续通过（按新契约调整断言）。

## Task 4: 抓取器重构（批次、只追加、当前观测指针）
- **Status**: `completed`
- **Completion Evidence**:
  - TR-4.1 pass：每次运行创建 RateFetchBatch（started_at/trigger），success/partial/failed 三态与 counts/error_summary 均有测试。
  - TR-4.2 pass：删除 singleton 原地覆写分支；重复抓取产生两行不同批次观测，旧 ExchangeRate 表零写入，current_observation 指向最新。
  - TR-4.3 pass：失败计数语义保持（5 个旧用例适配 ProviderRate mock 后全过），新增 4 个不可变存储用例。
  - 46 个 currencies 测试通过。
- **Priority**: high
- **Depends On**: Task 3
- **Description**:
  - `fetch_due_rates`/手动任务每次运行创建 RateFetchBatch（定时/手动触发标记），抓取结束更新批次状态与计数、错误摘要。
  - 删除 singleton 覆写分支：每条提供方结果一律 create 新 RateObservation（含批次、服务、提供方、effective 信息、来源 automatic_fetch/transitive）；成功后更新 `service.current_observation` 与 `last_fetch`、failure_count=0；异常时 failure_count 累加、last_fetch 不动（维持现状语义）。
  - processed_pairs 去重逻辑保留。
- **Acceptance Criteria Addressed**: AC-1, AC-5, AC-6
- **Test Requirements**:
  - `rule` TR-4.1: singleton=True 连续两次抓取：两条观测、旧行字段未变、current_observation 指向新行、批次两条记录归属合理。证据：fetcher 测试。
  - `rule` TR-4.2: 失败路径 failure_count 累加且 last_fetch 不变；成功路径归零且更新 last_fetch（沿用 test_automatic_exchange_rates 全部用例并通过）。证据：现有测试输出。
  - `rule` TR-4.3: 批次字段（触发方式、计数、状态、错误摘要）与观测 batch 关联正确。证据：测试断言。
- **Notes**: 手动抓取任务走同一入口，仅 trigger 不同。

## Task 5: 交易模型与汇总计算的 as-of 改造
- **Status**: `completed`
- **Completion Evidence**:
  - TR-5.1 pass：exchanged_amount() 返回 status/amount/observation_id/revision/provider/effective_date/batch_id/fallback/direction；缺失时 amount=None 且状态显式（2 个模型测试）。
  - TR-5.2 pass：calculate_currency_totals/calculate_account_totals 增加 as_of；整块共用一次观测解析；缺失字段为 None 不静默 0；consolidated 传播缺失状态。
  - TR-5.3 pass：未来观测不转换过去总额（专门测试）；percentage 分布对 None 安全。
  - 18 个相关测试通过。
- **Priority**: high
- **Depends On**: Task 2
- **Description**:
  - `Transaction.exchanged_amount()` 改为返回 ConversionResult 序列化字典（值+状态+observation_id/revision/provenance），保持"账户 exchange_currency 优先、其次币种默认目标"的两级回退结构。
  - `calculate_currency_totals`、`calculate_account_totals` 增加 `as_of` 目标时点参数；折算缺失时显式挂 missing 状态而非静默 0；deep_search 分支同步。
- **Acceptance Criteria Addressed**: AC-4, AC-7, AC-9
- **Test Requirements**:
  - `rule` TR-5.1: exchanged_amount 在 ok/两类缺失下返回结构正确（含观测标识或缺失状态）。证据：transactions/test_models.py 扩展用例。
  - `rule` TR-5.2: 汇总函数 as_of 取不同时点结果不同且可复现；缺失行不产生静默 0 折算。证据：calculations 测试。
- **Notes**: 与 Task 4 无文件重叠冲突（models 方法 vs fetcher），可并行开发但先合入者为准。

## Task 6: 报表视图、模板、API 与旧表冻结
- **Status**: `completed`
- **Completion Evidence**:
  - TR-6.1 pass：新增 test_settled_months_stay_identical_after_later_fetches（净资产逐月月末取观测；3/4 月新抓取后 1/2 月折算值与 observation_id 全量一致）。
  - TR-6.2 pass：test_missing_rate_records_explicit_status_for_settled_month（consolidated_missing 记录 missing_stale）；item.html 缺失标记+原币保留、来源 tooltip；currency_card 缺失 badge；API 新测试 test_ok_conversion_carries_observation_provenance / test_missing_conversion_is_explicit。
  - TR-6.3 pass：test_exchange_rate_freeze 4 用例（POST 新增/编辑 403、DELETE 403、只读视图 200；旧行零变更）；admin ExchangeRate 只读。
  - mini_tools exchange_rate_map/视图改 as-of now 观测解析并展示 provider/effective_date；DCA current_price/current_value 用 resolve_rate，API current_price 携 provenance。
  - 相关 33 个测试通过；无新增回归（22 个 transactions 失败为既有 Vite/附件环境问题）。
- **Priority**: medium
- **Depends On**: Task 5
- **Description**:
  - 各调用点传入正确目标时点：monthly_overview 月末日期；insights month_by_month 逐月月末、year_by_year 年末、overview 按所展示期间；net_worth 合并口径重构为逐月月末观测（建立 label→实际月份日期映射，禁止单一汇率贯穿）；mini_tools 换算器与 exchange_rate_map 改为 as-of now 的观测解析并展示来源；DCA 当前价采用观测结果。
  - 模板：折算区域支持缺失标记（原币保留）与来源提示（提供方/有效日期/批次）；交易 item.html、月度汇总、净资产图表 tooltip 同步。
  - API：`get_exchanged_amount` 输出观测标识与 provenance。
  - 冻结旧表：ExchangeRate 编辑/新增/删除视图与路由停用（或返回只读提示），admin 设置为只读。
- **Acceptance Criteria Addressed**: AC-4, AC-7, AC-9, AC-17
- **Test Requirements**:
  - `rule` TR-6.1: 已结算月度/净资产结果首次生成后，模拟后续抓取再重算，折算值与 observation_id 全量一致。证据：回归测试。
  - `rule` TR-6.2: 缺失场景渲染缺失标记且原币金额可见；API 响应含 observation_id/provenance。证据：视图/模板/API 测试。
  - `rule` TR-6.3: ExchangeRate 写入口（视图 POST/DELETE、admin 变更）被阻止，只读列表仍可访问。证据：测试断言 403/405/只读行为。
- **Notes**: 模板改动遵循现有 cotton + daisyUI 结构；翻译串走 gettext。

## Task 7: 观测修订登记与按标识固定解析
- **Status**: `completed`
- **Completion Evidence**:
  - TR-7.1 pass：apps/currencies/utils/revisions.py 新增 register_correction（revision_of 链接、revision+1、source=correction、effective 点不变、原因必填）；test_revision_service.py 5 用例：v1 不被覆写、链式 v3 链接 v2、未固定选取取得 v2、resolve_pinned 分别固定 v1/v2 值得 120/125、空原因 ValueError。
- **Priority**: medium
- **Depends On**: Task 2
- **Description**:
  - 提供修订服务（服务层函数，供管理命令/后台使用）：对既有观测登记修正 → 新增 RateObservation（revision_of 指向前序、revision 序号 +1、记录修订原因与来源 correction）。
  - 提供按 observation_id 固定解析（pinned resolve）：ID 在即返回该不可变值，不受后续修订影响；未固定的"最新选取"自动获得最新修订（复用 Task 2 排序）。
- **Acceptance Criteria Addressed**: AC-12
- **Test Requirements**:
  - `rule` TR-7.1: 修订后 v1 值不变、v2 链接/序号/原因正确；未固定选取得 v2；按 v1 ID 固定解析得旧值。证据：修订服务测试。
- **Notes**: 修订不改变旧观测 effective_at（避免重写历史语义）；同 effective_at 多修订靠排序裁决。

## Task 8: 旧数据迁移（基线快照 + 行转换 + 指针匹配）
- **Status**: `completed`
- **Completion Evidence**:
  - TR-8.1 pass：legacy_rate_migration.py 全部辅助函数；test_legacy_rate_migration.py 4 用例（字段映射 automatic/manual 来源、provider/derivation、created_at 必填）。
  - TR-8.2 pass：run_forward 完整序列（基线先行→1:1 转换→链接→服务指针）；空库安全；run_reverse 清除迁移数据保留旧表。
  - 真实历史模型验证：临时 Postgres 库全量 migrate 成功（0027 apply），反向 migrate currencies 0026 成功。关键坑：需跨应用依赖 accounts 0016/transactions 0050（否则 RunPython 注册表缺事务应用）；历史模型无嵌套 Choices（用字面值）；查询集必须无用户过滤（all_objects/userless_all_objects）。
- **Priority**: high
- **Depends On**: Task 1, Task 4
- **Description**:
  - 生成数据迁移 0027：
    1. 在同一迁移窗口按旧逻辑生成 RateMigrationBaseline：每个账户当前原币余额、当前折算值（旧逻辑、当前时刻）、所用 ExchangeRate 的 id/date/rate；
    2. 将每条 ExchangeRate 转为 RateObservation（effective_at=旧 date；effective_date 按旧 date 在用户/UTC 口径取日期；fetched_at 不可考=effective_at 并在来源标注；automatic → 自动抓取，否则手工录入；provider 不可考 → 遗留提供方标识）；
    3. 按币对+服务目标集合尽力匹配并设置各服务 current_observation。
  - 迁移逻辑放独立可导入辅助模块以便单测；迁移中显式标注 singleton 历史不可恢复。
- **Acceptance Criteria Addressed**: AC-10, AC-15
- **Test Requirements**:
  - `rule` TR-8.1: 转换函数：旧行 1:1 变观测，字段映射正确，自动/手工/遗留来源标注正确。证据：辅助函数单测。
  - `rule` TR-8.2: 端到端：从 0025 状态造数 → 迁移到 0027 → 观测数=旧行数、基线记录齐全、服务指针指向各币对最新观测。证据：迁移测试（使用 migration 回放到 0025→前向迁移）。
  - `rubric` TR-8.3: 审计取证完整度；scale 1-5；anchors 同 AC-15（新数据/迁移遗留/传递推导三类可反查）；threshold >= 4；证据：抽样反查走查记录。
- **Notes**: 基线快照必须在任何行转换前落库；迁移对空库安全。

## Task 9: 基线校验管理命令
- **Status**: `completed`
- **Completion Evidence**:
  - TR-9.1 pass：rate_baseline_verify 命令（只读）；test_rate_baseline_verify.py 4 用例：一致场景 rc0 [MATCH]、折算不一致（后续抓取 1.9）rc1 [MISMATCH]、原币余额不一致 rc1、未知 label rc1 提示无基线。
  - 规则：原币完全相等；折算容差一个最小展示单位（10^-decimal_places）；双侧缺失视为通过。
- **Priority**: medium
- **Depends On**: Task 8
- **Description**:
  - 新增管理命令（如 `rate_baseline_verify`）：按新逻辑重算当前折算，与 RateMigrationBaseline 逐项比对——原币余额完全相等；折算值按目标币种小数位取整后相等（容差不超过一个最小展示单位）；输出逐账户结果与差异报告及汇总退出码。
- **Acceptance Criteria Addressed**: AC-10, AC-16
- **Test Requirements**:
  - `rule` TR-9.1: 一致场景命令退出码表示通过且报告逐项一致；构造差异场景输出差异明细与非通过退出码。证据：命令测试（call_command 输出断言）。
  - `rubric` TR-9.2: 迁移安全与回滚证据；scale 1-5；anchors 同 AC-16；threshold >= 4；证据：在测试库演练 0025→0027 全流程 + 旧表只读确认 + 命令输出归档路径说明。
- **Notes**: 命令只读，不产生写入。

## Issue I-1: fallback 回退选择应以目标时点为参考
- **Status**: `completed`
- **Completion Evidence**:
  - select_closest_observation 改为接收 target 并按距目标时点的绝对距离选取；新增 test_fallback_measures_distance_from_target_not_now。回归：currencies + api + mini_tools + net_worth 74 tests OK；transactions/dca/monthly/yearly/insights 28 tests OK。
- **Priority**: high
- **Depends On**: None
- **Discovered By**: Review R1（F-1，CP-2）
- **Description**: select_closest_observation 改为接收 target，按距目标时点的绝对距离选择；resolve_conversion 调用点传入目标时点。
- **Acceptance Criteria Addressed**: AC-11
- **Test Requirements**:
  - `rule` TR-I-1.1: 目标时点两侧（或均过去、距目标一近一远）≥2 观测，开启 fallback 后返回距目标最近者且 fallback=True；保留单未来观测断言。
- **Notes**: -

## Issue I-2: 冻结旧表 API 写入口
- **Status**: `completed`
- **Completion Evidence**:
  - ExchangeRateViewSet 改 ReadOnlyModelViewSet；新增 apps/api/tests/test_currencies.py 断言 POST/PUT/PATCH/DELETE 被 403/405 阻止且旧表不变、GET 可用。同上回归通过。
- **Priority**: high
- **Depends On**: None
- **Discovered By**: Review R1（F-2，CP-1）
- **Description**: ExchangeRateViewSet 写方法（create/update/partial_update/destroy）返回 403/405，GET 保留。
- **Acceptance Criteria Addressed**: AC-1, AC-17
- **Test Requirements**:
  - `rule` TR-I-2.1: POST/PUT/PATCH/DELETE /api/exchange-rates/ 被阻止且旧表不变；list/retrieve 200。
- **Notes**: -

## Issue I-3: transitive 观测 source 正确标注
- **Status**: `completed`
- **Completion Evidence**:
  - fetcher 对 temporal_kind=transitive 落 source=transitive；新增 test_transitive_rates_are_marked_transitive_source。同上回归通过。
- **Priority**: medium
- **Depends On**: None
- **Discovered By**: Review R1（F-3，CP-4）
- **Description**: fetcher 对 temporal_kind=transitive 的 ProviderRate 落 source=transitive。
- **Acceptance Criteria Addressed**: AC-5
- **Test Requirements**:
  - `rule` TR-I-3.1: transitive 服务抓取后观测行 source=transitive，derivation observation_ids 保留。
- **Notes**: -

## Issue I-4: exchange_rate_map 条目携带 observation_id
- **Status**: `completed`
- **Completion Evidence**:
  - _build_provenance 增加 observation_id（根因修复），exchange_rate_map 条目取得真实 ID；mini_tools/tests.py 新增双向同 ID 用例。同上回归通过。
- **Priority**: medium
- **Depends On**: None
- **Discovered By**: Review R1（F-4，CP-5）
- **Description**: _entry 直接取 result.observation_id。
- **Acceptance Criteria Addressed**: AC-7
- **Test Requirements**:
  - `rule` TR-I-4.1: map 正反向条目 observation_id 为真实观测 ID 且双向相同。
- **Notes**: -

## Issue I-5: 迁移基线余额排除软删除交易
- **Status**: `completed`
- **Completion Evidence**:
  - calculate_native_balances 加 deleted=False；verify 命令修正零余额基线（pre None + 余额 0）判定；新增软删除场景用例覆盖基线=UI 余额且 verify 全 MATCH。同上回归通过。
- **Priority**: medium
- **Depends On**: None
- **Discovered By**: Review R1（F-5，CP-7）
- **Description**: calculate_native_balances 在软删除启用时过滤 deleted=False（或显式 deleted 字段），口径与 UI 当前余额一致；校验命令同口径。
- **Acceptance Criteria Addressed**: AC-10
- **Test Requirements**:
  - `rule` TR-I-5.1: ENABLE_SOFT_DELETE=true 软删一笔 paid 交易后迁移，original_balance 等于账户当前余额，verify 命令输出 MATCH。
- **Notes**: 默认 ENABLE_SOFT_DELETE=false，需在测试中显式开启。

## 依赖顺序总览
Task 1 → Task 2 → Task 3 → Task 4；Task 2 → Task 5 → Task 6；Task 2 → Task 7；Task 1 + Task 4 → Task 8 → Task 9。
允许并行：Task 5 与 Task 3/4 可并行（文件边界不重叠）；Task 7 可与 Task 3/4/5 并行。
