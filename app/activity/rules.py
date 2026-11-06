"""活动安排的时间规则：可用时段、排程、校验。

为什么单独一层：

- 生成、推迟、Agent 建议、确认保存**四个入口**必须用同一套判断。写在路由里，
  迟早会出现"页面拦得住、Agent 绕得过去"；
- 这一层不 import 数据库、不 import fastapi，所以能脱离服务跑单测，
  也保证规则只有一份实现。

约定：时间一律是带时区的 aware datetime，落库转成带 +08:00 的 ISO 串。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

# 一段空闲短于这个值就不排（15 分钟连起身加水都不够）
MIN_ITEM_MIN = 15
# 与已有活动的最小间隔，避免两条安排挤在一起
BUFFER_MIN = 10
# 时间对齐粒度
STEP_MIN = 5
# 推迟至少往后挪这么久，否则"推迟"等于没动
POSTPONE_GAP_MIN = 30
# 一条活动的默认时长（分钟）
DEFAULT_DURATION_MIN = 5
# 最多能提前几天规划。再远排出来的东西没有参考价值（作息、会议都还没定），
# 而且"给三个月后的每天排三条"这种请求会把库撑爆。
MAX_PLAN_DAYS = 14

# 活动库：只用轻量、无医疗表述的说法。
# 任务书明确不要运动处方、治疗方案或需要专业指导的动作。
ACTIVITY_CATALOG: dict[str, tuple[str, str]] = {
    "walk": ("起身走动", "离开座位走一小段，再回来继续。"),
    "water": ("接水 / 去洗手间", "顺便走动一下，喝口水。"),
    "stretch": ("肩颈放松", "缓慢转动肩颈，做几个深呼吸。"),
    "stand": ("站立片刻", "站起来活动一下腿脚，再坐下。"),
}
ROTATION = ["walk", "stretch", "water", "stand"]

STATUS_LABELS = {
    "planned": "待执行",
    "completed": "已完成",
    "postponed": "已推迟",
    "skipped": "今天不方便",
}


class RuleViolation(ValueError):
    """规则不通过。

    ``code`` 给测试与路由层判断（转成 409/422），``message`` 是给人看的中文。
    继承 ValueError 是为了让"参数错"这种语义在调用侧也能被 except 到。
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------- 时间基础

def tz() -> timezone:
    """排程用的时区。

    容器默认 UTC，而使用者在东八区。不显式指定，算出来的时间会整体差 8 小时，
    而且"看起来都合理"，自查时最难发现。
    """
    raw = os.environ.get("APP_TZ_OFFSET_HOURS", "8")
    try:
        hours = int(str(raw).strip())
    except (TypeError, ValueError):
        hours = 8
    return timezone(timedelta(hours=hours))


def now_local() -> datetime:
    return datetime.now(tz())


def at_time(day: date, hhmm: str) -> datetime:
    hour, minute = hhmm.split(":")
    return datetime.combine(day, time(int(hour), int(minute)), tzinfo=tz())


def parse_dt(text: str) -> datetime:
    """解析存储的 ISO 串；没有时区的值按本地时区补齐，避免 aware/naive 比较直接报错。"""
    value = datetime.fromisoformat(text)
    return value if value.tzinfo else value.replace(tzinfo=tz())


def iso(value: datetime) -> str:
    """落库格式：2026-11-06T10:30:00+08:00。带偏移，换时区也不产生歧义。"""
    return value.isoformat(timespec="seconds")


WEEKDAY_LABELS = "一二三四五六日"


def day_label(day: date, today: date | None = None) -> str:
    """把日期说成人话：今天 / 明天 / 后天 / 11 月 8 日（周六）。

    提示语里写死"今天"在往前规划时全是错的：用户明明选了 11 月 8 日，
    报错却说"今天剩余的可活动时间不足"，读起来像系统没收到他选的日期。
    所有面向用户的日期措辞统一走这里。
    """
    base = today or now_local().date()
    span = (day - base).days
    weekday = WEEKDAY_LABELS[day.weekday()]
    if span == 0:
        return "今天"
    if span == 1:
        return "明天"
    if span == 2:
        return "后天"
    return f"{day.month} 月 {day.day} 日（周{weekday}）"


def ensure_plannable(day: date, today: date | None = None) -> None:
    """目标日期必须落在 [今天, 今天 + MAX_PLAN_DAYS]。

    过去的日期排不出东西（``free_ranges`` 会把整段都当成"已经过去"），
    与其让用户看到"没有可用时段"，不如直接说清楚是日期的问题。
    往前规划也要有个上限，否则一次请求就能写十几年的数据。
    """
    base = today or now_local().date()
    if day < base:
        raise RuleViolation(
            "PAST_DAY",
            f"{day.isoformat()} 已经过去了，不能给过去的日期安排活动。",
        )
    span = (day - base).days
    if span > MAX_PLAN_DAYS:
        latest = base + timedelta(days=MAX_PLAN_DAYS)
        raise RuleViolation(
            "DAY_TOO_FAR",
            f"最多只能规划到 {MAX_PLAN_DAYS} 天后（{latest.isoformat()}）。",
        )


# ---------------------------------------------------------------- 区间运算

Range = tuple[datetime, datetime]


def _merge(ranges: list[Range]) -> list[Range]:
    out: list[Range] = []
    for start, end in sorted(ranges):
        if out and start <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return out


def _subtract(ranges: list[Range], cut: Range) -> list[Range]:
    cut_start, cut_end = cut
    out: list[Range] = []
    for start, end in ranges:
        if cut_end <= start or cut_start >= end:
            out.append((start, end))
            continue
        if start < cut_start:
            out.append((start, cut_start))
        if cut_end < end:
            out.append((cut_end, end))
    return out


# ---------------------------------------------------------------- 时段计算

def allowed_ranges(settings: dict, day: date) -> list[Range]:
    """允许活动的区间 = 活动窗口 ∩ 工作时段。

    窗口写到工作时段以外的部分直接裁掉：用户把上午窗口填成 08:00-13:00，
    也不该在上班前或午休时被安排。
    """
    work_start = at_time(day, settings["work_start"])
    work_end = at_time(day, settings["work_end"])
    if work_end <= work_start:
        return []

    out: list[Range] = []
    for window in settings.get("windows") or []:
        start = max(at_time(day, window["start"]), work_start)
        end = min(at_time(day, window["end"]), work_end)
        if start < end:
            out.append((start, end))
    return _merge(out)


def describe_windows(settings: dict) -> str:
    """把时段列表写成人话，用在错误提示里。"""
    windows = settings.get("windows") or []
    if not windows:
        return "（没有设置可活动时段）"
    return "、".join(f"{item['start']}-{item['end']}" for item in windows)


def _minutes(hhmm: str) -> int:
    hour, minute = hhmm.split(":")
    return int(hour) * 60 + int(minute)


def _hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def allowed_or_raise(settings: dict, day: date) -> list[Range]:
    """``allowed_ranges`` 的非空版本：交集为空就直接说清是哪一侧越界。

    专门用来区分一类很误导的失败：工作时段 09:00-10:00 配可活动时段 11:00-17:00，
    交集是空集，**任何引擎**都不可能排出东西。

    只说"没有重叠"是不够的——用户刚改完可活动时段，看到这句话只会认为"我明明改了"。
    所以这里判断是哪一侧越界（工作结束太早 / 工作开始太晚 / 两头都错开），
    把该改的字段和该改成的时间点出来。
    """
    ranges = allowed_ranges(settings, day)
    if ranges:
        return ranges

    windows = settings.get("windows") or []
    if not windows:
        raise RuleViolation(
            "NO_ALLOWED_RANGE",
            "还没有设置可活动时段，任何时间都不在允许范围内。先添加至少一个可活动时段。",
        )

    work_start = settings["work_start"]
    work_end = settings["work_end"]
    work_span = f"工作时段 {work_start}-{work_end}"
    window_text = describe_windows(settings)
    earliest = min(_minutes(item["start"]) for item in windows)
    latest = max(_minutes(item["end"]) for item in windows)

    if _minutes(work_end) <= earliest:
        raise RuleViolation(
            "NO_ALLOWED_RANGE",
            f"{work_span} 与可活动时段 {window_text} 没有重叠：时段最早 {_hhmm(earliest)} 才开始，"
            f"而 {work_end} 就下班了。把工作结束时间改到 {_hhmm(earliest)} 之后，"
            f"或者把时段挪进 {work_start}-{work_end} 里。",
        )
    if _minutes(work_start) >= latest:
        raise RuleViolation(
            "NO_ALLOWED_RANGE",
            f"{work_span} 与可活动时段 {window_text} 没有重叠：时段最晚 {_hhmm(latest)} 就结束了，"
            f"而 {work_start} 才开始上班。把工作开始时间改到 {_hhmm(latest)} 之前，"
            f"或者把时段往后挪。",
        )
    raise RuleViolation(
        "NO_ALLOWED_RANGE",
        f"{work_span} 与可活动时段 {window_text} 没有重叠（时段分别在上班前和下班后），"
        f"任何时间都排不进去。把可活动时段挪进工作时段，或者放宽工作时段。",
    )


def quiet_ranges(settings: dict, day: date) -> list[Range]:
    """免打扰区间。

    ``start > end`` 视为跨天（例如 22:00-07:00）：拆成"当天 22:00 到次日零点"
    与"当天零点到 07:00"两段。当成一段判的话一条都判不出来，这是最隐蔽的错误。
    """
    quiet_start = settings.get("quiet_start")
    quiet_end = settings.get("quiet_end")
    if not quiet_start or not quiet_end:
        return []

    start = at_time(day, quiet_start)
    end = at_time(day, quiet_end)
    if start < end:
        return [(start, end)]

    midnight = datetime.combine(day, time(0, 0), tzinfo=tz())
    return [(start, midnight + timedelta(days=1)), (midnight, end)]


def free_ranges(
    settings: dict,
    day: date,
    busy: list[dict],
    now: datetime,
    ignore_id: int | None = None,
) -> list[Range]:
    """可用区间 = 允许区间 − 免打扰 − 已占用(±BUFFER) − 已经过去的时间。

    ``ignore_id`` 是推迟场景必需的：算"后面还有没有位置"要把自己排除掉，
    否则永远和自己原来的时间冲突，表现为"今天永远没有可用时段"。
    """
    # 用非空版本：交集为空是"条件自相矛盾"，不是"今天排满了"，提示语完全不同
    ranges = allowed_or_raise(settings, day)
    for quiet in quiet_ranges(settings, day):
        ranges = _subtract(ranges, quiet)

    for activity in busy:
        if ignore_id is not None and activity.get("id") == ignore_id:
            continue
        # 以已有活动为中心左右各让开 gap_needed：
        # 校验用的是"两个起点之间的绝对间隔"，所以这里必须对称地扣，
        # 只扣一侧就会出现"算得出、存不进"的落点。
        pad = timedelta(minutes=gap_needed(activity))
        center = parse_dt(activity["scheduled_at"])
        ranges = _subtract(ranges, (center - pad, center + pad))

    floor = now + timedelta(minutes=1)
    out: list[Range] = []
    for start, end in ranges:
        start = max(start, floor)
        if start < end:
            out.append((start, end))
    return out


def _align_up(value: datetime) -> datetime:
    extra = (STEP_MIN - value.minute % STEP_MIN) % STEP_MIN
    return (value + timedelta(minutes=extra)).replace(second=0, microsecond=0)


def _align_down(value: datetime) -> datetime:
    extra = value.minute % STEP_MIN
    return (value - timedelta(minutes=extra)).replace(second=0, microsecond=0)


def _align_within(ranges: list[Range], moment: datetime, need: int) -> datetime | None:
    """把 ``moment`` 对齐到 STEP_MIN，且保证对齐后的整段仍在它所属的可用区间内。

    只写 ``_align_up`` 是不够的：它是向上取整，等分中点落在区间末尾前几分钟时
    （例如中点 14:57、这段区间到 15:00 结束），取整会跳到 15:00 —— 正好是免打扰
    的起点。结果是"生成看着正常、确认保存时被规则拒掉"，而生成和校验各自看都自洽，
    复现还依赖具体时刻，属于最难自查的一类。
    越界就退回区间内最后一个对齐点；连退回都放不下这段时长，就放弃这一条。
    """
    for start, end in ranges:
        if not (start <= moment < end):
            continue
        aligned = _align_up(moment)
        if aligned + timedelta(minutes=need) <= end:
            return aligned
        back = _align_down(moment)
        if start <= back and back + timedelta(minutes=need) <= end:
            return back
        return None
    return None


def gap_needed(activity: dict) -> int:
    """与某条已有活动之间至少要隔开多少分钟。

    单一来源：``free_ranges``（排程、找空位）和 ``validate_slot``（四个入口的校验）
    都调它。之前两边各写一份，左边按 BUFFER_MIN、右边按 时长+BUFFER_MIN，
    结果**排程能算出落点、保存时却被判冲突** —— 表现为"建议了两条，确认第一条就 422"。
    """
    return int(activity.get("duration_min") or 5) + BUFFER_MIN


def _at_offset(ranges: list[Range], minutes: float) -> datetime | None:
    """在总长度里取第 N 分钟对应的时刻。

    右端点必须算"区间外"：``remaining == length`` 时说明这个偏移正好落在区间末尾，
    那是下一段禁区（免打扰或已有活动）的起点，不是可选落点。用 ``<=`` 会把它当成
    合法值返回，后面即使对齐也救不回来。
    """
    remaining = minutes
    for start, end in ranges:
        length = (end - start).total_seconds() / 60
        if remaining < length:
            return start + timedelta(minutes=remaining)
        remaining -= length
    return None


# ---------------------------------------------------------------- 排程

@dataclass
class Draft:
    """一条尚未保存的安排。没有 id，因为它还没进库。"""

    at: datetime
    activity_type: str
    title: str
    description: str
    duration_min: int = DEFAULT_DURATION_MIN
    reason: str = ""


def plan_day(
    settings: dict,
    day: date,
    busy: list[dict],
    now: datetime,
    count: int,
) -> tuple[list[Draft], int]:
    """在可用总时长里等分取中点。

    返回 ``(草稿, 实际条数)``。放不下就少排——不要把 15 分钟切成两条 7 分钟，
    也不要为了凑数把活动塞进免打扰；条数不足由调用方如实告诉用户。
    """
    free = free_ranges(settings, day, busy, now)
    total = int(sum((end - start).total_seconds() for start, end in free) // 60)
    capacity = total // MIN_ITEM_MIN
    # 措辞跟着目标日期走：往前规划时写"今天"会让人以为日期没生效
    label = day_label(day, now.date())
    if capacity <= 0:
        raise RuleViolation(
            "NO_SLOT_AVAILABLE",
            f"{label}剩余的可活动时间不足，无法安排活动。可以放宽可活动时段，或减少每天次数。",
        )

    want = min(max(int(count), 1), capacity)
    per = total / want
    drafts: list[Draft] = []
    for index in range(want):
        moment = _at_offset(free, per * (index + 0.5))
        if moment is None:
            continue
        moment = _align_within(free, moment, DEFAULT_DURATION_MIN)
        if moment is None:
            continue
        # 等分后仍可能挨得太近（窗口很长、条数很少），按最小间隔二次过滤
        if drafts and (moment - drafts[-1].at) < timedelta(minutes=MIN_ITEM_MIN):
            continue
        activity_type = ROTATION[len(drafts) % len(ROTATION)]
        title, description = ACTIVITY_CATALOG[activity_type]
        drafts.append(
            Draft(
                at=moment,
                activity_type=activity_type,
                title=title,
                description=description,
                reason="按你的可活动时段平均分布",
            )
        )

    # 最后一道闸：排出来的每一条都必须能通过保存时的校验。
    # "生成"和"确认保存"是两个入口，共用同一套规则；这里把不变式钉死，
    # 以后改 free_ranges / plan_day 也不会再出现"排得出、存不进"。
    kept: list[Draft] = []
    for draft in drafts:
        try:
            validate_slot(settings, draft.at, day, busy, now)
        except RuleViolation:
            continue
        kept.append(draft)
    drafts = kept

    if not drafts:
        raise RuleViolation("NO_SLOT_AVAILABLE", f"{label}剩余的可活动时间不足，无法安排活动。")
    return drafts, len(drafts)


def next_slot(
    settings: dict,
    day: date,
    busy: list[dict],
    not_before: datetime,
    now: datetime,
    ignore_id: int | None = None,
) -> datetime | None:
    """推迟用：找 ``not_before`` 之后第一个够长的空闲起点。"""
    free = free_ranges(settings, day, busy, max(now, not_before), ignore_id=ignore_id)
    for start, end in free:
        if end - start >= timedelta(minutes=MIN_ITEM_MIN):
            return _align_up(start)
    return None


# ---------------------------------------------------------------- 校验

def validate_slot(
    settings: dict,
    moment: datetime,
    day: date,
    busy: list[dict],
    now: datetime,
    ignore_id: int | None = None,
) -> None:
    """四个入口共用的校验。不通过抛 RuleViolation，通过则静默返回。

    检查顺序按"用户最容易理解"排：过去时间 → 不在窗口 → 免打扰 → 与已有冲突。
    """
    if moment < now - timedelta(minutes=1):
        raise RuleViolation("PAST_TIME", f"{moment:%H:%M} 已经过去了，请选择之后的时间。")

    if not any(start <= moment < end for start, end in allowed_ranges(settings, day)):
        raise RuleViolation(
            "OUTSIDE_WINDOW",
            f"{moment:%H:%M} 不在可活动时段内，"
            f"请放在 {settings['work_start']}-{settings['work_end']} 的活动窗口里。",
        )

    for start, end in quiet_ranges(settings, day):
        if start <= moment < end:
            raise RuleViolation(
                "QUIET_HOURS",
                f"{moment:%H:%M} 落在免打扰时段 {start:%H:%M}-{end:%H:%M} 内，请换一个时间。",
            )

    for activity in busy:
        if ignore_id is not None and activity.get("id") == ignore_id:
            continue
        gap = abs((parse_dt(activity["scheduled_at"]) - moment).total_seconds()) / 60
        need = gap_needed(activity)
        if gap < need:
            raise RuleViolation(
                "CONFLICT",
                f"{moment:%H:%M} 与已有活动（{parse_dt(activity['scheduled_at']):%H:%M}）"
                f"间隔太近，至少需要 {need} 分钟。",
            )
