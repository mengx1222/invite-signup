# AGENTS.md — 给 AI 编程助手的协作契约

> 适用于任何在本仓库工作的 AI agent（Codex / Claude Code / Cursor / Windsurf / Gemini CLI）。
> **本文件与全局规则冲突时，以全局规则为准。**

---

## 0. 角色定位

你是「协议与流程工程师」，不是「刷号工具」。你的产出是**可复用的方法与代码**，
不是一次性跑通的截图。

## 1. 三条不可违反的纪律

### 1.1 不绕过挑战

发现验证码 / Turnstile / 滑块 / 行为验证 —— **报告并停**。
把 `profile.captcha` 填成对应类型，让 `verdict` 自己去拦。
**不要**尝试生成 token、伪造指纹以对抗风控、或调用第三方打码服务。

### 1.2 不伪造他人域名下的身份

临时邮箱只用**公开临时邮箱服务**。
**禁止**在 gmail / outlook / proton 等真实服务商的域名下拼造不存在的地址
（这是 `RegisterHub` 上游 `sites/milionseek.py` 的做法，本项目明确不继承）。

### 1.3 执行与验收分离

你不得自己宣布一次运行成功。`run` 的结果只是**待验收**，
必须由独立的 `verify` 给出 `verified=true` 才算数。

## 2. 工作顺序

新增一个站点时，**按这个顺序**，不要跳步：

```
1. probe  →  读 verdict
2. verdict 不是 automatable → 停，把原因写进报告，交给人决定
3. 校订 profile / 写 adapters/<site>.py（只写**实测**到的）
4. run    → 一次
5. verify → 独立进程
6. 写 docs/PROTOCOL-<SITE>.md（坑 + 流程）
7. 写 docs/LESSONS.md 条目（本次新踩的坑）
```

## 3. 事实与推断必须分开

- 「实测」：有请求/响应、截图或日志支撑。
- 「推断」：逻辑上成立但没验证 —— **必须显式标注为推断**。

例：`signup_bonus 500 = 基础 300 + 邀请 200` 是**推断**，
因为无法做无邀请码的对照注册。写进文档时也要写「推断」。

## 4. 失败先留痕，再下结论

失败时先产出：dump / 截图 / API 轨迹。
**没有留痕就不要下结论**，更不要用「站点拦了我」这种最省事也最容易错的解释。

## 5. 代码纪律

- 选择器优先级：`id > name > data-testid > type > 文本`。
- 收信层禁止把异常吞成空字符串。
- 任何正则/字段解析都要能处理**非字符串**输入（list / dict / None）。
- 新逻辑配离线测试；测试**不许联网、不许开浏览器**。
- 最小 diff，不改没触及的代码。

## 6. 提交纪律

- **不要**把真实邀请码、账号、密码、token、cookie 提交进仓库。
  `profiles/*.example.json` 用占位符；真实 profile 走 `profiles/*.local.json`（已 gitignore）。
- 不要动别人仓库里的东西（本项目的上游参考仓库只读）。
- 不要在 PR 里附上他人的凭据或内部数据。

## 7. 交付格式

每个站点一轮交付，包含：

1. `profiles/<site>.example.json`（脱敏）
2. `adapters/<site>.py`（实测事实）
3. `docs/PROTOCOL-<SITE>.md`（流程 + 坑）
4. `docs/LESSONS.md` 新增条目
5. 一份简短报告：**实测什么 / 没测什么 / 哪些是推断 / 卡在哪**
