from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from litellm.product.subscriptions.models import PlanSnapshot


class PortalSubscription(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subscription_id: str
    project_id: str
    plan_id: str
    status: str
    plan_snapshot: PlanSnapshot
    expires_at: datetime | None = None
    renewed_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    version: int
    key_count: int
    max_keys: int


class PortalSubscriptionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subscription: PortalSubscription


class PortalQuotaWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    limit: float
    used: float
    remaining: float
    period_id: str
    reset_at: datetime
    reset_at_local: str
    seconds_until_reset: int
    starts_on_first_success: bool = False
    anchor_at: datetime | None = None
    anchor_at_local: str | None = None


class PortalQuotaResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subscription_id: str
    project_id: str
    timezone: str
    generated_at: datetime
    generated_at_local: str
    windows: list[PortalQuotaWindow]


class PortalUsageBucket(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: str
    request_count: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    external_credits: float


class PortalUsageTotals(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_count: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    external_credits: float


class PortalUsageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subscription_id: str
    project_id: str
    timezone: str
    start_time: datetime
    end_time: datetime
    buckets: list[PortalUsageBucket]
    totals: PortalUsageTotals


class PortalRequestRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None
    request_duration_ms: float | None = None
    model: str | None = None
    api_base: str | None = None
    call_type: str | None = None
    status: str | None = None
    cache_hit: bool | str | None = None
    input_tokens: int
    output_tokens: int
    total_tokens: int
    external_credits: float


class PortalRequestListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subscription_id: str
    project_id: str
    start_time: datetime
    end_time: datetime
    page: int
    page_size: int
    total: int
    total_pages: int
    data: list[PortalRequestRecord]


class PortalKeyRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key_id: str
    key_alias: str | None = None
    models: list[str]
    allowed_routes: list[str]
    blocked: bool = False
    expires_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_active: datetime | None = None


class PortalKeyListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subscription_id: str
    project_id: str
    data: list[PortalKeyRecord]


class PortalKeyCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key_alias: str | None = Field(default=None, min_length=1, max_length=128)
    metadata: dict[str, object] = Field(default_factory=dict)

    @field_validator("key_alias")
    @classmethod
    def clean_key_alias(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("key_alias cannot be blank")
        return cleaned


class PortalKeyCreateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subscription: PortalSubscription
    key_id: str
    token_id: str
    key: str


class PortalKeyRevokeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subscription: PortalSubscription
    revoked_key_id: str


class PortalIntegrationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subscription_id: str
    project_id: str
    base_url: str
    api_base_url: str
    allowed_models: list[str]
    allowed_routes: list[str]
    sample_config: dict[str, object]
