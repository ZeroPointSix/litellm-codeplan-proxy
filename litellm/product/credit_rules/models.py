from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class CreditRuleStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    ARCHIVED = "archived"


class CreditRuleMultipliers(BaseModel):
    input_multiplier: float = Field(ge=0)
    output_multiplier: float = Field(ge=0)
    cache_read_multiplier: float = Field(ge=0)
    cache_write_multiplier: float = Field(ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_non_zero_rule(self) -> "CreditRuleMultipliers":
        multipliers = (
            self.input_multiplier,
            self.output_multiplier,
            self.cache_read_multiplier,
            self.cache_write_multiplier,
        )
        if not any(multiplier > 0 for multiplier in multipliers):
            raise ValueError("At least one credit multiplier must be greater than zero")
        return self


class CreditRuleCreateRequest(CreditRuleMultipliers):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    effective_at: datetime | None = None

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("name is required")
        return normalized


class CreditRulePatchRequest(BaseModel):
    version: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    input_multiplier: float | None = Field(default=None, ge=0)
    output_multiplier: float | None = Field(default=None, ge=0)
    cache_read_multiplier: float | None = Field(default=None, ge=0)
    cache_write_multiplier: float | None = Field(default=None, ge=0)
    effective_at: datetime | None = None
    metadata: dict[str, Any] | None = None

    model_config = ConfigDict(extra="forbid")

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str | None) -> str | None:
        if value is None:
            return value
        normalized = value.strip()
        if not normalized:
            raise ValueError("name is required")
        return normalized


class CreditRuleRecord(CreditRuleMultipliers):
    id: str
    credit_rule_id: str
    name: str
    description: str | None = None
    status: CreditRuleStatus = CreditRuleStatus.DRAFT
    version: int = Field(ge=1)
    effective_at: datetime | None = None
    created_at: datetime | None = None
    created_by: str | None = None
    updated_at: datetime | None = None
    updated_by: str | None = None

    model_config = ConfigDict(from_attributes=True)


class CreditRuleListResponse(BaseModel):
    object: str = "list"
    data: list[CreditRuleRecord]
