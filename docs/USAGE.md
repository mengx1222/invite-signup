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
[gate  ] this profile needs a human ack before running:
         - verification is link-based; a mailbox is required
         re-run `run` with --yes once you have read the above
```

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
