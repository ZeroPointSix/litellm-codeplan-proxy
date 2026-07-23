from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from litellm.product.plans.models import PlanEntitlements


class SubscriptionStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    CANCELED = "canceled"
    EXPIRED = "expired"


class PlanSnapshot(PlanEntitlements):
    plan_id: str
    name: str
    description: Optional[str] = None
    plan_version: int = Field(ge=1)
    credit_rule_version: Optional[int] = Field(default=None, ge=1)
    captured_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    model_config = ConfigDict(extra="forbid")


class SubscriptionCreateRequest(BaseModel):
    project_id: str = Field(min_length=1)
    plan_id: str = Field(min_length=1)
    expires_at: Optional[datetime] = None
    issue_key: bool = True
    key_alias: Optional[str] = Field(default=None, min_length=1, max_length=255)
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")

    @field_validator("project_id", "plan_id", "key_alias")
    @classmethod
    def clean_optional_strings(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        normalized = value.strip()
        if not normalized:
            raise ValueError("value is required")
        return normalized


class SubscriptionActionRequest(BaseModel):
    version: int = Field(ge=1)

    model_config = ConfigDict(extra="forbid")


class SubscriptionRenewRequest(SubscriptionActionRequest):
    expires_at: Optional[datetime] = None
    issue_key: bool = True
    key_alias: Optional[str] = Field(default=None, min_length=1, max_length=255)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("key_alias")
    @classmethod
    def clean_key_alias(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        normalized = value.strip()
        if not normalized:
            raise ValueError("key_alias is required")
        return normalized


class SubscriptionUpgradeRequest(SubscriptionRenewRequest):
    plan_id: str = Field(min_length=1)

    @field_validator("plan_id")
    @classmethod
    def clean_plan_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("plan_id is required")
        return normalized


class SubscriptionIssueKeyRequest(SubscriptionActionRequest):
    key_alias: Optional[str] = Field(default=None, min_length=1, max_length=255)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("key_alias")
    @classmethod
    def clean_key_alias(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        normalized = value.strip()
        if not normalized:
            raise ValueError("key_alias is required")
        return normalized


class SubscriptionRevokeKeyRequest(SubscriptionActionRequest):
    key_id: str = Field(min_length=1)

    @field_validator("key_id")
    @classmethod
    def clean_key_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("key_id is required")
        return normalized


class SubscriptionRecord(BaseModel):
    subscription_id: str
    project_id: str
    plan_id: str
    status: SubscriptionStatus = SubscriptionStatus.ACTIVE
    plan_snapshot: PlanSnapshot
    litellm_team_id: Optional[str] = None
    litellm_key_ids: list[str] = Field(default_factory=list)
    expires_at: Optional[datetime] = None
    renewed_at: Optional[datetime] = None
    paused_at: Optional[datetime] = None
    canceled_at: Optional[datetime] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    version: int = Field(ge=1)
    created_at: Optional[datetime] = None
    created_by: Optional[str] = None
    updated_at: Optional[datetime] = None
    updated_by: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)

    @model_validator(mode="after")
    def validate_plan_id_matches_snapshot(self) -> "SubscriptionRecord":
        if self.plan_id != self.plan_snapshot.plan_id:
            raise ValueError("plan_id must match plan_snapshot.plan_id")
        return self


class LiteLLMKeyProvision(BaseModel):
    key_id: str
    key: Optional[str] = None
    token_id: Optional[str] = None


class SubscriptionProvisionResponse(BaseModel):
    subscription: SubscriptionRecord
    key_id: Optional[str] = None
    key: Optional[str] = None
    token_id: Optional[str] = None


class SubscriptionListResponse(BaseModel):
    object: str = "list"
    data: list[SubscriptionRecord]
