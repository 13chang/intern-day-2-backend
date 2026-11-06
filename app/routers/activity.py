"""活动助手接口：设置、当天安排、三个操作、周统计、Agent 建议与确认。

错误约定沿用本仓库既有风格：``raise HTTPException(status_code, detail="中文")``，
前端 ``api.ts`` 的 ``extractDetail()`` 直接显示 ``detail``。
领域规则抛的 ``RuleViolation`` 在这里统一翻成 HTTP，路由本身不做判断。
"""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.activity import rules, service
from app.deps import get_current_user

router = APIRouter(prefix="/activity", tags=["activity"])

HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"
ActivityType = str  # walk / water / stretch / stand，非法值由服务层的目录校验兜住

# 领域错误 → HTTP 状态。规则层只关心"哪里不合法"，状态码是接口层的决定。
_STATUS_BY_CODE = {
    "NOT_FOUND": status.HTTP_404_NOT_FOUND,
    "INVALID_STATE": status.HTTP_409_CONFLICT,
    "NO_SLOT_AVAILABLE": status.HTTP_409_CONFLICT,
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


class SuggestRequest(BaseModel):
    count: int | None = Field(default=None, ge=1, le=8)
    note: str | None = Field(default=None, max_length=200, description="自然语言偏好")


class SuggestOut(BaseModel):
    suggestions: list[DraftOut]
    explanation: str
    notice: str = ""
    mode: str = "rule"


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


# ---------------------------------------------------------------- Agent 建议

@router.post("/suggestions", response_model=SuggestOut, summary="生成安排建议（不保存）")
def suggestions(
    payload: SuggestRequest | None = None,
    user: dict = Depends(get_current_user),
) -> dict:
    """只产出草稿，不写库。

    保存必须由用户在页面上点"确认"走 ``/activity/confirm``，
    这样"Agent 建议"和"用户手填时间"会经过同一条校验。
    """
    count = payload.count if payload else None
    note = payload.note if payload else None
    try:
        drafts, feasible, settings = service.suggest_day(user["id"], count)
    except rules.RuleViolation as exc:
        raise _http(exc) from exc

    suggestions_out = [
        {
            "scheduled_at": rules.iso(draft.at),
            "activity_type": draft.activity_type,
            "title": draft.title,
            "description": draft.description,
            "duration_min": draft.duration_min,
            "reason": draft.reason,
        }
        for draft in drafts
    ]

    prefix = f"考虑到你说的「{note}」，" if note else ""
    explanation = (
        f"{prefix}按你的可活动时段排了 {feasible} 次，已经避开免打扰时间。"
    )
    notice = ""
    if feasible < (count or settings["daily_target"]):
        notice = f"可用时间只够安排 {feasible} 次。"
    return {
        "suggestions": suggestions_out,
        "explanation": explanation,
        "notice": notice,
        "mode": "rule",
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
