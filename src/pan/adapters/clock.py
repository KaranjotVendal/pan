from __future__ import annotations

import uuid
from datetime import UTC, datetime


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(tz=UTC)


class UuidGen:
    def new_id(self) -> str:
        return str(uuid.uuid4())
