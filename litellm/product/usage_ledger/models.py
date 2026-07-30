from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class UsageLedgerEventType(str, Enum):
    RESERVE = "reserve"
    SETTLE = "settle"
    RELEASE = "release"
    REFUND = "refund"
    MANUAL_ADJUST = "manual_adjust"
    COMPENSATE = "compensate"
    EXPIRE = "expire"
    PENDING_USAGE = "pending_usage"


class UsageLedgerGroupBy(str, Enum):
    HOUR = "hour"
    DAY = "day"
    MODEL = "model"
    USER = "user"


class UsageLedgerCreate(BaseModel):
    request_id: str = Field(min_length=1)
    subscription_id: str = Field(min_length=1)
    project_id: str | None = None
    user_id: str | None = None
    api_key_id: str | None = None
    model: str | None = None
    event_type: UsageLedgerEventType
    credits: float
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    input_multiplier: float = Field(default=1.0, ge=0)
    output_multiplier: float = Field(default=1.0, ge=0)
    cache_read_multiplier: float = Field(default=0.0, ge=0)
    cache_write_multiplier: float = Field(default=0.0, ge=0)
    rule_version: int = Field(default=1, ge=1)
    window_5h_period_id: str | None = None
    window_5h_start: datetime | None = None
    window_5h_end: datetime | None = None
    window_week_period_id: str | None = None
    window_week_start: datetime | None = None
    window_week_end: datetime | None = None
    metadata: dict[str, object] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_credit_direction(self) -> UsageLedgerCreate:
        if self.credits < 0 and self.event_type != UsageLedgerEventType.MANUAL_ADJUST:
            raise ValueError("credits can only be negative for manual_adjust events")
        return self

    @field_validator("request_id", "subscription_id", "project_id", "user_id", "api_key_id", "model")
    @classmethod
    def clean_optional_strings(cls, value: str | None) -> str | None:
        if value is None:
            return value
        normalized = value.strip()
        if not normalized:
            raise ValueError("value is required")
        return normalized


class UsageLedgerRecord(UsageLedgerCreate):
    event_id: str
    created_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class UsageLedgerListResponse(BaseModel):
    object: str = "list"
    data: list[UsageLedgerRecord]
    has_more: bool = False
    limit: int | None = None
    offset: int | None = None


class UsageLedgerAggregateRecord(BaseModel):
    group: str
    credits: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    request_count: int = 0
    event_count: int = 0


class UsageLedgerAggregateResponse(BaseModel):
    object: str = "list"
    group_by: UsageLedgerGroupBy
    data: list[UsageLedgerAggregateRecord]


class UsageLedgerManualAdjustRequest(BaseModel):
    subscription_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    credits: float
    reason: str = Field(min_length=1)
    project_id: str | None = None
    user_id: str | None = None
    model: str | None = None
    rule_version: int = Field(default=1, ge=1)
    metadata: dict[str, object] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_manual_adjust_credits(self) -> UsageLedgerManualAdjustRequest:
        if self.credits == 0:
            raise ValueError("credits must not be zero")
        return self

    @field_validator("subscription_id", "request_id", "reason", "project_id", "user_id", "model")
    @classmethod
    def clean_strings(cls, value: str | None) -> str | None:
        if value is None:
            return value
        normalized = value.strip()
        if not normalized:
            raise ValueError("value is required")
        return normalized


class UsageLedgerQuery(BaseModel):
    request_id: str | None = None
    subscription_id: str | None = None
    project_id: str | None = None
    user_id: str | None = None
    model: str | None = None
    event_type: UsageLedgerEventType | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None
    limit: int = Field(default=100, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)


class UsageLedgerAggregateQuery(UsageLedgerQuery):
    group_by: UsageLedgerGroupBy = UsageLedgerGroupBy.DAY
