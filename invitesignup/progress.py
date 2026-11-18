"""Progress accounting and stable failure codes.

Two problems this solves:

* A probe takes ~40 s and a run can wait two minutes for mail.  "stage=email"
  tells you *where* you are but not *how far along*, and a UI cannot draw a
  bar from a stage name alone.
* Failures need a stable identifier.  "the site refused this mailbox" is good
  prose but a poor thing to grep for or to attach a fix to.  Every failure
  kind gets a short code that never changes meaning.

The weights are not measured timings -- they are a rough sense of how much of
the work each stage represents, so the bar moves at a believable pace instead
of jumping.  Anything that claims to be a percentage of an unknown duration is
an approximation; this one says so.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

# (stage, human label, weight)
Stage = Tuple[str, str, int]

PROBE_STAGES: List[Stage] = [
    ("landing", "打开邀请页", 8),
    ("chunks", "扫描前端代码找接口", 30),
    ("config", "读取站点公开配置", 7),
    ("dom", "读取表单结构", 40),
    ("verdict", "生成判定", 15),
]

RUN_STAGES: List[Stage] = [
    ("landing", "打开邀请页", 8),
    ("email", "填写邮箱", 10),
    ("password", "设置密码", 10),
    ("submit", "提交注册", 12),
    ("await-verification-link", "等待验证邮件", 28),
    ("await-verification-code", "等待验证码", 28),
    ("confirm-link", "打开验证链接", 14),
    ("confirm", "检查登录状态", 10),
    ("state", "读取账户额度", 8),
]

VERIFY_STAGES: List[Stage] = [
    ("sign-in", "重新登录", 50),
    ("done", "核对账户状态", 50),
]

STAGE_SETS = {"probe": PROBE_STAGES, "run": RUN_STAGES, "verify": VERIFY_STAGES}

# Human labels for stages that appear in events but are not part of the happy
# path, so a UI can say "已被拦截" instead of showing a raw identifier.
TERMINAL_LABELS: Dict[str, str] = {
    "blocked-by-verdict": "判定不允许执行",
    "blocked-by-captcha": "遇到验证码，已停止",
    "blocked-by-quota": "邀请名额已用完",
    "skipped-duplicate": "今天已经成功过",
    "mailbox-failed": "创建临时邮箱失败",
    "mailbox-rejected": "站点拒绝了这个邮箱",
}


def labels(kind: str) -> Dict[str, str]:
    return {stage: label for stage, label, _ in STAGE_SETS.get(kind, [])}


def percent(kind: str, stage: str) -> int:
    """How far through ``kind`` the given stage is, 0-100.

    An unknown stage returns 0 rather than guessing: a bar that jumps to 90%
    on a stage the table has never heard of would be worse than a bar that
    stays put.
    """
    stages = STAGE_SETS.get(kind)
    if not stages:
        return 0
    total = sum(weight for _, _, weight in stages) or 1
    done = 0
    for name, _, weight in stages:
        if name == stage:
            # A stage counts as "reached", not "finished", so the bar shows
            # the beginning of that step rather than overshooting it.
            return int(done * 100 / total)
        done += weight
    return 0


def percent_within(kind: str, stage: str, fraction: float) -> int:
    """Interpolate inside a stage, for a step that reports its own progress.

    Scanning 25 bundles is one stage that can take half a minute.  Without
    interpolation the bar sits at the stage's start percentage and looks
    hung; with it the bar creeps forward while the work actually happens.
    """
    stages = STAGE_SETS.get(kind)
    if not stages:
        return 0
    total = sum(weight for _, _, weight in stages) or 1
    done = 0
    for name, _, weight in stages:
        if name == stage:
            fraction = max(0.0, min(1.0, fraction))
            return int((done + weight * fraction) * 100 / total)
        done += weight
    return 0


def describe(kind: str, stage: str) -> str:
    """A human label for a stage, falling back to the terminal table."""
    for name, label, _ in STAGE_SETS.get(kind, []):
        if name == stage:
            return label
    return TERMINAL_LABELS.get(stage, stage)


def plan(kind: str) -> List[Dict[str, object]]:
    """The full stage list with labels and percentages, for a UI checklist."""
    stages = STAGE_SETS.get(kind, [])
    total = sum(weight for _, _, weight in stages) or 1
    out: List[Dict[str, object]] = []
    done = 0
    for name, label, weight in stages:
        out.append({
            "stage": name,
            "label": label,
            "percent": int(done * 100 / total),
            "weight": weight,
        })
        done += weight
    return out


# Stable failure codes.  These never change meaning; new failures get new
# codes rather than reusing an existing one.
FAILURE_CODES: Dict[str, str] = {
    "transport": "E100",
    "rate_limit": "E101",
    "mail_timeout": "E200",
    "mailbox_rejected": "E201",
    "site_rejected": "E300",
    "captcha": "E301",
    "verification": "E302",
    "config": "E400",
    "unknown": "E900",
}

# What the operator can actually do about each code.  A code without a next
# step is just a number.
FAILURE_HINTS: Dict[str, str] = {
    "E100": "网络或连接问题。发信前可以重试；发信后不要重试（站点可能已建号）。",
    "E101": "被限速。等一会儿再试，或换出口 IP。",
    "E200": "没等到验证邮件。先看 mail dump：信到了但解析失败，和没到是两回事。",
    "E201": "站点拒绝了这个邮箱域名。用 --email 换真实邮箱，或加 --wait-link 走半自动。不要重试同一地址。",
    "E300": "站点拒绝了这次请求（账号已存在 / 邀请码无效 / 条款拒绝）。整批停，不要换号补号。",
    "E301": "遇到验证码。本工具不绕过，请在浏览器里手动完成。",
    "E302": "账号可能建成了，但没能证明。用 verify 单独确认一次。",
    "E400": "profile 不完整或过期。重新 probe 一次，或手工补 selectors / endpoints。",
    "E900": "未分类错误。先看截图和 page_text，再决定要不要重试。",
}


def failure_code(kind: str) -> str:
    return FAILURE_CODES.get(kind, "E900")


def failure_hint(kind: str) -> str:
    return FAILURE_HINTS.get(failure_code(kind), "")
