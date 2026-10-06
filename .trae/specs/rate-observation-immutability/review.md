# 不可变汇率观测与时点换算 - Independent Review

- [x] CP-1: 抓取只追加不可变观测
  - **Type**: `rule`
  - **Covers**: AC-1
  - **Pass Condition**: 每次抓取新增 RateObservation，已存在观测 save/delete 抛 ImmutableObservationError，旧表不被写入
  - **Evidence R2**: `pass` — R1 的 API 冻结缺口（F-2）已修复；只追加、不可变守卫、web/admin/API 三层冻结均独立核实。

- [x] CP-2: 时点策略（禁未来 / 陈旧期 / 回退）
  - **Type**: `rule`
  - **Covers**: AC-2, AC-3, AC-11
  - **Pass Condition**: 默认仅选 effective ≤ target 的最近观测；7 天陈旧期可配置；allow_closest_fallback 开启才允许未来值且 fallback=True
  - **Evidence R2**: `pass` — R1 的 fallback 参考点缺陷（F-1）已修复，距离以目标时点计；禁未来与陈旧期边界正确。

- [x] CP-3: 缺失状态显式且不混入汇总
  - **Type**: `rule`
  - **Covers**: AC-4
  - **Pass Condition**: missing_no_observation / missing_stale 显式传播至视图/模板/API，缺失折算字段为 None 而非静默 0，原币金额保留
  - **Evidence R2**: `pass` — 全链显式缺失，无静默 0（advisory：模板标记无渲染断言，A-8）。

- [x] CP-4: 来源元数据完整，singleton 只指当前观测
  - **Type**: `rule`
  - **Covers**: AC-5, AC-6
  - **Pass Condition**: 观测记录 provider/batch/fetched_at/effective_at/effective_date/temporal_kind/source/direction；singleton 服务仅更新 current_observation 指针，不覆写任何历史
  - **Evidence R2**: `pass` — R1 的 transitive source 误标（F-3）已修复；字段逐字段落库。

- [x] CP-5: 报表携观测标识，正反向共用同一观测
  - **Type**: `rule`
  - **Covers**: AC-7, AC-8
  - **Pass Condition**: 视图/模板/API 可取到 observation_id 与 provenance；反向返回同一 observation_id，值为高精度倒数；净资产/月度/年度逐月（期）取观测
  - **Evidence R2**: `pass` — R1 的 map observation_id 丢失（F-4）已修复；净资产/insights/yearly 逐期取观测独立核实。

- [x] CP-6: 已结算期间稳定，修订链可追溯且固定解析保持旧值
  - **Type**: `rule`
  - **Covers**: AC-9, AC-12
  - **Pass Condition**: 后续抓取后已结算月份折算值与 observation_id 不变；correction 追加 revision（revision_of 链接、revision+1、原因记录）；resolve_pinned 按 ID 得旧值
  - **Evidence R2**: `pass` — 已结算稳定性测试逐位锁定，修订链与 pinned 解析核实。

- [x] CP-7: 迁移生成观测、基线校验可用、缓存键含修订号
  - **Type**: `rule`
  - **Covers**: AC-10, AC-13
  - **Pass Condition**: 0027 旧行 1:1 转观测、基线先行落库、服务指针匹配、空库安全、正反向迁移可执行；rate_baseline_verify 按规则校验；缓存键包含修订号
  - **Evidence R2**: `pass` — R1 的软删除口径（F-5）已修复；真实 Postgres 空库/播种两场景前向、反向、重前向演练成功；verify 命令 MATCH、exit 0。

- [x] CP-U1: 跨提供方日期边界、时区、反向精度、节假日缺口
  - **Type**: `rubric`
  - **Covers**: AC-14
  - **Scale**: 1-5
  - **Anchors**: 1 = 提供方时区混用、周末/节假日取到未来或错误日期；3 = 主要边界正确但反向精度或发布时点有遗漏；5 = 各提供方 effective 口径、CET 16:00 定盘、K 线时区、反向高精度、周末与节假日缺口均有代码与测试锁定
  - **Pass Threshold**: >= 4
  - **Evidence R2**: **4/5** — DST、K 线 UTC、周末/节假日 stale、多修订均锁定；扣分：CET 发布时点无专门断言、传递反向边 28 位上下文（A-10）。

- [x] CP-U2: 审计取证完整度
  - **Type**: `rubric`
  - **Covers**: AC-15
  - **Scale**: 1-5
  - **Anchors**: 1 = 无法反查数值来源；3 = 来源可查但批次/推导链不全；5 = 新抓取/迁移遗留/传递推导三类均可经 provider、batch、derivation、基线反查
  - **Pass Threshold**: >= 4
  - **Evidence R2**: **4/5** — 三类反查链经真实库演练闭合；扣分：singleton 标注（A-9）、correction 回滚边界（A-5）。

- [x] CP-U3: 迁移安全与回滚证据
  - **Type**: `rubric`
  - **Covers**: AC-16
  - **Scale**: 1-5
  - **Anchors**: 1 = 迁移可能改写余额或无法回退；3 = 余额一致但回滚/基线演练不完整；5 = 基线先行、旧表冻结只读、当前余额迁移前后一致、正反迁移与校验命令均有演练证据
  - **Pass Threshold**: >= 4
  - **Evidence R2**: **4/5** — 基线先行、真实 Postgres 全量迁移正反演练、verify 退出码语义、旧表三层冻结独立复现；扣分：策略变更不入缓存键（A-11）、correction 回滚边界（A-5）。

## Review History

### Review R1
- **Result**: `fail`
- **Reviewer**: 全新只读独立 subagent（Agent 0c505a18-2c91-4d3b-a31c-9924d6c520a2）
- **Evidence**:
  - 独立运行：currencies 63 OK；net_worth 3 OK；dca 10 OK；monthly_overview 14 OK；api test_transactions 2 OK；transactions 51 中 21 errors+1 failure 全部为既有 Vite/附件环境问题（逐条核对不含汇率失败）。
  - Actionable 发现 5 条：F-1 fallback 参考点错误；F-2 ExchangeRate API 写入口未冻结；F-3 transitive 观测 source 误标；F-4 exchange_rate_map observation_id 恒 None；F-5 迁移基线未排除软删除交易。
  - Advisory 11 条（F-6~F-16）：queryset 绕过不可变保护、transitive effective_at 历史不可达、pinned 不校验币对、修订分叉、reverse 误删 correction、DCA 缺失显示为 0、日度索引、空测试覆盖、singleton 标注、传递反向边精度、Python 乘法精度。
- **Routing**: 转 Implement，登记 Issue I-1~I-5 后修复，修复完成后以全新 reviewer 开始 Review R2。

### Review R2
- **Result**: `pass`
- **Reviewer**: 全新只读独立 subagent（Agent ff643131-5be7-4521-862b-8bb1ba950c4f）
- **Evidence**:
  - 独立测试：currencies 66/66 OK；net_worth+dca+monthly/yearly_overview+mini_tools+insights+test_calculations+api 共 36/36 OK；transactions 全量 21 errors+1 failure 与既有环境基线逐条一致、零汇率相关失败。
  - 真实 Postgres 演练：空库全量 migrate 与 0026 反向/重前向成功；播种旧数据 apply 0027 后旧行 1:1 转观测、基线 original_balance=100/pre_converted=120、服务指针匹配、旧表保留；rate_baseline_verify 全 MATCH、exit 0；verify_baseline 非零余额+基线缺失分支实证仍判失败。
  - I-1~I-5 修复逐条核实为真实有效；R1 的 11 条 advisory 无升级，另记录 2 条新 advisory（A-11 策略变更不入缓存键、A-12 convert.py 死代码/DCA 模板 date 过滤），均不阻塞。
- **Conclusion**: 所有 rule checkpoint 通过、三项 rubric 均 4/5（≥阈值）、每个 AC 有独立证据、无 actionable 发现。工作流结束。
