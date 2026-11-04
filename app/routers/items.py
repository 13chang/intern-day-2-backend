"""自选功能：按用户隔离的待办清单。

所有接口都通过 get_current_user 校验登录状态，未登录一律 401。
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from app.db import get_conn
from app.deps import get_current_user

router = APIRouter(prefix="/items", tags=["items"])

NOT_FOUND = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND,
    detail="记录不存在",
)


class ItemCreate(BaseModel):
    title: str = Field(min_length=1, max_length=100, description="待办内容")


class ItemUpdate(BaseModel):
    done: bool


class ItemOut(BaseModel):
    id: int
    title: str
    done: bool
    created_at: int


@router.get("", response_model=list[ItemOut], summary="列出当前用户的待办")
def list_items(user: dict = Depends(get_current_user)) -> list[ItemOut]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, title, done, created_at
            FROM items
            WHERE user_id = ?
            ORDER BY done ASC, id DESC
            """,
            (user["id"],),
        ).fetchall()

    return [
        ItemOut(
            id=row["id"],
            title=row["title"],
            done=bool(row["done"]),
            created_at=row["created_at"],
        )
        for row in rows
    ]


@router.post(
    "",
    response_model=ItemOut,
    status_code=status.HTTP_201_CREATED,
    summary="新增待办",
)
def create_item(payload: ItemCreate, user: dict = Depends(get_current_user)) -> ItemOut:
    title = payload.title.strip()
    if not title:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="待办内容不能为空",
        )

    created_at = int(time.time())
    with get_conn() as conn:
        cursor = conn.execute(
            "INSERT INTO items (user_id, title, done, created_at) VALUES (?, ?, 0, ?)",
            (user["id"], title, created_at),
        )
        item_id = int(cursor.lastrowid)

    return ItemOut(id=item_id, title=title, done=False, created_at=created_at)


@router.patch("/{item_id}", response_model=ItemOut, summary="切换完成状态")
def update_item(
    item_id: int,
    payload: ItemUpdate,
    user: dict = Depends(get_current_user),
) -> ItemOut:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, title, done, created_at FROM items WHERE id = ? AND user_id = ?",
            (item_id, user["id"]),
        ).fetchone()
        if row is None:
            raise NOT_FOUND
        conn.execute(
            "UPDATE items SET done = ? WHERE id = ?",
            (1 if payload.done else 0, item_id),
        )

    return ItemOut(
        id=row["id"],
        title=row["title"],
        done=payload.done,
        created_at=row["created_at"],
    )


@router.delete(
    "/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除待办",
)
def delete_item(item_id: int, user: dict = Depends(get_current_user)) -> Response:
    with get_conn() as conn:
        cursor = conn.execute(
            "DELETE FROM items WHERE id = ? AND user_id = ?",
            (item_id, user["id"]),
        )
        if cursor.rowcount == 0:
            raise NOT_FOUND

    return Response(status_code=status.HTTP_204_NO_CONTENT)
