# SOP：邀请制站点注册流水线的标准作业流程

> 这份文档是本仓库的主交付物。代码是它的实现，文档是它的依据。
> 每一步都写清：**怎么做 / 得到什么 / 记到哪里 / 出错怎么办**。

---

## 第 0 步 · 确认授权与边界（不可跳过）

在做任何事之前确认三件事，缺一不可：

| # | 确认项 | 怎么确认 | 不满足时 |
| :--- | :--- | :--- | :--- |
| 0.1 | 邀请码是**你自己的**，或你有权使用 | 问一句；或看站点后台的 Invite 页 | **停**。不要替别人刷邀请 |
| 0.2 | 目标站点的条款**没有**禁止自动化 | 读 ToS；读不出来就当**禁止** | 传 `--tos-blocks`，流程会停 |
| 0.3 | 你打算**建几个**号 | 明确数字 | 默认 1 个；用完名额要显式点头 |

**为什么要单独列出来**：这类任务的默认失败模式不是技术失败，是"脚本跑得很顺，
但做的不是你以为的那件事"。第 0 步是唯一的拦截点。

---

## 第 1 步 · 探站（只读，唯一例外是那一次存在性检查）

目标：**不猜**，把站点的事实挖出来。8 个动作，全部有产出。

### 1.1 抓邀请落地页

```bash
curl.exe -sS -L "https://example.com/invite/CODE" -o landing.html
```

**挖什么**：邀请 payload。用 `probe.extract_invite_payload()` 从 RSC/Next.js 的
`self.__next_f.push([...])` 里解出 JSON。常见字段：

```json
{"valid": true, "referralCode": "CODE", "inviterName": "...",
 "rewardCredits": 200, "hasRewardSlots": true}
```

**记到**：`profile.invite_valid` / `invite_code` / `reward_credits` / `has_reward_slots`。

**注意**：payload 常嵌一层（如 `{"landingData": {...}}`），解析要**逐层下钻**，
不能只匹配顶层 key —— 这是本项目第一版真实踩的 bug。

### 1.2 确认归因载体

看落地页 `Set-Cookie` 里有没有 `*invite*` / `*referral*`，或 URL query 里有没有
`ref=` / `invite=` / `code=`。

**记到**：`profile.attribution.{kind,name,source}`。

**为什么重要**：只要从邀请链接进入且服务端下发归因 cookie，**归因就是自动的**，
后续不用再传邀请码。若归因载体是 query，则注册请求必须带上它。

### 1.3 扫前端 chunk，提取 API 端点

```bash
# 落地页里列出所有 chunk，逐个下载，grep "/api/"
```

**记到**：`profile.endpoints`。用 `probe.role_endpoints()` 把原始路径映射到规范角色：

| 角色 | 含义 | 典型路径 |
| :--- | :--- | :--- |
| `check-email` | 邮箱是否已注册 | `/api/referral/check-email` |
| `sign-up` | 建号 | `/api/auth/sign-up/email` |
| `send-verification` | 触发验证邮件 | `/api/auth/send-verification-email` |
| `verify` | 校验 token | `/api/auth/verify-email` |
| `session` | 当前会话 | `/api/auth/get-session` |
| `credits` | 额度 | `/api/credits` |

### 1.4 同一轮扫描里判断**有没有验证码**

关键词：`turnstile` / `cf-turnstile` / `recaptcha` / `grecaptcha` / `hcaptcha` /
`_abck`（Akamai）/ `slideverify` / `geetest`。

**记到**：`profile.captcha`，取值 `none` / `turnstile` / `recaptcha` / `hcaptcha` /
`akamai` / `slider` / `unknown`。

> **这是全流程最重要的一个事实。** 有验证码 → 第 2 步一定停。
> 不要试图"想办法过去"：token 不可伪造，能做的只是"在真 Chrome 里点复选框"，
> 成功率完全取决于 IP 与指纹信誉，那是**运气**，不是工程。

### 1.5 打公开 config 接口

常见路径：`/api/config/public`、`/api/config`、`/api/public/config`。

典型字段：`email_verification_enabled`、`google_auth_enabled`、`invite_code_required`。

**记到**：`profile.verification`（是否需要验证）、`profile.notes`。

### 1.6 DOM 探针（只读）

用 Playwright 打开落地页，读出所有 `input` / `button` / `label` 的
`id` / `name` / `type` / `data-testid` / `placeholder`。

**选择器优先级（硬规则）**：

```
id  >  name  >  data-testid  >  type+索引  >  文本（兜底）
```

**文本选择器是脆弱的** —— 它随语言变。站点按 `Accept-Language` 跳 `/zh/`、`/en/`，
同一份选择器在不同语言下结论不一致。探站时**固定 locale**（用 `/en/` 路径 +
`locale="en-US"` 的 context）。

**记到**：`profile.selectors`，并把稳定性分类存进 `profile.selector_stability`。

### 1.7 推进一步（唯一的一次写请求）

很多站的密码字段**在第一步之后才渲染**。办法：填一个探针邮箱 → 点 Continue →
再读一次 DOM。

> 这一步发的是**「邮箱是否已注册」的存在性检查**，**不建号**。
> 但要用**明显的探针地址**（如 `...-probe-xxxx@example.com`），并在 profile 的
> notes 里记录做过这一次检查。

### 1.8 判定验证方式

| 观察到的 | 结论 |
| :--- | :--- |
| 验证页有**验证码输入框** | `code` 型，需要 `pattern`（默认 `\b\d{4,8}\b`） |
| 有 `verify-email` 端点但**没有输入框** | `link` 型，需要 `pattern` 抽 URL |
| 两者都没有 | `none` 型 |

**记到**：`profile.verification.{mode,sample,pattern}`。

**记到 profile 的是「形状」，不是「内容」**：验���模式的判定会随站点改版失效，
所以**每次运行前重新探测**，不缓存结论。

---

## 第 2 步 · 判定（verdict）

`invitesignup.verdict.decide(profile)` 是一个**纯函数**：给定 profile，返回能不能自动、
卡在哪、需要人配合哪一步。把它单独做成纯函数，是因为**这个决定必须是可离线测试的**。

| verdict | 触发条件 | 含义 |
| :--- | :--- | :--- |
| `automatable` | 无验证码 + 验证模式已知 + 关键选择器齐全 | 可以跑 |
| `needs-human-in-loop` | 流程可行但某一步要人 | 可跑，但要人配合 |
| `blocked-by-captcha` | 存在验证码 | **停**，不绕过 |
| `blocked-by-tos` | 条款禁止自动化（人显式声明） | **停** |
| `blocked-missing-facts` | 邀请码无效 / 选择器缺失 / 验证模式未知 | **停**，去补事实 |

**`unknown` 永远不等于 `none`。** 探不出来就是 `unknown`，然后停。
猜错一次的成本，远高于多探一轮。

---

## 第 3 步 · 生成或校订 profile

`probe` 的自动产物是**草稿**。下面这些必须人工确认：

| 字段 | 怎么确认 |
| :--- | :--- |
| `selectors` | 在浏览器里核对每个选择器确实指向目标输入框 |
| `steps` | 确认步骤顺序，尤其是"密码在存在性检查之后才出现"这种两步表单 |
| `verification.pattern` | 抽一次真实邮件，确认正则命中的是**验证链接**而不是图片/logo |
| `endpoints` | 确认每个角色对应的是**签发**端点，不是**查询**端点 |
| `max_successful_invites` | 调 `referral/me` 之类接口读出来，不要猜 |
| `sign_in_endpoint` | 独立验收要用它，必须和注册端点区分开 |

**推荐做法**：把已验证的站点写成 `adapters/<site>.py`，用 `profiles/<site>.example.json`
作为提交物（**不含真实邀请码与凭据**）。这样 profile 进了版本控制，改版时能 diff。

---

## 第 4 步 · 执行

```bash
python -m invitesignup run --profile profiles/example.json
```

### 4.1 停批语义（借鉴 muse-protocol，这里更严格）

| 失败类型 | 处置 |
| :--- | :--- |
| **传输层**失败（DNS/连接超时） | 可重试，同一个邮箱换代理/换网络再来 |
| **业务拒绝**（账号已存在、邀请码无效、条款拒绝） | **不重试、不换号、不补号**，记录后停 |
| **已触发发信**之后的任何失败 | **不重试、不补号**，记录后停 |

> 「发信之前失败可以重试，发信之后失败必须停」是硬边界：
> 发信意味着服务端已经记下了这次尝试，盲目重试就是在烧号。

**这条规则由代码执行，不是靠自觉。** `invitesignup/failures.py` 把错误先分类
（transport / rate_limit / site_rejected / captcha / mail_timeout / config /
verification / unknown），再用 `mail_requested` 这个阶段标志决定动作：

```
classify("Connection reset by peer", mail_requested=False)  -> transport / retry
classify("Connection reset by peer", mail_requested=True)   -> transport / abort
classify("something nobody has seen before")                -> unknown   / stop
```

**未分类的错误默认不重试。** 宁可停一次让人看一眼，也不要自动重试出一个重复账号。

### 4.1.1 邮箱被拒是**第四种**动作

不是 retry，也不是 stop，而是 `NEEDS_HUMAN`：

| 情形 | Kind | Action | 为什么 |
| :--- | :--- | :--- | :--- |
| 站点说这个邮箱是一次性的 | `mailbox_rejected` | `needs_human` | 同一个地址必然再被拒，重试无意义；换邮箱只有人能做 |
| 业务拒绝（账号已存在等） | `site_rejected` | `abort` | 整批停，不换号 |
| 收不到验证信 | `mail_timeout` | `stop` | 账号可能已建，不能再跑 |

**为什么值得单独一类**：这三种失败的处置方式完全不同，混成一个
"失败了"会让人用错补救手段——比如对 `mailbox_rejected` 反复重试，
或者对 `mail_timeout` 换邮箱重跑（那会建出第二个号）。

识别是**当场**做的：提交邮箱后立刻读页面的可见错误文案，命中
`disposable` / `temporary email` / `domain not allowed` / `临时邮箱` 等
就停，不等 120 秒超时。

```
[stage] mailbox-rejected
the site refused this mailbox: Disposable email addresses are not allowed
→ supply a real inbox with --email <address>
```

### 4.1.2 半自动模式：`--wait-link`

当站点拒掉所有一次性邮箱、而验证邮件只能发到你能访问的收件箱时，
不要退化成"手动注册一遍"。用：

```powershell
python -m invitesignup run --profile p.json --email you@outlook.com --wait-link --yes
```

```
自动：打开邀请页 → 填邮箱 → 填密码 → 提交 → 触发发信
人工：只做一件事——把邮件里的链接粘进来
自动：打开链接 → 确认会话 → 读额度 → 落台账
```

**这条路径仍然受纪律约束**：它不绕过任何挑战，只是把"读你自己的收件箱"
这一步交还给你。粘空行等于放弃，运行记为失败。

### 4.2 每一步都要留痕

- 失败时截屏 → `output/snapshots/`
- 收到的每一封邮件都 dump → `output/mail_dump_<site>.txt`
- API 轨迹记进结果对象

### 4.3 配额闸门

`profile.max_successful_invites` 是**服务端自己说的**上限。执行前对照台账里
该邀请码已成功次数，用满就停，除非显式 `--allow-multi`。

**不要把「把邀请码用满」当成默认行为。** 邀请计划给你几个名额，是让你分享给
真实的人，不是让你自己刷。

### 4.4 收信必须防串码

验证邮件的匹配有**两道闸**，缺一不可：

| 闸 | 做法 | 为什么 |
| :--- | :--- | :--- |
| **消息 ID 基线** | 发出注册请求**之前**先 `current_ids()` 抓一次，之后只接受不在这个集合里的邮件 | 时钟会漂，ID 不会。上一轮遗留的验证邮件会被当成这一次的 |
| **时间戳** | `not_before` 设为请求发出的时刻 | 兜底；但**必须能正确解析**，否则静默失效 |

> 时间戳解析是易错点：mail.tm 的 `createdAt` 是 ISO-8601 字符串
> （`"2026-11-17T10:20:06.372Z"`），不是毫秒数。解析失败会返回 `0.0`，
> 而 `0.0` 是 falsy，`not_before` 那个条件直接短路——保护**静默失效**。
> 用 `mailbox.parse_timestamp()`，它有离线测试覆盖。

---

## 第 5 步 · 独立验收

```bash
python -m invitesignup verify --profile profiles/example.json --email ... --password ...
```

**另起一个进程、用 HTTP 重新登录、重新查状态端点**：

- 不复用执行时的浏览器上下文
- 不复用执行时的 cookie
- 只有它说 `verified=true`，这次运行才算数

**执行者不得自己宣布成功。** 这条规矩不新鲜，但它挡掉的错误极多：
页面跳转成功 ≠ 会话建立；会话建立 ≠ 邮箱已验证；邮箱已验证 ≠ 额度到账。

---

## 第 6 步 · 归档

| 产物 | 位置 | 作用 |
| :--- | :--- | :--- |
| 账号台账 | `output/accounts.jsonl`（gitignore） | 本机留底，含密码 |
| 运行台账 | `output/ledger.jsonl` | append-only、脱敏、去重键 |
| profile | `profiles/<site>.json` | 进版本控制，供复用 |
| 站点协议笔记 | `docs/PROTOCOL-<SITE>.md` | 记录**坑**，不只记录流程 |

台账是 **append-only**：失败重试是新的一行，不是改旧行。
`dedupe_key = invite-signup:<site>:<code>:<date>`，同一邀请码同一天不重复跑。

---

## 故障分诊表

| 现象 | 最可能的原因 | 处置 |
| :--- | :--- | :--- |
| 提示 `mailbox-rejected` | 站点明确拒绝了一次性邮箱域名 | 加 `--email` 用真实邮箱，或加 `--wait-link` 走半自动。**不要重试同一地址** |
| 注册页提交后说"收不到验证信"，但邮件其实**到了** | 邮件正文是**数组**不是字符串，被 join 炸掉后异常被吞 | 用 `mailbox.flatten()`；检查 `mail_dump` 里 `body (N chars)` 的 N |
| 找不到"继续"按钮 | 按**文案**匹配 + 正则用了 `\b`（对 CJK 不成立） | 改用 id/name/type；探站时固定 locale |
| 第一次探测只有邮箱框，第二次才有密码框 | 两步表单 | 用 `advance_probe()` 推进一次（存在性检查，不建号） |
| 探站说"没有验证码"，跑起来卡在验证 | 验证码是后加载的 iframe | 以**运行时**轨迹为准，重新探站并把该站标记为有验证码 |
| 临时邮箱被拒（域名被拉黑） | 站点有域名黑名单 | 切「自备邮箱 + 人工贴一次链接」；不要改用真实域名拼造地址 |
| 账号建了但额度没到账 | 归因没带上 | 检查 `attribution`：cookie 过期？query 没传？ |
| 独立验收说 sign-in 失败 | 注册返回的密码与登录要求不同（如需要额外字段） | 读 `sign_in_fields`，对照注册时的实际 payload |
| 同一站点第二次跑直接跳过 | 去重键命中 | 这是设计行为；确实要重跑用 `--force` |

---

## 复用清单

做下一个邀请制站点时，按顺序打勾：

- [ ] 第 0 步：授权、条款、要建几个号
- [ ] 1.1–1.5 抓 payload / 归因 / 端点 / 验证码 / config
- [ ] 1.6 DOM 探针（固定 locale）
- [ ] 1.7 推进一步，看密码字段
- [ ] 1.8 判定验证方式
- [ ] 第 2 步：verdict 是 `automatable` 才继续
- [ ] 第 3 步：把已验证事实写进 `adapters/<site>.py`
- [ ] 第 4 步：执行（单号；配额闸门）
- [ ] 第 5 步：独立验收
- [ ] 第 6 步：归档 + 写 `docs/PROTOCOL-<SITE>.md`
