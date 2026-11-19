# invite-signup

> **邀请制站点注册流水线的标准作业流程（SOP）与实现。**
> 探站 → 判定 → 生成 → 执行 → 独立验收。站点无关，一次搭好，之后每个邀请制平台只填一份 profile。

[![tests](https://img.shields.io/badge/tests-208%20passed-brightgreen)](https://github.com/mengx1222/invite-signup)
[![python](https://img.shields.io/badge/python-3.10%2B-3776ab)](https://www.python.org/)
[![license](https://img.shields.io/badge/license-MIT-22272e)](LICENSE)

---

## 0. 它解决什么问题

邀请制平台（给一个邀请链接，送额度或送会员）的注册流程，几乎每个站点都是同一套骨架：

```
邀请链接落地  →  归因（cookie / query）  →  邮箱  →  密码
             →  邮箱验证（链接式 or 码式）  →  建号成功  →  查额度
```

**难的不是写脚本，是搞清楚这个站怎么注册**：邀请码怎么带、注册入口在哪、有没有验证码、
验证是发链接还是发数字码、字段选择器长什么样。

这个仓库把「搞清楚」这件事做成了**可复用的探站器**，把「跑」做成 **profile 驱动的执行器**，
把「跑通了吗」做成**独立进程验收**。站点之间的差异，全部沉淀在一份 `profile` JSON 里。

---

## 1. 和两个参考仓库的关系

本项目**借鉴**了以下两个公开项目的工程思路，但**定位不同**、**代码独立编写**：

| 参考项目 | 它的强项 | 本项目的差异 |
+| :--- | :--- | :--- |
| [duwqijlk/muse-protocol](https://github.com/duwqijlk/muse-protocol) | 纯 HTTP 协议化、**「几个容易踩的坑」**式文档、批量调度的停批语义、离线冒烟测试 | 它**绑定 muse.ai 一个站点**；本项目做**站点无关的通用流水线**，站点差异由探站器自动消化 |
| [xyzww888/muse-JP-Reg](https://github.com/xyzww888/muse-JP-Reg) | WebUI 控制台、代理池、**多接码源可插拔**、邀请码裂变池 | 它是**同一协议的第二代工程化**；本项目先不做 WebUI，聚焦「探站 + 判定 + 验收」这条内核 |

具体借鉴并落地的三件事：

1. **协议笔记要写"坑"，不只写流程** —— 见 [`docs/LESSONS.md`](docs/LESSONS.md) 与 [`docs/PROTOCOL-YILA.md`](docs/PROTOCOL-YILA.md)。
2. **失败语义要分级**：传输层失败可以重试，**业务拒绝必须整批停**，不换号、不补号。
3. **离线冒烟测试**：核心逻辑必须能在不联网、不开浏览器的情况下被验证。

### 2026-11-17 第二轮调研：又看了一批同类项目

| 项目 | 星数 | 值得学的 | 落到本项目哪里 |
| :--- | :--- | :--- | :--- |
| [lxf746/any-auto-register](https://github.com/lxf746/any-auto-register) | 3.4k | **`before_ids` 邮件基线**（请求前先抓一次消息 ID，之后只认新的）；**provider 注册表**（`@register_provider` 装饰器 + `from_config` 工厂 + 启动扫描）；**失败分级异常类** | `mailbox.current_ids()` + `poll(before_ids=...)`；`failures.py` 的 `Kind` / `Action` |
| [techinz/playwright-captcha](https://github.com/techinz/playwright-captcha) | 365 | 验证码做成**可插拔 provider**，click 模式与 2captcha API 两种驱动 | **未采纳**：本项目遇到验证码直接停，不引入打码服务 |

**没有采纳的**：`any-auto-register` 的 DDD 四层（`domain/` `application/` `infrastructure/` `api/`）和 Electron 桌面端。
本项目是 CLI 工具 + 库，硬套分层只会增加跳转成本；等真有 WebUI 需求再考虑。

---

## 2. 安装

```bash
git clone https://github.com/mengx1222/invite-signup.git
cd invite-signup
pip install -r requirements.txt
playwright install chromium        # 或使用本机已安装的 Chrome
```

Python 3.10+。

---

## 3. 快速开始（三条命令）

> 完整用法（含每种失败怎么处理、换新站点要做什么）见 [`docs/USAGE.md`](docs/USAGE.md)。
> 不想敲命令的话，`python -m invitesignup web` 会起一个本机界面（`http://127.0.0.1:8765`）。

### 3.1 探站 —— 搞清楚这个站怎么注册

```bash
python -m invitesignup probe "https://example.com/invite/CODE" --out profiles/example.json
```

输出一份 `profile` JSON 和一条 `verdict`：

```json
{
  "invite_valid": true,
  "reward_credits": 200,
  "attribution": { "kind": "cookie", "name": "example_invite_code" },
  "captcha": "none",
  "verification": { "mode": "link", "sample": "/api/auth/verify-email?token=" },
  "steps": ["email", "password"],
  "selectors": { "email": "input[type=email]", "password": "#signup-password" },
  "endpoints": { "session": "/api/auth/get-session", "credits": "/api/credits" },
  "quota": { "max_successful_invites": 4 }
}
```

探站过程里唯一的写操作是**一次「邮箱是否已注册」的存在性检查**，它**不建号**。

### 3.2 执行 —— 用邀请码建一个号

```bash
python -m invitesignup run --profile profiles/example.json
```

### 3.3 验收 —— 从干净会话证明账号真的存在

```bash
python -m invitesignup verify --profile profiles/example.json \
    --email <email> --password <password>
```

**执行者不能自己宣布成功。** `run` 说成功不算，`verify` 说了算。

---

## 4. 流水线

```
   ① probe                     ② verdict            ③ profile          ④ run         ⑤ verify
 ┌──────────────┐        ┌──────────────┐     ┌──────────────┐   ┌──────────┐   ┌──────────┐
 │ 抓邀请 payload│        │ 有验证码？    │     │ 端点 / 选择器 │   │ 填表提交  │   │ 重新登录  │
 │ 扫 chunk 提端点│───────▶│ 条款禁自动化？ │────▶│ 验证模式     │──▶│ 收信取链接│──▶│ 查额度    │
 │ 探公开 config │        │ 事实齐不齐？  │     │ 配额 / 归因   │   │ 建会话    │   │ 出结论    │
 │ DOM 探针      │        │ 能否全自动？  │     │              │   │ 落台账    │   │          │
 └──────────────┘        └──────────────┘     └──────────────┘   └──────────┘   └──────────┘
        只读                     纯函数              可提交             可执行          独立进程
```

完整方法论见 [`docs/SOP.md`](docs/SOP.md)。

---

## 5. 安全边界（本项目**主动不做**什么）

这不是免责声明，是**代码里的硬分支**：

| 情形 | 行为 | 代码位置 |
| :--- | :--- | :--- |
| 页面存在 CAPTCHA / Turnstile / 滑块 | **停**，报告，不尝试绕过 | `verdict.decide()` → `blocked-by-captcha` |
| 站点条款禁止自动化（由人显式声明） | **停** | `verdict.decide()` → `blocked-by-tos` |
| 关键事实探不出来（选择器 / 验证模式） | **停**，不猜 | `verdict.decide()` → `blocked-missing-facts` |
| 临时邮箱被拒 | 停并留证，转「自备邮箱 + 人工贴一次链接」 | `runner.py` |
| 邀请码配额用尽 | **停**（除非显式 `--allow-multi`） | `runner.py` |
| 同一个邀请码当天重复跑 | **跳过**（除非 `--force`） | `ledger.py` |

另外两条硬规矩：

- **不使用真实邮箱服务商的域名拼造不存在的地址。** 一次性邮箱只来自公开临时邮箱服务；
  这条是 `RegisterHub` 上游 `sites/milionseek.py` 的做法，本项目**不继承**。
- **可以用自己的邮箱收信，且不用人工介入。** `--email` + `--inbox-password`（IMAP 授权码）
  让工具自己轮询收件箱取验证链接；先跑 `inbox-check` 验证凭据，别等建号时才发现读不了。
- **临时收件箱要能重开。** 建号用的邮箱凭据随账号一起存在 `accounts.jsonl` 的
  `inbox` 字段，`python -m invitesignup inbox --email <地址>` 可以重新打开，
  这样站点以后发密码重置还读得到。只存地址不存密码等于没存。
- **凭据不提交进仓库。** 账号落在本地 `output/accounts.jsonl`（已 gitignore），
  台账 `output/ledger.jsonl` 会自动把 `password` / `token` 之类的字段脱敏。

---

## 6. 目录结构

```
invite-signup/
├── invitesignup/
│   ├── probe.py       探站器：抓 payload / 扫 chunk / 读 config / DOM 探针
│   ├── profile.py     SiteProfile 数据模型（站点差异的唯一存放处）
│   ├── verdict.py     判定：能不能自动、要人配合哪一步、还是该停
│   ├── runner.py      执行器：填表 → 提交 → 收信 → 建会话 → 落台账
│   ├── verify.py      独立验收器：另起进程重新登录 + 查状态
│   ├── mailbox.py     收信层：类型摊平、链接/码抽取、失败留痕
│   ├── ledger.py      append-only 台账 + 去重键 + 脱敏
│   ├── failures.py    失败分类：能不能重试由阶段决定
│   ├── human.py       拟人输入/点击（不碰任何挑战）
│   ├── flow.py        一条龙：一个链接 → 探站 → 建号 → 独立验收
│   ├── progress.py    进度百分比 / 步骤清单 / 稳定错误码
│   ├── web.py         本机 Web 界面（Flask + SSE，只绑 127.0.0.1）
│   └── cli.py         命令行入口
├── adapters/          各站点的已验证事实（第一个样本：yila.py）
├── profiles/          profile JSON（示例：yila.example.json）
├── docs/              SOP / 踩坑 / 每站协议笔记 / 外部评审核实记录
├── examples/          可直接运行的示例
└── tests/             208 个离线测试
```

---

## 6.5 外部评审怎么被处理的

四份 AI 评审逐条对照代码核实过，不是照单全收：
[`docs/REVIEW-2026-11-19.md`](docs/REVIEW-2026-11-19.md)。

那份文档同时记录了**不成立**的建议和理由——比如"runner 缺少 verdict 强制校验"其实是误判，
门禁一直在 `runner.py` 入口；"引入 SQLite"在单任务模型下没有收益。
采纳与拒绝都写明依据，比单方面接受更有用。

---

## 7. 已验证站点

| 站点 | 邀请入口 | 验证码 | 验证方式 | 状态 |
| :--- | :--- | :--- | :--- | :--- |
| `yila.ai` | `/en/invite/<code>`（服务端下发 `yila_invite_code` cookie） | **无** | 邮件链接 | ✅ 全链路实跑通过（2026-11-17，27.9 s） |
| `automationexercise.com` | 无（**不是邀请制**，公开测试站） | **无** | 无 | ✅ 仅验证探站（2026-11-18）——它暴露了三个探站误判，见 [`docs/PROTOCOL-AUTOMATIONEXERCISE.md`](docs/PROTOCOL-AUTOMATIONEXERCISE.md) |

> 第二个站点只跑了 `probe`，没有跑 `run` / `verify`：它不是邀请制，没有邀请码可走完整链路。
> 它的价值是**压测探站器的假设**，不是"又多支持一个站"。code 型验证和 Turnstile 站点仍然只有
> "检测到就停"的路径，没有跑通的样本。

其余站点请先跑 `probe`，看 `verdict` 给不给过。

---

## 8. 测试

```bash
python -m pytest tests/ -q          # 208 passed
python tests/test_mailbox.py         # 也可以单文件离线跑，不需要 pytest
```

所有测试**不联网、不开浏览器**。

---

## 9. 免责声明

本项目仅供学习、协议研究与对**自己有权访问**的系统的自动化走查使用。
使用者须自行遵守目标站点的服务条款与所在地法律法规。

仓库内不包含任何真实账号、凭据、令牌或有效邀请码；示例中的地址与邀请码均为占位符。
