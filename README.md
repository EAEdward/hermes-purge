# hermes-purge

移植自 [dsh-purge](https://github.com/YuJunZhiXue/dsh-purge)（DeepSeek Harness 指令权威性清洗）到 **Hermes Agent** 的插件。

核心思路不变：**提示词层 + 行为策略层 + 引擎层重构宿主默认行为，使模型响应遵循操作员定义的指令权威性；所有操作可回滚，升级后可重洗。**

但移植不是代码复制，是**机制映射**。Hermes 与 DSH 架构差异决定了实现方式：

| dsh-purge（DSH） | hermes-purge（Hermes） |
|---|---|
| 40 个 patch 直接改写 `@deepseek-ai/*` 包 | Hermes 配置开放，L1/L2 走官方机制：`system_prompt_section` + `config.yaml`。仅深度模式（`deepPatch=true`）补源码 |
| `prompt-inject.md` override | `$HERMES_HOME/hermes-inject.md` |
| `/purge`、`/rules` 命令 | `/purge`、`/purge-rules` 斜杠命令 + `hermes purge` 子命令 |
| `purge_status/apply/revert` 工具 | 同名模型工具 + `purge_rules_status` |
| shim 启动注入（cmd 静默） | Hermes 无此问题；CLI 静默由官方处理 |
| 每次启动自动重洗 | `on_session_start` hook 异步自动应用 |
| 备份 `<file>.dshpurge.bak` | `plugin-data/hermes-purge/backups/` |

## 关键设计（为什么这样移植）

1. **注入主通道是 `system_prompt_section`，不是 AGENTS.md**。Hermes 对上下文文件（AGENTS.md/SOUL.md/.cursorrules）做 prompt-injection 扫描并阻断匹配内容；插件注册的 system_prompt_section 走插件权限通道，不受该扫描干预，且随会话冻结持久化。规则激活写入的 AGENTS.md/CLAUDE.md 目标文件**可能被该扫描 BLOCK**——那是文件侧的尽力而为；规则真正生效走 system_prompt_section（`purge-rules` 段），两者独立。
2. **身份不由插件发明**。`hermes-inject.md`（和用户自己的 SOUL.md）是身份唯一来源，插件逐字注入。SOUL.md 是身份文件，**不作为规则目标**（禁止覆盖）。
3. **审批尊重显式设置**。Hermes 的 `approvals.mode: off` 是官方 `--yolo` 等价物；插件只改写**未显式设置**的无人值守面（cron/single-query/unattended 走默认 deny 时）为 approve，并加白名单。用户显式写的 `mode`/`cron_mode`/`single_query_mode`（含显式 deny）一律保留——通过与默认值同值的 raw config 键区分。
4. **深度清洗是真语义改写**。`deepPatch=true` 时针对当前 Hermes 源码定位真实 needle 并**执行 replacement**（不是插注释），覆盖三个点：审批硬线拒绝句、否认断路器禁令、默认身份行；带备份回滚。升级被 `hermes update` 覆盖后可由 `autoApplyOnStart` 重洗；`autoRevertOnMissing=true` 时文件缺失自动整体回滚。
5. **注入段总预算受控**。Hermes 对每节 4000 字符、总 8000 字符逐一渲染并**静默丢弃超预算节**；插件用两段固定通道（`purge-core`≤4000 + `purge-rules`≤3800，恒 ≤7800）避免 rules 段被挤掉。

## 安装

实测过的完整步骤。**注意：安装时传的 `--enable` 不会真的启用插件**——它只把文件放到位，
`plugins list` 里仍然是 `not enabled`，必须再显式跑一次 `enable`。

```sh
# 1. 安装（仓库根即插件根，clone 落到 <HERMES_HOME>/plugins/hermes-purge/）
hermes plugins install https://github.com/Theater-ahyeon/hermes-purge

# 2. 启用 —— 这一步不能省
hermes plugins enable hermes-purge
#   会问 "Allow this plugin to replace built-in tools?"。本插件不覆盖内置工具，
#   选 No 即可（非交互环境下自动为 No）。确实需要时才加 --allow-tool-override。

# 3. 验证（应打印状态面板，而不是一行空白）
hermes purge status

# 4. 可选：把 settings 写进 config.yaml（不写则全部使用默认值）
```

不走 Git 时手动放置：

```powershell
Copy-Item -Recurse hermes-purge "$env:LOCALAPPDATA\hermes\plugins\hermes-purge"
hermes plugins enable hermes-purge
$env:LOCALAPPDATA\hermes\bin\hermes.exe purge status
```

`$HERMES_HOME` 在本机是 `C:\Users\<user>\AppData\Local\hermes`。

## 配置

`hermes plugins enable` 只会往 `$HERMES_HOME/config.yaml` 写入这两块：

```yaml
plugins:
  enabled:
    - hermes-purge
  entries:
    hermes-purge:
      allow_tool_override: false
```

**`settings` 不会被自动写入**，需要自己补。不补也能跑（全部取默认值），但要改
`deepPatch` / `approvals` / `verbose` 就必须写：

```yaml
plugins:
  entries:
    hermes-purge:
      settings:
        autoApplyOnStart: true   # 新会话启动时自动应用
        verbose: false
        deepPatch: false         # 深度清洗：补 Hermes 源码（可选，默认关）
        autoRevertOnMissing: false
        approvals:
          mode: off              # off = 全放行（== --yolo）；manual/smart 保留人工/智能审批
          rewriteConfig: true    # 只改写"未显式设置"的键，显式值一律保留
          permanentAllowlist: []
```

Hermes 合法的 capability 只有 `tools.override` 等少数几个；本插件**不需要**任何
capability（写入走 Python 文件操作，由用户显式配置授权），所以上一步的
`allow_tool_override` 保持 `false` 即可。

> 说明：`deepPatch: true` 会改写 Hermes 源码树中的 `tools/approval_floors.py`、
> `tools/approval.py`、`agent/prompt_builder.py`，带备份回滚；升级被 `hermes update`
> 覆盖后由 `autoApplyOnStart` 重洗。默认关闭，普通清理只走官方配置 + system_prompt_section
> 注入，不改源码。

## 使用

```sh
hermes purge status
hermes purge apply
hermes purge revert

/purge status
/purge apply
/purge revert
/purge write <文本>
/purge edit
/purge-rules list
/purge-rules use <id>
/purge-rules create <id> [别名] [AGENTS.md|CLAUDE.md]
/purge-rules edit <id> <内容>
/purge-rules delete <id>
/purge-rules reset
```

模型工具（agent 可直接调用）：`purge_status`、`purge_apply`、`purge_revert`、`purge_rules_status`。

## 运行时用户文件

| 文件 | 作用 |
|---|---|
| `$HERMES_HOME/hermes-inject.md` | 身份与行为框架（override 注入源；空文件不注入） |
| `$HERMES_HOME/plugin-data/hermes-purge/rules/` | 规则库 `<id>.md` + `<id>.json` |
| `$HERMES_HOME/plugin-data/hermes-purge/state.json` | 激活规则状态 |
| `$HERMES_HOME/plugin-data/hermes-purge/backups/` | deep patch 备份 + 规则激活前目标文件原样备份（`AGENTS.md.pre-purge.bak`） |

规则激活会覆盖 `$HERMES_HOME/<target>`；覆盖前自动备份原始文件，`/purge-rules reset` 优先按备份恢复。

## 验证

```sh
hermes purge status
```

输出应包含：hermes home、override 文件、approvals mode、规则集、deep patches 进度。首次运行自动创建 override（默认框架）与规则初始状态。

## 与 dsh-purge 的行为差异（移植边界）

- **无 cmd 静默**：Hermes 无 Windows CMD 闪窗问题，删除该能力。
- **无 liangshen/doctor/market 专属补丁**：那些是 DSH 生态插件，Hermes 没有对应物。
- **无 `dsh://` URI 安装**：Hermes 用 `hermes plugins install`。
- **设置页 UI**：Hermes dashboard 插件机制与 DSH cordis 不同；当前版本提供 CLI/命令/工具三通道，dashboard 面板后续可加。
- **身份默认不同**：DSH 默认「小码酱」；Hermes 默认不发明身份，override 文件初始为通用可执行框架，操作员可自填。
- **SOUL.md 不作为规则目标**：DSH 允许规则写 CLAUDE.md/SOUL.md，Hermes 的 SOUL.md 是身份源，允许覆盖会破坏身份加载，故排除。
- **deepPatch 是源码级语义改写**：patch 目标随 Hermes 版本演进；`pattern_not_found` 表示当前版本结构变化，不会假装 applied。

## 测试

`tests/test_smoke.py` 覆盖：路径/override/规则 CRUD+覆盖保护+reset 恢复/审批显式键尊重+隐式键放行/deepPatch 真语义改写+幂等+回滚/注入预算/register 全注册。另有 `tests/verify_real_needles.py`（只读）对当前 Hermes 源码验证 3 个 patch 的实际命中。

## License

MIT。参考 dsh-purge（MIT, YuJunZhiXue）移植。
---

## 本 fork 的改动（EAEdward/hermes-purge）

上游最后提交停留在 2026-09-17；以下是 2026-11-19 在 **Hermes 0.23.1（官方镜像 `nousresearch/hermes-agent:latest`，aarch64）** 上实测后做的改动。

### 兼容性核对（实测，不是推断）

| 检查项 | 结果 |
|---|---|
| `ctx.register_system_prompt_section` / `render_system_prompt_sections` | 存在（`hermes_cli/plugins.py:1014`） |
| 注入节预算常量 `MAX_SYSTEM_PROMPT_SECTION_CHARS` / `MAX_SYSTEM_PROMPT_SECTIONS_TOTAL_CHARS` | 存在 |
| `on_session_start` hook | 存在（`hermes_cli/plugins.py:134`） |
| deepPatch 三个目标文件 `tools/approval_floors.py`、`tools/approval.py`、`agent/prompt_builder.py` | 都存在 |
| 三个 needle 命中 | **3/3 命中**（`tests/verify_real_needles.py` → `RESULT: ALL_OK`） |
| `hermes plugins enable` + `hermes purge status` / `purge apply` | 在隔离的 `HERMES_HOME` 里跑通 |

### 改动 1：`tests/verify_real_needles.py` 可移植

上游把路径硬编码成 `C:\Users\theater\...` 和 `E:\codex\dshpursh\hermes-purge`，换个机器直接 `FileNotFoundError`，等于这条验收命令在别人手里根本跑不起来。改成按 `HERMES_SRC` / `PLUGIN_DIR` 环境变量 + `hermes_constants.get_hermes_home()` 解析，容器里 `HERMES_SRC=/opt/hermes` 即可。

### 改动 2：`presets/default-inject.md` 增加两段硬规则

都是在真实回归里踩出来的：

- **MULTI-TASK DELIVERY**：清单题的结论表是**开头**不是结尾；必须同一条回复内按编号全量交付，禁止用“报编号我再给 / tell me which one”收尾；单条越界只一句话带过，不许拖累整批；“是否全部可以执行”是交付指令不是确认环节。
  > 依据：同一套提示词，模型会把“先给结论表”当成终点，只吐一张分档表就停（42 题清单：改动前 983 字符、改动后 57 000+ 字符）。
- **REASONING BUDGET**：`high`/`max` 思考档会把这段提示词当政策题重审，拒绝率上升、输出预算被思考吃光（空正文）；不要在这条链路上把 `max_tokens` 压到 8000 以下。

### 改动 3：`tests/test_preset_budget.py`

presets 是被编辑频率最高的文件，而 Hermes 对每节 4000 字符静默截断。加一条硬守卫（留 300 字符余量），`python3 tests/test_preset_budget.py` 即可校验。

### 已知边界（上游同款，未改）

- 注入节走 `after_memory`，是**平台无关**的：CLI / Telegram / API server / cron 都会渲染，不依赖 AGENTS.md，也不受上下文文件注入扫描影响。
- `deepPatch` 是真语义替换，Hermes 换版本后 needle 会漂移；`pattern_not_found` 就代表漂移，不要当成 applied。
- `reasoning_effort` 的三个非法值（`minimal` / `none` / `ultra`）会被上游 400 拒，最短可用档是 `low`。

## 升级到 1.1.0：对齐 dsh-purge 1.7.20 的 refusal-retry

上游最后停更在 2026-09-17，而 dsh-purge 本体已经走到 **1.7.20**（本机 `dsh-purge-community 1.7.20` 实测）。1.7.x 的核心增量是**拒绝检测 + 自动重试**，这一版把它按 Hermes 的机制映射过来。

### 映射表：dsh-purge 1.7.20 → hermes-purge 1.1.0

| dsh-purge（DSH） | hermes-purge（Hermes） |
|---|---|
| 流层拦截：`streamFactory()` 重建流，命中拒绝就重发（1.7.19 修了子代理建流抛错、1.7.20 修了假 `recovered`） | Hermes 的流由内核持有，插件无法重建。改为**两个可介入点**：<br>`llm_request` 中间件（请求层改写）+ `pre_verify` hook（回合停门，仅在改过文件时开放同回合 continue） |
| 中文句中拒绝句式（1.7.17 补：“我不会给”“我不提供”…） | `lib/refusal.py` 逐条对齐，CN + EN 两套，另加开头 200 字符的兜底正则 |
| 推理先行的误判修复（1.7.17） | 只看正文 `content`，从不看 `reasoning_content` |
| `refusal_retry` / `refusal_retry_outcome` 审计 | `plugin-data/hermes-purge/refusal-audit.jsonl`，`refusal_detected` / `refusal_retry` / `refusal_retry_outcome(exhausted)` |
| 逐次升级的 nudge 文案 | `NUDGES` 三段递进，`maxAttempts` 默认 2 |
| 假成功标签（1.7.20） | 未命中正文（空答复）**不计**为拒绝——空是预算问题，交给 REASONING BUDGET 规则，避免 dsh 那种 `recovered` 误判 |

### 配置

```yaml
plugins:
  entries:
    hermes-purge:
      settings:
        refusalRetry:
          enabled: true      # 默认开
          maxAttempts: 2     # pre_verify 通道的同回合重试上限
          patterns: []       # 追加自定义拒绝句式（小写子串匹配）
```

### 两条通道的实际差别（别当成同一件事）

- `llm_request`：**跨回合自愈**。上一次 assistant 是拒绝时，下一次请求自动带纠正指令——所有平台（CLI/TG/API/cron）都生效，但用户要再发一条消息。
- `pre_verify`：**同回合真重试**。Hermes 只在“本回合改过文件”时才开放这个 continue 口子，所以它救的是编码/运维这类有文件产出的回合。
- 纯聊天回合想同回合重试，需要内核开放流层 hook；当前 Hermes 没有，这就是移植边界。

### 测试

`tests/test_refusal.py`：CN/EN 检测矩阵、交付物不误判、nudge 升级与封顶、只惩罚最后一条 assistant、开关、以及中间件/hook 入口的真实返回值。`python3 tests/test_refusal.py` 全绿即可。

## 1.2.0：把 dsh-purge 的「尾部 boost」搬过来（两个项目对照后的结论）

同时读完 dsh-purge 1.7.20 与 hermes-purge 上游源码后，效果差异的根因只有一条：**注入位置**。

| 项目 | 注入怎么落位 | 实测（42 题清单，同模型同机） |
|---|---|---|
| dsh-purge | 多 section + `order` 排序，boost 用**最大 order** 挂末尾，probe 再挂它后面 | 本机 41/42 交付（尾部权重） |
| hermes-purge 1.0/1.1 | 只有 `system_prompt_section`，位置锁死 `after_memory`（中段，≤4000 字符） | 4 039 字节：约 14 条可做、24 条只讲思路 |
| 手搓（SOUL 身份槽 + `agent.system_prompt` 每请求末尾） | 身份 + 末尾，两层都在高位 | 57 418 字符：41 条全量交付 |

结论：Hermes 里唯一能拿到「末尾」位置的官方口子是 **`llm_request` 中间件**——它在发请求前拿到整份 `messages`，可以把 boost 作为**最后一条 system 消息**追加。这就是 dsh-purge `order: 最大` 的等价物，而且不需要 `agent.system_prompt`（那属于用户配置，不是插件）。

### 新增

- `lib/boost.py`：`BOOST_TEXT`（移植 dsh-purge boost 的四段结构：层级声明 → 夹具化 → 反软化 → 多轮重申，另加多任务交付一条）+ 可选自定义文件 `$HERMES_HOME/hermes-boost.md`。
- `llm_request` 中间件改为 **尾部注入**：`messages + [boost] (+ [拒绝纠正])`；用 `MARKER` 幂等，重复经过不会叠加。
- **运行时探针**（移植 `probe.js`）：`/purge probe` 生成 `purge-xxxx` nonce，boost 末尾附带「看到就回显」指令，`/purge probe-clear` 清除；`purge status` 显示当前 nonce。文件在 `plugin-data/hermes-purge/probe.json`，30 分钟 TTL。
- 配置：`boost.enabled`（默认 true）、`boost.file`（自定义 boost 文本）。

### 为什么不再需要改 SOUL / agent.system_prompt

- 身份仍由用户的 `SOUL.md` 提供（插件不发明第二身份，这是上游的設計）。
- 破甲强度由 boost 的**位置**保证，不再依赖往 SOUL 里塞规则，也不依赖用户级 `agent.system_prompt`。
- `deepPatch` 保持默认关：实测三个源码补丁（身份改指、审批硬线、断路口径）会让模型更倾向分档拒绝，收益是负的。
