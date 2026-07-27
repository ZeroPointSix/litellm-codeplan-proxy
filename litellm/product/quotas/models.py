from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class QuotaEventType(str, Enum):
    RESERVE = "reserve"
    SETTLE = "settle"
    RELEASE = "release"
    PENDING_USAGE = "pending_usage"
    EXPIRED = "expired"
    COMPENSATED = "compensated"


class ReservationState(str, Enum):
    RESERVED = "RESERVED"
    SETTLED = "SETTLED"
    RELEASED = "RELEASED"
    PENDING_USAGE = "PENDING_USAGE"
    EXPIRED = "EXPIRED"
    COMPENSATED = "COMPENSATED"


class CreditUsage(BaseModel):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)

    model_config = ConfigDict(extra="forbid")


class CreditMultipliers(BaseModel):
    input_multiplier: float = Field(default=1.0, ge=0)
    output_multiplier: float = Field(default=1.0, ge=0)
    cache_read_multiplier: float = Field(default=0.0, ge=0)
    cache_write_multiplier: float = Field(default=0.0, ge=0)

    model_config = ConfigDict(extra="forbid")


class QuotaWindow(BaseModel):
    name: str = Field(min_length=1)
    limit: float = Field(gt=0)
    ttl_seconds: int = Field(gt=0)
    period_id: str = Field(min_length=1)
    period_end_epoch: int = Field(gt=0)
    anchor_epoch: int | None = None
    starts_on_first_success: bool = False

    model_config = ConfigDict(extra="forbid")

    @field_validator("name", "period_id")
    @classmethod
    def clean_required_string(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value is required")
        return normalized


class QuotaReserveRequest(BaseModel):
    request_id: str = Field(min_length=1)
    subscription_id: str = Field(min_length=1)
    project_id: str | None = None
    input_usage: CreditUsage
    multipliers: CreditMultipliers
    windows: list[QuotaWindow] = Field(min_length=1)
    default_max_output_tokens: int = Field(ge=0)

    model_config = ConfigDict(extra="forbid")


class QuotaSettlementRequest(BaseModel):
    request_id: str = Field(min_length=1)
    subscription_id: str = Field(min_length=1)
    project_id: str | None = None
    actual_usage: CreditUsage
    multipliers: CreditMultipliers
    windows: list[QuotaWindow] = Field(min_length=1)
    reserved_credits: float = Field(ge=0)
    event_type: QuotaEventType = QuotaEventType.SETTLE

    model_config = ConfigDict(extra="forbid")


class QuotaDecision(BaseModel):
    allowed: bool
    request_id: str
    subscription_id: str
    project_id: str | None = None
    event_type: QuotaEventType
    credits: float = Field(ge=0)
    balances_before: dict[str, float] = Field(default_factory=dict)
    balances_after: dict[str, float] = Field(default_factory=dict)
    idempotent: bool = False
    reason: str | None = None
    metadata: dict[str, object] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")
