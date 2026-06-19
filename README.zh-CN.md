# AutoDevLoop

> 设定一个目标，让 AI 编码 agent 自动完成架构设计、开发、测试、评审，并持续进化你的项目 —— 一个版本接一个版本 —— 直到达到你设定的版本数。

[English](README.md) · **简体中文**

AutoDevLoop 是一个零依赖的小型 Python 工具。它驱动一个命令行编码 agent
（默认 Claude Code，也支持 Codex / Gemini CLI）按照一套**标准化的多阶段流程**
不断迭代开发。你只需给出目标和版本数量；它会自动设计架构、规划每个版本、编写代码、
运行测试、评审结果；当目标达成后，它会自己**评估并筛选有价值的新功能**继续完善产品。

每个版本都是可用的、独立留存的构建。一份一目了然的 `FEATURES.md` 表格记录了每个版本
实现了什么、相比上个版本新增/改进了什么。

---

## 核心特性

- **目标驱动的两阶段循环。** **build（攻坚）阶段**径直冲向你的目标；当一个独立的检查
  判定目标真正达成后，自动切换到 **expand（扩展）阶段**，开始开发有价值的周边功能。
- **新功能的价值闸门。** 在扩展阶段，一个 agent 负责发掘点子，另一个**独立的** agent
  对每个点子的价值/成本打分 —— 只有被接受的点子才进入待办清单并被开发。不会无脑堆功能。
- **流程标准、细节动态。** 主流程（架构 → 计划 → 开发 → 测试 → 评审 → 修复 → 发掘 →
  评估）是固定、可预测的；但由计划 agent 动态决定开多少个开发 agent、各自负责什么，
  prompt 也给模型留足发挥空间。所有 prompt 都是**可编辑的模板文件**，不是写死的字符串。
- **简单模式 vs 高级模式。** `simple` 只跑省 token 的核心循环（计划 → 开发 → 测试 →
  评审）；`advanced` 额外加入目标检查、测试规划 agent、文档、功能发掘和价值闸门。
  每个步骤都可单独开关。
- **只编辑一个文件夹，留存多个快照。** agent 永远只编辑 `current/`；每完成一个版本就
  复制一份到 `versions/vN/`。若本地有 git，每个版本还会自动 commit 并打 tag；首次达成
  目标的那个版本会被打上特殊的 `goal-complete` 标签。
- **成本与 token 统计。** 每次调用的花费和 token 都会被记录并实时展示。
- **本地网页面板。** 在浏览器里新建项目、实时查看进度（当前版本、当前 agent、当前步骤、
  成本、每个 agent 的输出文本）、阅读 changelog 和功能表、修改配置与 prompt —— 全部内置，
  无需构建、无需依赖。
- **健壮可靠。** provider 的输入输出走独立线程（杜绝 stdin/stdout 死锁）、瞬时失败按
  指数退避重试、状态文件原子写入、单个版本出错时自动回滚工作目录。

---

## 环境要求

- **Python 3.10+**
- **本地已安装并登录好的编码 agent CLI**，三选一：
  - [Claude Code](https://docs.claude.com/en/docs/claude-code) —— `claude`（默认）
  - Codex CLI —— `codex`
  - Gemini CLI —— `gemini`

AutoDevLoop **永远不会向你索要 API key**。你需要提前把所选 CLI 配置好（登录/接好第三方
API 均可）；切换 provider 只是切换被调用的命令而已。比如你用 `claude` CLI 接了第三方 API，
本工具直接调用 `claude` 即可。

工具本身无任何 Python 运行依赖。`PyYAML` 为可选项（内置了 YAML 兜底解析器）。

---

## 安装

```bash
# 在项目根目录
pip install -e .
# 之后即可使用 `autodevloop` 命令
autodevloop --version
```

或者不安装直接运行：

```bash
python -m autodevloop --help
# 或使用向后兼容的入口：
python autodev.py --help
```

---

## 快速开始

### 命令行

```bash
# 交互式（会依次询问目录、目标、版本数、模式）：
autodevloop run

# 非交互式：
autodevloop run --project-dir ./my-app \
  --goal "做一个类似微信的应用：实时聊天 + 朋友圈" \
  --max-versions 8 --mode advanced

# 先头脑风暴梳理设计（交互式问答，之后再开跑）：
autodevloop run --project-dir ./my-app --goal "一个 todo 命令行工具" --brainstorm
```

**头脑风暴模式**（`--brainstorm`）：在自动循环开始前，AI 会**每次只问一个问题**，
逐步厘清目的、范围、约束与验收标准，把粗略的想法打磨成一份达成共识的设计。问答记录
保存在 `.autodev/brainstorm.json`（中断不丢、续跑不会重复发问），最终设计写入
`docs/brainstorm-spec.md`，磨好的目标随后喂给本次运行。中途可输入 `/done` 提前结束、
`/skip` 取消。在 Web 面板创建项目时也提供「开关 + 聊天面板」。`--non-interactive`
运行会自动跳过。

查看 / 控制运行：

```bash
autodevloop status --project-dir ./my-app
autodevloop stop   --project-dir ./my-app     # 在当前步骤结束后优雅停止
```

### 网页面板

```bash
autodevloop web            # http://127.0.0.1:8787
autodevloop web --port 9000
```

界面支持 **English / 简体中文 / 日本語**（右上角 🌐 小图标切换）。内置**帮助**指南，
配置/agent/按钮旁都有 `?` 悬停说明，第一次用的人也能看懂每一项是干啥的。

在面板里你可以：

1. **新建项目** —— 目录、目标、版本数、模式、provider、架构提示。新建只会**创建**，不会立即运行；
   你可以先去设置里调整参数，准备好后再点 **运行**。
2. **实时观察** —— 状态、阶段、当前版本、**每个正在运行的 agent 各自的实时计时器**（并行时可同时显示多个）、
   agent 调用次数、token 用量、项目总运行时长、可滚动的活动日志（**版本之间有分隔线**），以及每个 agent 的完整输出（常驻查看器）。
3. **修改设置** —— 流程模式与各步骤开关、最大版本数、评审/价值阈值、重试次数、测试命令、
   provider 命令/模型，以及**每一个 prompt 模板**。必需 agent（计划、开发、测试、评审、修复）会展示但锁定为开启，
   只有可选步骤可以开关。prompt 改动会做**格式校验**——文字可用任意语言改写，但引擎依赖的 `{{占位符}}`
   和 JSON 字段名不能删除。**运行中设置为只读**，保存的设置在下次运行时生效。
4. **两种停止** —— *优雅停止*（跑完当前版本再停）或 *废弃停止*（立即中止、丢弃未完成的版本、
   并把工作目录回退到上一个已完成版本）。每种停止都会弹出确认，说明刚刚发生了什么。
5. **阅读文档** —— `FEATURES.md` 总览表和 `CHANGELOG.md`。

> 故意不显示金额成本（CLI 背后接第三方 API，价格不可靠）；面板改为展示 **agent 调用次数和 token**。

---

## 流程是怎么跑的

```
            ┌──────────────────────── 仅在最开始执行一次 ───────────────────────┐
            │  AgentARCH → 选定主流技术栈、目录结构、运行与测试策略               │
            └────────────────────────────────────────────────────────────────────┘
 每个版本：
   AgentPLAN ── 决定本版本目标，以及开几个开发 agent、各自负责哪些文件
       │
   AgentDEV_* ─ 一个或多个（并行）agent 在隔离工作区里实现，然后合并回 current/
       │        （只合并真正改动过的文件；冲突时先写者优先并告警）
   AgentDOC ── （高级模式）维护 README / 设计文档的准确性
       │
   AgentTEST ─ 运行测试：简单模式用内置检测，高级模式由 agent 决定测试命令
       │
   AgentREVIEW  打分、标记阻断性问题、判断目标完成度，并写出可读的"本版新增"摘要
       │
   （修复循环）─ 若测试失败/有阻断/低于阈值，AgentFIX 修复并重测
       │
   AgentGOALCHECK （高级模式）独立确认目标是否达成
       │
   ── 首次判定目标达成 → 切换到 EXPAND 阶段，打 goal-complete 标签 ──
       │
   AgentSCOUT + AgentEVALUATE （扩展阶段）发掘并价值筛选新功能，写入持久待办清单
       ▼
   快照 → versions/vN/，git commit + 打 tag vN，更新 CHANGELOG.md 和 FEATURES.md
```

循环**不会因为"已经够好了"而提前停止** —— 它会一直跑到你设定的 `max_versions`（或你手动停止）。
达成目标只会改变它"做什么"，不会改变它"是否继续做"。

---

## 产物结构

每个项目目录下会生成：

| 路径 | 含义 |
|---|---|
| `current/` | agent 唯一编辑的工作目录（启用 git 时是一个仓库） |
| `versions/vN/` | 每个已完成版本的完整快照 |
| `FEATURES.md` | 一览表：每个版本的功能 + 相比上版的变化 |
| `CHANGELOG.md` | 逐版本变更记录，含摘要与测试状态 |
| `.autodev/state.json` | 完整运行状态 |
| `.autodev/progress.json` | 实时进度 + 事件流（网页面板使用） |
| `.autodev/backlog.json` | 发掘到的功能及其"接受/拒绝"判定 |
| `.autodev/architecture.md` | 初始架构报告 |
| `.autodev/prompts/templates/` | 可编辑的 prompt 模板 |
| `.autodev/plans/`、`reviews/`、`tests/`、`logs/` | 各阶段产物 |
| `.autodev/final_report.md` | 运行结束时的总结报告 |

---

## 配置

配置位于项目目录下的 `.autodevloop.yml`（网页设置页和 CLI 参数都会写入它）。所有项都有合理默认值，
完整示例如下：

```yaml
project:
  name: My App
  max_versions: 8
  arch_hint: "React + FastAPI + SQLite"   # 可选，给 AgentARCH 的架构提示

provider:
  name: claude          # claude | codex | gemini
  command: ""           # 留空 = 使用该 provider 的默认命令（如 "claude"）
  model: ""             # 可选的模型别名/名称
  extra_args: []        # 每次调用都附加的额外 CLI 参数

pipeline:
  mode: advanced        # simple | advanced
  steps:                # 在模式默认值之上，单独覆盖某些步骤
    goal_check: true
    test_agent: true
    doc: true
    scout: true
    evaluate: true
    features_doc: true

agents:
  timeout: 1800         # 每次 provider 调用的超时（秒）
  allow_parallel: true
  max_parallel: 3
  retries: 3            # provider 瞬时失败的重试次数
  backoff_seconds: 5

review:
  threshold: 80         # 评审分低于此值触发修复

value:
  threshold: 65         # 功能价值低于此值会被闸门拒绝

fix:
  retries: 2

tests:
  timeout: 120
  command: ""           # 留空 = 自动检测 / 由 agent 决定

vcs:
  git: true             # 在 current/ 内为每个版本 commit + 打 tag
```

常用 CLI 参数：`--mode`、`--provider`、`--provider-command`、`--model`、
`--max-versions`、`--review-threshold`、`--fix-retries`、`--max-parallel-agents`、
`--no-parallel`、`--no-git`、`--test-command`、`--reset`、`--non-interactive`。

---

## ⚠️ 安全须知 —— 请务必阅读

AutoDevLoop 是一个**会在你机器上运行代码的自治代码生成器**。请把它当作任何会执行不可信代码的工具来对待：

- **它会编写并执行代码。** agent 拥有文件编辑权限，`AgentTEST` 会在你的项目目录里运行 shell
  测试/构建命令。生成的代码在运行前**没有经过人工审查**。
- **它无人值守地运行，并且会花钱。** 循环会持续调用你的 provider CLI，直到达到版本数或你手动停止。
  请关注实时成本读数、设置合理的 `--max-versions`，并留意你的 provider 账单。
- **请在隔离环境中运行。** 建议使用专门的目录、容器或虚拟机。不要把它指向包含密钥或重要无关文件的目录。
- **网页面板没有鉴权，且仅绑定 localhost。** 它能启动运行、执行命令。**切勿**把端口暴露到你不完全信任的网络。
  它没有任何鉴权层。
- **本工具不处理任何 API key** —— 凭据由你的 provider CLI 自行管理，AutoDevLoop 只调用你配置好的命令。

运行 AutoDevLoop 即代表你接受：你对它生成的代码和执行的命令负责。

---

## 常见问题

- **"Provider command not found"** —— 请安装对应 CLI 并确保它在 `PATH` 中，或把 `provider.command`
  设为完整路径 / 包装命令。
- **git 提交没出现** —— git 是可选的，工具会回退到文件夹快照。企业级 git 钩子拦截提交时会被静默容忍。
- **旧版 Windows 控制台乱码** —— 输出已强制 UTF-8；若终端仍有问题，请用 Windows Terminal 或设置
  `PYTHONIOENCODING=utf-8`。

---

## 许可证

[MIT](LICENSE)。欢迎贡献 —— 见 [CONTRIBUTING.md](CONTRIBUTING.md)。
