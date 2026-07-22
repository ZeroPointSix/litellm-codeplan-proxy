from datetime import datetime
from enum import Enum
from typing import Any, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class PlanStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    ARCHIVED = "archived"


class PlanEntitlements(BaseModel):
    quota_5h: int = Field(gt=0)
    quota_weekly: int = Field(gt=0)
    allowed_models: List[str] = Field(default_factory=list)
    rpm_limit: Optional[int] = Field(default=None, gt=0)
    tpm_limit: Optional[int] = Field(default=None, gt=0)
    max_parallel_requests: Optional[int] = Field(default=None, gt=0)
    max_keys: int = Field(default=5, ge=1)
    default_max_output_tokens: Optional[int] = Field(default=None, gt=0)
    credit_rule_id: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")

    @field_validator("allowed_models", mode="before")
    @classmethod
    def clean_allowed_models(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError("allowed_models must be a list")

        cleaned: list[str] = []
        seen: set[str] = set()
        for model_name in value:
            if not isinstance(model_name, str):
                raise ValueError("allowed_models must contain model names")
            normalized = model_name.strip()
            if normalized and normalized not in seen:
                cleaned.append(normalized)
                seen.add(normalized)
        return cleaned

    @model_validator(mode="after")
    def validate_quota_window(self) -> "PlanEntitlements":
        if self.quota_weekly < self.quota_5h:
            raise ValueError("quota_weekly must be greater than or equal to quota_5h")
        return self


class PlanCreateRequest(PlanEntitlements):
    name: str = Field(min_length=1, max_length=255)
    description: Optional[str] = None

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("name is required")
        return normalized


class PlanPatchRequest(BaseModel):
    version: int = Field(ge=1)
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    description: Optional[str] = None
    quota_5h: Optional[int] = Field(default=None, gt=0)
    quota_weekly: Optional[int] = Field(default=None, gt=0)
    allowed_models: Optional[List[str]] = None
    rpm_limit: Optional[int] = Field(default=None, gt=0)
    tpm_limit: Optional[int] = Field(default=None, gt=0)
    max_parallel_requests: Optional[int] = Field(default=None, gt=0)
    max_keys: Optional[int] = Field(default=None, ge=1)
    default_max_output_tokens: Optional[int] = Field(default=None, gt=0)
    credit_rule_id: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None

    model_config = ConfigDict(extra="forbid")

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        normalized = value.strip()
        if not normalized:
            raise ValueError("name is required")
        return normalized

    @field_validator("allowed_models", mode="before")
    @classmethod
    def clean_allowed_models(cls, value: Any) -> Optional[list[str]]:
        if value is None:
            return None
        return PlanEntitlements.clean_allowed_models(value)

    @model_validator(mode="after")
    def validate_patch_quota_window(self) -> "PlanPatchRequest":
        if self.quota_5h is not None and self.quota_weekly is not None and self.quota_weekly < self.quota_5h:
            raise ValueError("quota_weekly must be greater than or equal to quota_5h")
        return self


class PlanRecord(PlanEntitlements):
    plan_id: str
    name: str
    description: Optional[str] = None
    status: PlanStatus = PlanStatus.DRAFT
    version: int = Field(ge=1)
    created_at: Optional[datetime] = None
    created_by: Optional[str] = None
    updated_at: Optional[datetime] = None
    updated_by: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class PlanListResponse(BaseModel):
    object: str = "list"
    data: list[PlanRecord]
