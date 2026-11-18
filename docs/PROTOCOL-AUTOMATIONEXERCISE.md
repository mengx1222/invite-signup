# 协议笔记：automationexercise.com

> 记录于 2026-11-18。**这不是邀请制站点**，是公开的自动化测试练习站。
> 用它做「第二个站点」验证，是因为它结构完全不同、且允许自动化访问。
> 本文件的价值不在于"又多支持一个站"，而在于**它暴露了探站器的三个误判**。

---

## 为什么选它

第一轮只在 yila.ai 上验证过。一个样本不足以说明探站器是通用的——
尤其 yila.ai 是 Next.js + Better Auth，特征太典型。这个站是：

| 维度 | yila.ai | automationexercise.com |
| :--- | :--- | :--- |
| 框架 | Next.js (RSC) | Django 模板渲染 |
| 认证 | Better Auth，`/api/auth/*` | 表单 POST + CSRF token |
| 邀请制 | 是 | **否** |
| 验证码 | 无 | 无（但页面挂 AdSense） |
| 页面上 email 输入框 | 1 个 | **3 个**（登录/注册/页脚订阅） |

差异足够大，正好压测探站器的假设。

---

## 它暴露的三个误判

### 1. 选择器被页脚订阅框抢走

页面有三个 email 输入框：

| 位置 | 属性 |
| :--- | :--- |
| 登录表单 | `name="email"`（无 id） |
| 注册表单 | `name="email"`（无 id） |
| 页脚订阅 | `id="susbscribe_email"`（站点自己拼错了 subscribe） |

「id > name」的属性阶梯让页脚那个赢了——**整条注册流程都会填错地方**。

**修法**：加 `looks_incidental()`，把 newsletter / subscribe / search / promo
这类明显不属于注册表单的输入先排除掉，**再**走属性阶梯。
上下文优先于属性。

### 2. AdSense 的 reCAPTCHA iframe 被当成验证码

页面挂了 Google 广告，广告栈会加载
`https://www.google.com/recaptcha/api2/aframe`——那是**广告质量检测帧**，
不是给用户看的挑战。

探站器把它算成 `recaptcha`，verdict 就会判 `blocked-by-captcha`——
**一个没有验证码的站点被自己的护栏拦住**。

**修法**：`CAPTCHA_FALSE_FRIENDS` 名单，排除
`google.com/recaptcha/api2/aframe`、`doubleclick.net`、
`adtrafficquality.google`、`googlesyndication.com` 等广告域。
真正的 `recaptcha/api2/anchor` 仍然算数。

### 3. `/signup` 被当成邀请码

没有 invite payload 时，探站器会从 URL 最后一段猜邀请码。
`/signup` 的 `signup` 恰好 6 个字母——被当成邀请码，
于是 verdict 说「invite code did not validate」。

**对一个根本不是邀请制的站点，这是自信的错误结论。**

**修法**：只有 URL 里出现 `/invite/` `/invitation/` `/ref/` `/referral/`
这类标记时才猜；同时 verdict 区分
`invite_absent`（没找到码，可能不是邀请制）和
`invite_invalid`（码被拒了，链接可能失效）——两者的处置完全不同。

---

## 站点本身的结构（实测）

```
GET  /signup                    200, 三个 email 输入框
POST /signup                    form-encoded, 带 csrfmiddlewaretoken
```

| 项 | 实测值 |
| :--- | :--- |
| 验证码 | 无 |
| 邮箱验证 | 无（注册即完成） |
| 步骤 | 单步表单 |
| API 端点 | 无 `/api/`，纯表单 POST |
| traffic 捕获 | 0 条（没有 XHR） |

**没有 `/api/` 是重要信息**：说明这个站的 `confidence["endpoints"]`
必然是 `missing`，而那不代表探站器坏了——**是站点本来就没有 API**。

---

## 这次验证证明了什么、没证明什么

**证明了**：
- 探站器在完全不同的框架上仍能读出表单结构
- `confidence` / `needs_review()` 给出的判断是准的
- 三个 bug 都是**真 bug**，修完之后行为正确

**没证明**：
- 这个站不是邀请制，所以 `run` / `verify` 的完整链路**没有跑**
- code 型验证（6 位数字码）仍然没有真实站点验证过
- 滑块 / Turnstile 站点仍然只有"检测到就停"的路径，没有"能跑通"的样本

**不要把这次当成"第二个站点全链路通过"**——它只验证了 probe 这一段的通用性。
