# 协议笔记：yila.ai

> 记录于 2026-11-17，全部条目均为**实测**（跑通一次完整链路，27.9 秒）。
> 本文件不含任何有效邀请码或凭据。

---

## 协议流程

| 步骤 | 请求 | 观察到的响应 |
| :--- | :--- | :--- |
| 落地 | `GET /en/invite/<code>` | 页面内含 `landingData`：`{valid, referralCode, inviterName, rewardCredits, hasRewardSlots}`；服务端下发 cookie `yila_invite_code` |
| 存在性检查 | `POST /api/referral/check-email` `{email, referralCode}` | `{"code":0,"message":"ok","data":{"exists":false}}` |
| 建号 | `POST /api/auth/sign-up/email` `{name, email, password, ...}` | `200 {"token":null,"user":{...,"emailVerified":false}}` |
| 触发发信 | `POST /api/auth/send-verification-email` | `200 {"status":true}` |
| 校验 | `GET /api/auth/verify-email?token=<JWT>&callbackURL=/research` | `302` |
| 会话 | `GET /api/auth/get-session` | `{"session":{...},"user":{...,"emailVerified":true}}` |
| 额度 | `GET /api/credits` | `{"data":{"balance":500,"history":[{"transactionScene":"signup_bonus","credits":500,"description":"Welcome research credits","expiresAt":...}]}}` |
| 邀请统计 | `GET /api/referral/me` | `{referralCode, rewardCredits:200, maxSuccessfulInvites:4, remainingInvites, earnedCredits, history}` |
| 登录 | `POST /api/auth/sign-in/email` `{email, password}` | `200 {"redirect":true,"token":...,"url":"/research","user":{...}}` |

---

## 容易踩的坑

### 1. **没有验证码** —— 这是它最简单的根本原因

扫过落地页加载的 **25 个 chunk**，`turnstile` / `recaptcha` / `hcaptcha` / `captcha`
**零命中**。所以这条链路不需要任何"突破"动作。

### 2. **验证是链接式，不是码式**

`/verify-email` 页面**没有任何输入框**，只有三个按钮：重新发送 / 继续 / 返回登录。
页面每秒轮询 `get-session`，直到服务端把账号标记为已验证。

**推论**：抓验证邮件时要从**链接**里提取 token，不是去正则匹配 6 位数字。

### 3. **没有独立注册页**

`/en/sign-up` 会 **302 回 `/en/sign-in`**；注册表单只存在于**邀请落地页**上。
「不用邀请码注册」这条路在 UI 上不存在（`invite_code_required` 配置是 `false`，
但 UI 仍然只从邀请入口走）。

### 4. **两步表单**

第一步只有邮箱框。点 Continue 之后才渲染
`#invite-password` / `#invite-confirm-password`（密码 ≥ 8 位，两处必须一致）。
**只探一次 DOM 看不到密码字段**，必须推进一次。

### 5. **注册用的 name 是邮箱本地部分**

前端 chunk 里 `name: email.split("@")[0]`，注册响应里的 `name` 字段实测等于邮箱的本地部分（不是合成人设名）。
所以上游那套"合成人设"（Alexander / Jacob / `alexanderjac148`）在这一步**被完全绕过**，
只有密码来自 `generate_compliant_password()`。

### 6. **邀请计划有次数上限**

`maxSuccessfulInvites: 4`（服务端强制）。每成功邀请一次：邀请方 +200，被邀请方 +200。
**用满 4 个名额是分享，不是刷号** —— 本项目默认只跑 1 个。

### 7. **额度 30 天过期**

`signup_bonus` 那条账目带 `expiresAt`（实测为发放后 30 天）。过期未用完作废。

---

## 归因怎么带

邀请链接落地时**服务端下发** `yila_invite_code` cookie，
且 `check-email` 的请求体里带 `referralCode`。
只要浏览器从邀请 URL 进入，归因就是自动的；后续任何跳转都带着这个 cookie。

---

## 待确认

- **邀请人账户是否到账 +200**：只有登录邀请人自己的账户才能看（`/en/invite/<code>` 的
  Referrals / Credits 栏）。**执行方无法代验**，需要人自己确认。
- **500 是多少构成的**：`signup_bonus` 单条账目记 500 credits。
  推断为「基础 300 + 邀请 200」，但**无法做无邀请码的对照注册**（见坑 3），
  所以这条是**推断**，不是实测。
