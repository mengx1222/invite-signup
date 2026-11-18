# 怎么用

三条命令：`probe` 探站、`run` 建一个号、`verify` 独立验收。
**顺序不能颠倒**——`run` 之前必须先看 `probe` 的结论。

---

## 一次性准备

```powershell
git clone git@github.com:mengx1222/invite-signup.git
cd invite-signup
pip install -r requirements.txt
```

本机已装 Chrome 就直接用（`channel="chrome"`），不用 `playwright install`。

---

## 第 1 步：探站（只读）

拿到一个邀请链接后，先探它：

```powershell
python -m invitesignup probe "https://yila.ai/en/invite/YL-XXXX" --out profiles/yila.json
```

输出分三块：

```
[verdict] [automatable] verification is link-based; a mailbox is required
[human ] if the disposable mailbox is rejected, supply a real inbox and paste the link
[conf  ] inferred, not measured: endpoints
[review] confirm before trusting this profile:
         - no account-state endpoint (balance will not be reported)
[gate  ] this profile needs a human ack before running:
         - verification is link-based; a mailbox is required
         re-run `run` with --yes once you have read the above
```

`[conf]` 列出**推断**而非实测的项。`measured` 表示这次探站真的观察到了；
`inferred` 表示从代码或结构里推出来的，值得人看一眼。`[review]` 更直接，
把需要确认的地方列成清单（选择器脆弱、验证模式未知、缺 state 端点等）。

| verdict | 含义 | 下一步 |
| :--- | :--- | :--- |
| `automatable` | 可以跑 | 读一遍 `[human]` 提示，再 `run --yes` |
| `blocked-by-captcha` | 有验证码 | **停**。自己在浏览器里手动注册 |
| `blocked-by-tos` | 条款禁止自动化 | **停**。别跑 |
| `blocked-missing-facts` | 关键事实探不出来 | 加 `--headed` 亲眼看一次，或手工补 profile |

**`probe` 唯一的写操作**是一次「邮箱是否已注册」的存在性检查，**不建号**。
不想要它就用 `--no-advance`（代价：看不到第二步的密码字段）。

---

## 第 2 步：建号

```powershell
python -m invitesignup run --profile profiles/yila.json --yes
```

**默认只建 1 个号**，用一次性邮箱，验证邮件自动收、自动点。

常用变体：

| 想做的事 | 加什么 |
| :--- | :--- |
| 用自己邮箱（站点拒临时邮箱时） | `--email you@outlook.com` |
| 用自己邮箱，发信后停下来让你贴链接 | `--email you@outlook.com --wait-link` |
| 想看着它跑 | `--headed` |
| 同一个邀请码再跑一次 | `--force`（默认会被去重挡） |
| 用满剩余名额 | `--allow-multi`（**先想清楚**） |
| 等信久一点 | `--poll-timeout 300` |

---

## 第 3 步：独立验收

`run` 说成功**不算数**。换一个干净会话重新登录：

```powershell
python -m invitesignup verify --profile profiles/yila.json `
    --email <上一步的邮箱> --password <上一步的密码>
```

期望输出：

```
verified=True
  [ok  ] sign-in: POST /api/auth/sign-in/email accepted
  [ok  ] session: user=... verified=True
  [ok  ] state:credits: {"data":{"balance":500,...}}
```

只有 `verified=True` 才算真的成了。

---

## 看台账

```powershell
python -m invitesignup ledger
```

每次尝试一行（成功失败都记），`output/ledger.jsonl`。
账号密码在 `output/accounts.jsonl`，**明文，别提交**（已在 `.gitignore`）。

`outcome` 有四种：

| outcome | 含义 |
| :--- | :--- |
| `success` | 建号并建立了会话 |
| `failure` | 跑过但没成（看 `stage` 知道死在哪步） |
| `declined` | 被 `--yes` 门禁挡下，**根本没开始跑** |

`declined` 单独列出来，是为了让"没试过"和"试了被闸门挡住"能区分开。

---

## 常见情况怎么处理

| 现象 | 处理 |
| :--- | :--- |
| `blocked-by-captcha` | 别绕。手动注册一次，或换站 |
| 收不到验证邮件 | 看 `output/mail_dump_<site>.txt`——信到了但解析失败，和没到是两回事 |
| 临时邮箱被拒 | 换 `--email` 用自己邮箱，脚本会停下来让你贴链接 |
| 提示 `mailbox-rejected` | 站点明确拒了这个域名。加 `--email` 换真实邮箱，或加 `--wait-link` 走半自动 |
| `skipped-duplicate` | 今天这个码已经成功过了。确实要重跑加 `--force` |
| `blocked-by-quota` | 名额用完了。这是保护，不是 bug |
| 探站说可以、跑起来被拦 | 站点改版了。重新 `probe` 一次 |

---

## 三条硬边界（代码里执行，不是文档承诺）

1. **有验证码就停**，不尝试绕过，不接打码服务。
2. **条款禁止自动化就停**（用 `--tos-blocks` 声明）。
3. **默认一个邀请码一个号**。用满配额要显式 `--allow-multi`。

外加一条：**不在真实邮箱域名（gmail/outlook 等）下拼造不存在的地址**。
临时邮箱只来自公开临时邮箱服务。

---

## 换一个新站点要做什么

```powershell
# 1. 探
python -m invitesignup probe "https://新站/invite/CODE" --out profiles/newsite.json

# 2. 看 verdict，不是 automatable 就停

# 3. 核对 profile 里的 selectors / endpoints（探站器可能有漏）
#    在浏览器里对着看一遍，改 profiles/newsite.json

# 4. 跑
python -m invitesignup run --profile profiles/newsite.json --yes

# 5. 验收
python -m invitesignup verify --profile profiles/newsite.json --email ... --password ...

# 6. 把实测到的事实写成 adapters/newsite.py + docs/PROTOCOL-<SITE>.md
```

第 3 步别省。探站器能挖出大部分，但**端点角色**和**验证链接的匹配特征**
最好人工核一遍——猜错的代价是一个号。

---

## Web 界面

```powershell
python -m invitesignup web
```

然后打开 <http://127.0.0.1:8765>。

### 界面上有什么

三个卡片对应三条命令，每个卡片顶部有一段说明写清"这一步在做什么"：

| 卡片 | 在做什么 | 会不会建号 |
| :--- | :--- | :--- |
| **1 · 探站** | 搞清楚这个站怎么注册 | **不会**（只读 + 一次存在性检查） |
| **2 · 建号** | 按第 1 步的 profile 建一个号 | 会，默认 1 个 |
| **3 · 独立验收** | 换干净会话重新登录，确认号能用 | 不会 |

**进度区**显示三样东西：

- **百分比**（0–100）：按各阶段的相对耗时加权算的，不是精确计时
- **步骤清单**：当前走到哪一步、还剩哪几步，已完成的打勾
- **实时日志**：每个动作一行，包括"已等 12s / 共 120s"

扫描前端代码那一步会逐条报 `3/25 bundles`，进度条在这期间也会缓慢前进——
不然三十秒不动会让人以为卡死了。

### 报错怎么读

失败时进度区下方会出现一个红色面板，三行：

```
E201                                    ← 错误码，可以 grep
the site refused this mailbox: ...      ← 到底发生了什么
下一步：用 --email 换真实邮箱...         ← 该怎么办
```

错误码是稳定的，不会改含义：

| 码 | 含义 | 该怎么办 |
| :--- | :--- | :--- |
| `E100` | 网络/连接问题 | 发信前可重试；发信后不要重试 |
| `E101` | 被限速 | 等一会儿，或换出口 |
| `E200` | 没等到验证邮件 | 先看 mail dump，信到了但解析失败 ≠ 没到 |
| `E201` | 站点拒绝这个邮箱 | 换真实邮箱，或走 `--wait-link`。**不要重试同一地址** |
| `E300` | 站点拒绝这次请求 | 账号已存在/邀请码无效/条款拒绝，整批停 |
| `E301` | 遇到验证码 | 本工具不绕过，手动完成 |
| `E302` | 账号可能建成了但没证明 | 单独跑一次 verify |
| `E303` | 站点不用表单注册（钱包签名 / 仅第三方登录） | 手动注册一次；本工具不保管钱包私钥 |
| `E400` | profile 不完整或过期 | 重新探站，或手工补字段 |
| `E900` | 未分类 | 看截图和 page_text 再决定 |

### 半自动模式在网页上怎么用

勾上「用我自己的邮箱，收到信我自己贴链接」，然后在邮箱栏填你自己的地址。
流程会停在「等待验证链接」，页面出现一个输入框——把邮件里的链接粘进去，
点「提交链接」，剩下的自动继续。

**它只是同一套命令的另一种入口**，没有第二份逻辑：探站 / 建号 / 验收走的还是
`probe` / `run_registration` / `verify_account`。好处是能看到**实时进度**——
探站要扫 25 个 bundle、等验证邮件可能要一两分钟，CLI 在这段时间里是哑的。

设计上有三条刻意的限制：

| 限制 | 原因 |
| :--- | :--- |
| 只绑 `127.0.0.1` | 这个工具持有账号密码。绑到外网卡不是"方便"，是泄露 |
| **一次只跑一个任务** | 两个任务会抢同一个浏览器、把去重台账搞乱。第二个请求直接拒绝，不排队 |
| 没有数据库 | 任务是内存里的，重启就没了。要留档看 `output/ledger.jsonl` |

`--host 0.0.0.0` 可以开，但会先打一行警告——想清楚再开。

### 探站器**探不到**什么

有一类端点它永远发现不了：**登录后才会加载的接口**。

比如 `/api/credits` 只在你登录进去之后才被前端请求，探站时那个页面根本没加载过。
所以自动生成的 profile 里 `endpoints_state` 通常是空的——**这不是 bug，是边界**。

后果：建号能成功，但结果里读不到额度。想要额度，手工加：

```json
"endpoints_state": {
  "credits": "/api/credits",
  "referral": "/api/referral/me"
}
```

怎么知道该填什么：登录进站点，开 DevTools → Network，看仪表盘加载时打了哪些
返回余额的接口。

---

## 半自动模式（`--wait-link`）

有些站会拒掉所有一次性邮箱，而验证邮件只能发到你能访问的真实邮箱。这时：

```powershell
python -m invitesignup run --profile profiles/site.json `
    --email you@outlook.com --wait-link --yes
```

流程变成：

```
自动打开邀请页 → 自动填邮箱 → 自动填密码 → 自动提交
        ↓
   停下来，等你把邮件里的链接粘进来
        ↓
自动打开链接 → 自动确认会话 → 自动读额度 → 落台账
```

**只有中间那一步是人工的**，前后仍然自动化。这比"跑失败了再去手动注册一遍"省事得多。

粘空行（直接回车）等于放弃，运行会记为失败。

### 站点明确拒信时会立刻停

如果站点在**提交邮箱时**就报"不支持一次性邮箱"，runner 会当场识别并停下：

```
[stage] mailbox-rejected
the site refused this mailbox: Disposable email addresses are not allowed
→ supply a real inbox with --email <address>
```

而不是等两分钟才说"没收到验证邮件"。这两种情况的处理方式完全不同，
所以必须分开报。

---

## 通用性：没有哪个站点是"默认"

早期版本把 `sign_in_endpoint` 和 session 端点默认成 Better Auth 的路径
（`/api/auth/sign-in/email`、`/api/auth/get-session`）。那让整个工具**悄悄变成了
Yila 专用**：在任何用 Better Auth 的站点上"能跑"，在别处一律失败，而且失败信息
误导（"没有会话"而不是"这个 profile 没告诉我怎么登录"）。

现在：

- `sign_in_endpoint` **没有默认值**。探站器能找到就填，找不到就留空。
- 留空时 `verify` 会明确说「profile 没有 sign_in_endpoint，无法独立验收」，
  而不是去试一个这个站可能从来没有过的路径。
- `needs_review()` 会把「缺 sign-in 端点」列进待确认清单。
- 手写 profile 时端点写完整 URL 或相对路径都可以，不会被拼成
  `https://sitehttps://site/api/me`。

**同理**：只有邮箱、没有密码的单步表单（magic link 类站点）现在也能跑。
verdict 早就说它 automatable，但 runner 会因为"没有密码框"报错——判定和执行
打架。现在没有密码框就跳过填密码，直接提交。
