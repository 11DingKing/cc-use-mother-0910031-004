"""可替换的时钟，便于测试确定时间。"""
from __future__ import annotations

from datetime import datetime, timezone


class Clock:
    def __init__(self, fixed: datetime | str | None = None):
        if isinstance(fixed, str):
            fixed = datetime.fromisoformat(fixed)
        self._fixed = fixed

    def freeze(self, value: datetime | str | None) -> None:
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        self._fixed = value

    def reset(self) -> None:
        self._fixed = None

    def now_dt(self) -> datetime:
        value = self._fixed or datetime.now(timezone.utc)
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def now(self) -> str:
        return self.now_dt().isoformat()
