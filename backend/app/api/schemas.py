from typing import Literal

from pydantic import BaseModel, field_validator


class AnalystBody(BaseModel):
    analyst: str

    @field_validator("analyst")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("analyst must not be empty")
        return v


class FeedbackBody(AnalystBody):
    decision: Literal["confirmed_fraud", "false_positive"]
    note: str | None = None
