"""活动助手接口：设置、当天安排、三个操作、周统计、Agent 建议与确认。

错误约定沿用本仓库既有风格：``raise HTTPException(status_code, detail="中文")``，
前端 ``api.ts`` 的 ``extractDetail()`` 直接显示 ``detail``。
领域规则抛的 ``RuleViolation`` 在这里统一翻成 HTTP，路由本身不做判断。
"""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.activity import planner, rules, service
from app.agent.config import load_llm_settings
from app.deps import get_current_user

router = APIRouter(prefix="/activity", tags=["activity"])

HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"
ActivityType = str  # walk / water / stretch / stand，非法值由服务层的目录校验兜住

# 领域错误 → HTTP 状态。规则层只关心"哪里不合法"，状态码是接口层的决定。
_STATUS_BY_CODE = {
    "NOT_FOUND": status.HTTP_404_NOT_FOUND,
    "INVALID_STATE": status.HTTP_409_CONFLICT,
    "NO_SLOT_AVAILABLE": status.HTTP_409_CONFLICT,
    # 用户选了真实模型引擎，但它排出来的时间全都不合条件：这是"当前排不出来"，
    # 不是请求写错了，所以用 409 而不是 422。
    "LLM_NO_VALID_SLOT": status.HTTP_409_CONFLICT,
}


def _http(exc: rules.RuleViolation) -> HTTPException:
    return HTTPException(
        status_code=_STATUS_BY_CODE.get(exc.code, status.HTTP_422_UNPROCESSABLE_ENTITY),
        detail=exc.message,
    )


# ---------------------------------------------------------------- 模型

class WindowIn(BaseModel):
    label: str = Field(default="window", max_length=20)
    start: str = Field(pattern=HHMM, description="HH:MM")
    end: str = Field(pattern=HHMM, description="HH:MM")


class SettingsIn(BaseModel):
    work_start: str = Field(default="09:00", pattern=HHMM)
    work_end: str = Field(default="18:00", pattern=HHMM)
    windows: list[WindowIn] = Field(default_factory=list)
    quiet_start: str | None = Field(default=None, pattern=HHMM)
    quiet_end: str | None = Field(default=None, pattern=HHMM)
    reminders_enabled: bool = True
    allow_during_work: bool = True
    daily_target: int = Field(default=3, ge=1, le=8)
    postpone_policy: str = Field(default="next_slot", pattern="^(next_slot|ask)$")


class SettingsOut(SettingsIn):
    user_id: int


class ActivityOut(BaseModel):
    id: int
    activity_type: str
    title: str
    description: str
    scheduled_at: str
    duration_min: int
    status: str
    source: str
    postponed_from: int | None = None
    postpone_count: int = 0
    skipped_reason: str | None = None


class GenerateOut(BaseModel):
    created: int
    activities: list[ActivityOut]
    notice: str = ""


class PostponeOut(BaseModel):
    original: ActivityOut
    replacement: ActivityOut


class DraftOut(BaseModel):
    """一条建议：没有 id，因为它还没进库。"""

    scheduled_at: str
    activity_type: str
    title: str
    description: str
    duration_min: int = 5
    reason: str = ""


class ConditionsIn(BaseModel):
    """本次生成用的条件，**不落库**。

    省略某个字段表示"沿用已保存的设置"；传了就只影响这一次生成。
    这样用户可以在建议面板里临时试试"如果下午不安排会怎样"，而不必改长期设置。
    """

    work_start: str | None = Field(default=None, pattern=HHMM)
    work_end: str | None = Field(default=None, pattern=HHMM)
    windows: list[WindowIn] | None = Field(default=None, description="传了就整份替换")
    quiet_start: str | None = Field(default=None, pattern=HHMM)
    quiet_end: str | None = Field(default=None, pattern=HHMM)
    ignore_quiet: bool = Field(default=False, description="这次不考虑免打扰（暂时关闭提醒）")


class SuggestRequest(BaseModel):
    engine: str = Field(default="rule", pattern="^(rule|llm)$", description="rule=本地规则，llm=真实模型")
    count: int | None = Field(default=None, ge=1, le=8)
    note: str | None = Field(default=None, max_length=200, description="自然语言偏好")
    conditions: ConditionsIn | None = None


class SuggestOut(BaseModel):
    suggestions: list[DraftOut]
    explanation: str
    notice: str = ""
    mode: str = "rule"
    dropped: int = Field(default=0, description="模型给出但被规则丢掉的条数")


class EngineOut(BaseModel):
    """一个可选的建议引擎，以及它现在能不能用。"""

    id: str
    label: str
    available: bool
    model: str | None = None
    reason: str


class EnginesOut(BaseModel):
    engines: list[EngineOut]


class ConfirmRequest(BaseModel):
    items: list[DraftOut] = Field(min_length=1, max_length=8)


class ConfirmOut(BaseModel):
    saved: int
    activities: list[ActivityOut]


class SkipRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=100)


# ---------------------------------------------------------------- 设置

@router.get("/settings", response_model=SettingsOut, summary="读取活动设置")
def read_settings(
    user: dict = Depends(get_current_user),
) -> dict:
    return service.get_settings(user["id"])


@router.put("/settings", response_model=SettingsOut, summary="保存活动设置")
def update_settings(
    payload: SettingsIn,
    user: dict = Depends(get_current_user),
) -> dict:
    try:
        return service.save_settings(user["id"], payload.model_dump())
    except rules.RuleViolation as exc:
        raise _http(exc) from exc


# ---------------------------------------------------------------- 当天安排

@router.get("/today", response_model=list[ActivityOut], summary="当天的活动安排")
def list_today(
    day: date | None = Query(default=None, description="不传就是今天"),
    user: dict = Depends(get_current_user),
) -> list[dict]:
    return service.list_day(user["id"], day or rules.now_local().date())


@router.post("/generate", response_model=GenerateOut, summary="生成当天安排")
def generate(
    force: bool = Query(default=False, description="true 时重建当天待执行安排"),
    user: dict = Depends(get_current_user),
) -> dict:
    try:
        return service.generate_day(user["id"], rules.now_local().date(), force)
    except rules.RuleViolation as exc:
        raise _http(exc) from exc


@router.post("/{activity_id}/complete", summary="标记完成")
def complete(activity_id: int, user: dict = Depends(get_current_user)) -> dict:
    try:
        return service.complete(user["id"], activity_id)
    except rules.RuleViolation as exc:
        raise _http(exc) from exc


@router.post("/{activity_id}/postpone", response_model=PostponeOut, summary="推迟到下一个可用时段")
def postpone(activity_id: int, user: dict = Depends(get_current_user)) -> dict:
    try:
        return service.postpone(user["id"], activity_id)
    except rules.RuleViolation as exc:
        raise _http(exc) from exc


@router.post("/{activity_id}/skip", summary="今天不方便")
def skip(
    activity_id: int,
    payload: SkipRequest | None = None,
    user: dict = Depends(get_current_user),
) -> dict:
    try:
        reason = payload.reason if payload else None
        return service.skip(user["id"], activity_id, reason)
    except rules.RuleViolation as exc:
        raise _http(exc) from exc


# ---------------------------------------------------------------- 统计

@router.get("/stats/weekly", summary="本周完成情况")
def weekly(
    week_start: date | None = Query(default=None, description="不传就是本周一"),
    user: dict = Depends(get_current_user),
) -> dict:
    today = rules.now_local().date()
    start = week_start or (today - timedelta(days=today.weekday()))
    return service.weekly_stats(user["id"], start)


# ---------------------------------------------------------------- 建议引擎

@router.get("/engines", response_model=EnginesOut, summary="可用的建议生成引擎")
def engines(user: dict = Depends(get_current_user)) -> dict:
    """告诉前端"能不能选 DeepSeek"。

    单独开一个接口而不是让前端读环境变量：前端拿不到后端的环境变量，
    而猜一个（"大概配了吧"）一旦猜错，用户点了按钮才看到报错，体验很差。
    """
    llm = load_llm_settings()
    return {
        "engines": [
            {
                "id": "rule",
                "label": "本地规则",
                "available": True,
                "model": None,
                "reason": "服务端规则算法，不联网、响应快、结论确定",
            },
            {
                "id": "llm",
                "label": "DeepSeek 真实模型",
                "available": not llm.is_mock,
                "model": llm.model,
                "reason": llm.reason,
            },
        ]
    }


# ---------------------------------------------------------------- Agent 建议

def _draft_out(draft: rules.Draft) -> dict:
    return {
        "scheduled_at": rules.iso(draft.at),
        "activity_type": draft.activity_type,
        "title": draft.title,
        "description": draft.description,
        "duration_min": draft.duration_min,
        "reason": draft.reason,
    }


@router.post("/suggestions", response_model=SuggestOut, summary="生成安排建议（不保存）")
def suggestions(
    payload: SuggestRequest | None = None,
    user: dict = Depends(get_current_user),
) -> dict:
    """只产出草稿，不写库。两个引擎共用这个入口。

    保存必须由用户在页面上点"确认"走 ``/activity/confirm``，
    这样"模型建议"和"用户手填时间"会经过同一条校验。
    """
    request = payload or SuggestRequest()
    conditions = request.conditions.model_dump() if request.conditions else None
    note = (request.note or "").strip()
    dropped = 0

    try:
        if request.engine == "llm":
            # 先按条件算出"今天还剩哪些可活动时段"，把这张时间表交给模型，
            # 让它只负责挑时间与活动，而不是自己算窗口减免打扰减冲突。
            settings = service.resolve_conditions(user["id"], conditions)
            now = rules.now_local()
            day = now.date()
            busy = service.planned_of_day(user["id"], day)
            count = request.count or settings["daily_target"]
            drafts, explanation, dropped = planner.plan(
                settings, day, busy, now, count, note
            )
            feasible = len(drafts)
        else:
            drafts, feasible, settings = service.suggest_day(
                user["id"], request.count, conditions
            )
            prefix = f"考虑到你说的「{note}」，" if note else ""
            explanation = f"{prefix}按你的可活动时段排了 {feasible} 次，已经避开免打扰时间。"
    except planner.LlmUnavailable as exc:
        # 用户明确选了真实模型，就如实报错，不要偷偷换成规则引擎
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except rules.RuleViolation as exc:
        raise _http(exc) from exc

    notice = ""
    if feasible < (request.count or settings["daily_target"]):
        notice = f"可用时间只够安排 {feasible} 次。"
    if dropped:
        notice = f"{f'模型有 {dropped} 条建议不满足条件，已丢弃。'}{notice}"

    return {
        "suggestions": [_draft_out(draft) for draft in drafts],
        "explanation": explanation,
        "notice": notice,
        "mode": request.engine,
        "dropped": dropped,
    }


@router.post("/confirm", response_model=ConfirmOut, summary="确认保存建议")
def confirm(
    payload: ConfirmRequest,
    user: dict = Depends(get_current_user),
) -> dict:
    try:
        return service.confirm(user["id"], [item.model_dump() for item in payload.items])
    except rules.RuleViolation as exc:
        raise _http(exc) from exc
