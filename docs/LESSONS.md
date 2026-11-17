# 踩坑记录

> 格式沿用 [muse-protocol](https://github.com/duwqijlk/muse-protocol) 的做法：
> **只写真正踩过的坑**，每条都给出「症状 → 根因 → 处置」。
> 没有踩过的坑不写进来。

---

## 1. 邮件正文是**数组**，被 join 炸掉后异常被吞

**症状**：注册明确成功（页面到了 "Check your email"），服务端也返回
`send-verification-email → 200 {"status":true}`，但脚本一直报
「收不到验证链接」。看起来像**站点拦了临时邮箱**。

**根因**：临时邮箱服务 mail.tm 的消息详情里，`html` 字段返回的是**字符串数组**
（HTML 片段列表），不是字符串。上游代码直接：

```python
"\n".join(filter(None, [j.get("subject",""), j.get("text",""), j.get("html","")]))
```

`join` 遇到数组抛 `TypeError`，而外层 `except: return ""` 把异常吞成空字符串。
于是**正文恒为空** → 链接永远抽不出来。

**证据**：修复前 dump 文件里抓到的是
`subject='Verify your email - Yila AI'`、**`body (0 chars)`**；修复后同一位置
拿到了完整的 `https://yila.ai/api/auth/verify-email?token=...`。

**处置**：

- `mailbox.flatten()` 统一摊平 list / tuple / dict / None；
- 收信层**禁止**把异常吞成空字符串，每封收到的邮件都 dump 到文件；
- 判定「有没有收到信」看的是**正文长度**，不是「有没有抛异常」。

> 影响面：任何依赖 mail.tm 正文的自动化都受影响。这条坑值得给上游提 issue。

---

## 2. 正则里的 `\b` 对 CJK 不成立

**症状**：`get_by_role("button", name=re.compile(r"(?:^|\b)(Continue|继续)\b"))`
在英文下能点到按钮，切到中文 locale 就永远点不到。

**根因**：`\b` 是**单词边界**，依赖 `\w`（`[A-Za-z0-9_]`）。中文汉字不是单词字符，
所以 `继续` 两字之间、末尾都构不成词边界，`\b继续\b` 永远不匹配。

**背景**：站点按 `Accept-Language` 跳转 `/zh/`、`/en/`。同一个页面在 curl 下
（默认 Accept-Language）落在 `/en/`，在浏览器下落在 `/zh/` —— **探测结论会不一致**。

**处置**：

- 选择器优先级：`id > name > data-testid > type+索引 > 文本(兜底)`；
- 探站时固定 locale（`/en/` 路径 + `locale="en-US"` 的 browser context）；
- 用文本兜底前，先把该语言下的实际文案 dump 出来再写正则。

---

## 3. 邀请 payload 藏在嵌套 JSON 里

**症状**：探站返回 `{}`，`invite_valid` 是 `None`。

**根因**：Next.js 的 RSC payload 里是
`self.__next_f.push([1,"3:{\"landingData\":{\"valid\":true,\"referralCode\":...}}"])`。
解析时只匹配顶层 key，而真正的字段在**第二层**。

**处置**：解析后**逐层下钻**（`obj` 本身 + `obj` 的所有 dict 值），
找到含 `referralCode` / `inviteCode` 的那一层就返回。

---

## 4. 「注册成功但说收不到信」是个陷阱

**症状**：同第 1 条。

**教训**：这类流程里，**"没报错"和"没收到东西"是两回事**，而最省事的解释
（站点拦了我）恰好是最错的解释。

**处置**：任何一步失败，先看**留痕**（dump / 截图 / API 轨迹），
再下结论。留痕缺失时，先把留痕补上再重试。

---

## 5. 上游脚手架生成的适配器跑不动两步表单

**症状**：通用脚手架生成的是 `email → password → username → submit` 的**单步**适配器，
在 Yila 上跑不完。

**根因**：Yila 的密码字段要等 `check-email` 返回 `exists=false` 之后才渲染，
而且验证是**链接式**的，模板里根本没有「收信 → 抽链接 → 打开」这一段。

**处置**：本项目的适配器模板是**邀请制专用**的；站点差异沉淀在
`adapters/<site>.py` 里，由 `profile` 驱动执行，而不是由模板里的选择器驱动。

---

## 6. 上游在**真实邮箱域名**上拼造不存在的地址

**症状**：读 `RegisterHub` 的 `sites/milionseek.py` 时发现：

```python
accepted_domains = ["outlook.com", "gmail.com", "proton.me", "icloud.com", ...]
ident = generate_identity(email_domain=chosen_domain, locale="US")
```

**根因**：某些站会做 MX 校验并屏蔽一次性邮箱域名，于是脚本**在 gmail / outlook
等真实服务商的域名下拼出一个不属于任何人的地址**。

**性质**：这比用一次性邮箱严重一档 —— 不只是"临时"，而是在别人的域名下
伪造身份。

**处置**：**本项目不继承这个做法。** 临时邮箱只来自公开临时邮箱服务；
站点拒绝临时邮箱时，处置是「切自备邮箱 + 人工贴一次链接」，
而不是换成真实域名拼造。

---

## 7. 存在性检查不建号，但它确实发了一次请求

**处置**：`advance_probe()` 填探针邮箱点 Continue，发的是
「这个邮箱注册过吗」。实测返回 `{"code":0,"data":{"exists":false}}`，
**没有创建任何用户**。

**约束**：探针地址必须明显是探针（`...-probe-xxxx@example.com`），
并在 profile 的 notes 里写明做过这一次检查。

---

## 8. 页面成功 ≠ 会话建立 ≠ 额度到账

**症状**：这三件事在 Yila 上是**三个不同的状态**，中间任何一环断了，
"看起来成功"都会骗人。

**处置**：验收独立成步、独立进程、独立 HTTP 会话：

```
sign-in 成功  →  session 里有 user  →  状态端点（额度/邀请统计）有值
```

`runner` 说成功不作数，`verify` 说了算。

---

## 9. 凭据明文落盘 + 导进密码管理器

**症状**：上游把 `accounts.txt` / `keys.txt` 明文写盘，还提供
`export --export bitwarden` 导进密码库。

**问题**：一次性邮箱 + 固定格式密码 + 明文 + 可能被导出，
一旦泄露基本等于白送。

**处置**：

- 本项目账号只落在本机 `output/accounts.jsonl`（已 gitignore）；
- 台账 `output/ledger.jsonl` 自动脱敏 `password` / `token` / `api_key` 等字段；
- **不提供**导出到密码管理器的命令。

---

## 10. `("hcaptcha")` 不是元组，是**字符串**

**症状**：探站器把一个**完全没有验证码**的真实站点判成 `hcaptcha`，verdict 直接
`blocked-by-captcha`，流程根本跑不起来。

**根因**：

```python
CAPTCHA_KEYWORDS = {
    "hcaptcha": ("hcaptcha"),      # <- 单个括号，不是元组
    ...
}
```

`("hcaptcha")` 就是字符串 `"hcaptcha"`。`for needle in needles` 于是迭代出
**8 个字符** `h, c, a, p, t, c, h, a`，而扫描逻辑是「只要 needle 在 chunk 里出现
就算命中」—— 于是**每个 chunk 都命中了 `h`**。

**处置**：

- 所有关键词表的值必须是**元组**，并加一条测试断言「每个 needle 长度 > 1」；
- 验证码判定改成**分级**：
  - DOM 里有 captcha iframe / script / widget 元素 → 判定为该类型，**停**；
  - 只有 bundle 关键词 → 判定为 `suspected`，**不停**，但要求人盯第一遍；
  - 什么都没有 → `none`。
- 运行器再做一次**实时 DOM** 检查：真的挂载了挑战就停，在做任何不可逆动作之前。

> 这条是本项目里最贵的一个 bug：它让一个本来能全自动的站点被自己的护栏挡住。

---

## 11. 端点是**运行时拼出来的**，只有 fragment

**症状**：chunk 里明明有 `"/verify-email"`，但探站说「没有验证端点」，
verdict 判成 `blocked-missing-facts`。

**根因**：现代 auth 客户端（Better Auth / NextAuth 等）把 URL 拆成
**basePath + fragment**，bundle 里只有 fragment：

```js
"/api/auth"  +  "/verify-email"   →   /api/auth/verify-email
```

而第一版扫描只认完整的 `"/api/..."` 字面量，于是验证端点**完全隐形**。

**处置**：扫描时同时收集「相对 fragment」，再用 basePath 合成完整路径；
`role_endpoints()` 负责合成。合成结果要同时喂给验证模式判定。

---

## 12. 两步表单：推进之后的 DOM **不能覆盖**之前的

**症状**：探站说「找不到邮箱输入框」，而页面上明明有邮箱框。

**根因**：为了看到第二步的密码字段，探站会推进一次（存在性检查）。
但代码把**推进后的 DOM 整体替换**了推进前的 DOM —— 而推进后**邮箱框已经消失**
（Yila 的第二步只渲染两个密码框），于是邮箱选择器丢了。

**处置**：两个 DOM 代表同一表单的**两个步骤**，必须**合并**：

```
selectors = infer_selectors(dom_initial)          # 第一步：email
for k, v in infer_selectors(dom_advanced).items():  # 第二步：password / confirm
    selectors.setdefault(k, v)                     # 已有的不覆盖
```

---

## 13. React `useId` 生成的 id 每次构建都会变

**症状**：选出来的邮箱选择器是 `#base-ui-_R_79av5ubql3ivb_`。

**根因**：React 的 `useId()` 会生成形如 `base-ui-_R_79av5ubql3ivb_` 的 id，
**每次构建都变**。它是 `id` 属性的合法值，按「id 优先」规则会被选中 —— 但下次
站点重新构建，这个选择器就指向别的元素了。

**处置**：选择器优先级里加一条「**像生成 id 的 id 不算 id**」：

```python
GENERATED_ID_RE = re.compile(r"(_R_|_r_|^[a-z]{1,3}[0-9a-f]{6,}$|[0-9]{4,}$)")
```

命中就跳过 id，退到 name / data-testid / type。

---

## 14. 「名字里带 invite 的字符串」多半不是 cookie 名

**症状**：归因 cookie 抓成了 `invite-confirm-password`（那是字段 id）。

**根因**：客户端 bundle 里出现的带 "invite" 的字符串，既有 cookie 名，
也有字段 id、事件名、翻译键。

**处置**：**交叉排除** —— 拿页面上真实存在的 input id 列表做黑名单，
再排除掉含 `password` / `confirm` / `email` / `placeholder` / `label` 的候选。
剩下的才是 cookie 名。

另外，归因 cookie 往往是**前端 JS 写的**（`document.cookie = ...`），
不是服务端 `Set-Cookie` 头。两处都要查，先查 `Set-Cookie`（更权威）。

---

## 15. 时间戳解析失败是**静默**的，而且会废掉防串码

**症状**：没有立刻可见的症状。这是最危险的一类。

**根因**：`Message.received_at` 原本这么算：

```python
received_at=float(m.get("createdAt") or 0) / 1000.0
            if str(m.get("createdAt") or "").isdigit() else 0.0
```

而 mail.tm 的 `createdAt` 是 **ISO-8601 字符串** `"2026-11-17T10:20:06.372Z"`，
`isdigit()` 为 `False`，于是**恒返回 `0.0`**。

**后果**：`poll(not_before=...)` 里那句
`if not_before and message.received_at and message.received_at < not_before`
——`received_at` 是 `0.0`（falsy），整个条件直接短路，
**"只接受本次请求之后到达的邮件"这条保护完全失效**。上一轮遗留的旧验证邮件
会被当成这一次的验证结果，表现为"用错的链接把号验了"或"验了个已存在的账号"。

**处置**：

- 加 `parse_timestamp()` 统一处理 ISO-8601 / 毫秒 / 秒 / 数字字符串 / 垃圾输入，
  并配 6 条离线测试（含 ISO 带 Z、不带 Z、毫秒、秒、数字串、垃圾）；
- **不要只靠时间戳**。加 `before_ids` 基线：请求发出**之前**先抓一次
  `current_ids()`，之后只接受不在这个集合里的邮件。时间戳会因时钟漂移失准，
  消息 ID 不会。

> 这条是从 `lxf746/any-auto-register` 的 `before_ids` 做法学来的，比只看时间戳稳。

---

## 16. 「能不能重试」必须由**阶段**决定，不能只看错误类型

**症状**：一个连接超时，重试之后可能建出两个号，或者白白烧掉一个邀请名额。

**根因**：把失败一律当"技术问题，重试一下"。

**规则**：

| 阶段 | 传输层失败 | 429 | 站点拒绝 | 验证码 | 收不到信 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **发信请求之前** | 可重试 | 可重试 | 整批停 | 停 | — |
| **发信请求之后** | **整批停** | 停 | 整批停 | 停 | 停 |

发信意味着**服务端已经记下了这次尝试**，盲目重试就是在重复建号。

**处置**：`invitesignup/failures.py` 把错误分成 `Kind`（transport / rate_limit /
site_rejected / captcha / mail_timeout / config / verification / unknown），
再按 `mail_requested` 这个阶段标志决定 `Action`（retry / stop / abort）。
未分类的错误**默认不重试**。

---

## 17. 台账只在"最后一行"写，等于大部分失败**没有记录**

**症状**：翻 `ledger.jsonl` 发现失败记录远少于实际尝试次数；被 verdict 拦下的、
去重跳过的、配额挡住的，一条都没有。

**根因**：`run_registration()` 里有十几个守卫分支各自 `return result`，
而台账写入写在函数**最后**。于是每一个提前返回都跳过了持久化。
这不是"少写一行日志"，是让去重键和配额计数**失去了输入**——
`already_done()` 永远查不到失败，配额也永远算不准。

**处置**：拆成两层——

```
run_registration()   ← 只做一件事：调 _attempt()，然后无条件 _persist()
  └── _attempt()     ← 真正的流程，允许随便 return
```

外层保证"无论走哪条路，都写且只写一行"。`_persist()` 自己再包一层
`try/except`，台账写失败不能把整个运行拖垮。

**测试**：`tests/test_runner_ledger.py` 用 4 条用例覆盖被拦截 / 配额 / 去重 /
缺选择器四种提前返回，全部断言"台账多了一行"。

---

## 18. 判"有没有验证码"，**可见性**和"存在性"是两件事

**症状**：探站说 `automatable`，跑起来立刻 `blocked-by-captcha`，自相矛盾。

**根因**：`_live_captcha()` 只用 `locator(...).count()` 判断元素存不存在。
但验证码厂商习惯在 DOM 里**常驻一个隐藏容器**
（`<div id="cf-turnstile-xxx" style="display:none">`，或零尺寸的 wrapper），
只有站点真决定挑战时才把它显示出来。

于是两侧口径打架：

| 侧 | 判据 | 结论 |
| :--- | :--- | :--- |
| `verdict` | bundle 里有关键词 → 只标 `suspected`，**放行** | automatable |
| `_live_captcha` | DOM 里有元素 → **拦** | blocked |

**处置**：`_live_captcha()` 逐元素 `is_visible()`，只认**可见**的挑战。
选择器也收窄了：去掉 `script[src*=...]`（脚本存在不等于有挑战），
保留 iframe / 可见 widget 容器。

**测试**：`tests/test_live_captcha.py` 用真实 headless 浏览器跑 5 条——
隐藏容器不算、零尺寸不算、可见 Turnstile/hCaptcha 算、干净页面不算。

---

## 19. 一次**错误**的"修复"：`press_sequentially` 其实是对的

**我一度以为的 bug**：`press_sequentially` 会把每个字符当 xdotool 的键名解析，
密码里的 `+` `-` 会变成修饰键组合，输入内容错乱。

**实测结果**：用 `Ab+cd-Ef!g@h#i$j%k&l*m_n=o+p` 实测，
`press_sequentially` **逐字符还原完全正确**。

**但另一个差异是真的**：

| 方式 | keydown | keypress | input |
| :--- | :--- | :--- | :--- |
| `press_sequentially` | 每字符 1 次 | 每字符 1 次 | 每字符 1 次 |
| `keyboard.insert_text` | **整串只有 1 次** | 0 | 每字符 1 次 |

`insert_text` 少了 `keydown`/`keypress`，站点若监听按键事件判断"真人打字"，
它**更像脚本**。

**结论**：不要改。已经把这条写进 `human.py` 的 docstring，避免下次有人
（包括我自己）再"修"一遍。

> 教训：**先测，再改**。这次的假设听起来很合理，但就是错的。

---

## 20. 一段"防护"代码如果**没生效**，比没有更糟

**症状**：没有症状。这正是问题——它看上去在防检测，实际上什么都没做。

**根因**：`apply_environment_consistency()` 往每个 context 注入

```js
Object.defineProperty(navigator, 'webdriver', { get: () => false, configurable: true })
```

但实测发现：加上 `--disable-blink-features=AutomationControlled` 并去掉
`--enable-automation` 之后，`navigator.webdriver` **已经是原生 `false`**
（`function get webdriver() { [native code] }`）。原型链上的原生 accessor
优先级高于在实例上 `defineProperty`，所以这段注入**完全无效**。

**更糟的地方**：如果它真的生效了，反而会制造一个**非原生 getter**，
那本身就是可检测特征。

**处置**：删掉注入，只保留启动参数；`apply_environment_consistency()`
保留为空操作并写明原因，避免有人以为它是有效防护。

**判据**：验证"原生"与否，看
`Object.getOwnPropertyDescriptor(Navigator.prototype, 'webdriver').get.toString()`
里有没有 `[native code]`。

---

## 21. 同一个启动参数写在三个地方，一定会漂

**症状**：探站和执行的浏览器行为偶尔不一致，但代码"看起来一样"。

**根因**：`probe.dom_probe()`、`probe.advance_probe()`、`runner` 各自
复制了一份 `chromium.launch(args=[...], ignore_default_args=[...])`。
三份拷贝里改一份、漏两份，是迟早的事。

**处置**：收敛成 `human.launch_browser(playwright, headless=...)`，
参数只定义一次（`LAUNCH_ARGS` / `IGNORE_DEFAULT_ARGS`）。三处调用点全部改用它。

---

## 22. 算了却没人读的字段，是**假的安全门**

**症状**：`Verdict.requires_confirmation` 被赋值两处（`captcha == suspected`、
`verification` 需要邮箱），但 `grep` 全仓发现**没有任何调用方读它**。

**危害**：代码里看起来有一道"需要人工确认"的闸门，实际上 CLI 照样往下跑。
这比没有闸门更危险——读代码的人会以为它是拦住的。

**处置**：接进 CLI。

```
probe  -> 打印 [gate] 与理由（提示要用 --yes）
run    -> 没有 --yes 就退出 3，并列出理由
run    -> 有 --yes 才继续；但 captcha 仍然拦（--yes 不能越过 blocked）
```

**测试**：`tests/test_cli_gate.py` 三条——无 `--yes` 退出 3、
有 `--yes` 过闸、`--yes` 越不过 captcha。
