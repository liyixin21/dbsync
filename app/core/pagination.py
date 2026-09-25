"""
统一分页契约。

列表响应统一为：
    {"data": [...], "page": {"total": N, "skip": 0, "limit": 50}}
"""
from typing import Generic, List, Tuple, TypeVar

from pydantic import BaseModel
from sqlalchemy.orm import Query

T = TypeVar("T")

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


class PageMeta(BaseModel):
    total: int
    skip: int
    limit: int


class Page(BaseModel, Generic[T]):
    data: List[T]
    page: PageMeta


def clamp_skip(skip: int) -> int:
    return max(0, skip)


def clamp_limit(limit: int, maximum: int = MAX_LIMIT) -> int:
    return max(1, min(limit, maximum))


def paginate(query: Query, skip: int, limit: int, maximum: int = MAX_LIMIT) -> Tuple[list, PageMeta]:
    """对查询执行计数 + 分页。"""
    skip = clamp_skip(skip)
    limit = clamp_limit(limit, maximum)
    total = query.count()
    items = query.offset(skip).limit(limit).all()
    return items, PageMeta(total=total, skip=skip, limit=limit)
