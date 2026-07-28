from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import ceil
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException, status

from litellm.product.me.models import (
    PortalIntegrationResponse,
    PortalKeyCreateRequest,
    PortalKeyCreateResponse,
    PortalKeyListResponse,
    PortalKeyRevokeResponse,
    PortalQuotaResponse,
    PortalQuotaWindow,
    PortalRequestListResponse,
    PortalRequestRecord,
    PortalSubscription,
    PortalSubscriptionResponse,
    PortalUsageBucket,
    PortalUsageResponse,
    PortalUsageTotals,
)
from litellm.product.me.repository import PortalRepository
from litellm.product.quotas.models import CreditMultipliers, CreditUsage, QuotaWindow
from litellm.product.quotas.service import calculate_credits, quota_windows_from_metadata
from litellm.product.subscriptions.litellm_client import CODE_PLAN_GATEWAY_ALLOWED_ROUTES
from litellm.product.subscriptions.models import (
    SubscriptionIssueKeyRequest,
    SubscriptionRecord,
    SubscriptionRevokeKeyRequest,
)
from litellm.product.subscriptions.repository import SubscriptionRepository
from litellm.product.subscriptions.service import SubscriptionService
from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth


class QuotaReader(Protocol):
    async def resolve_windows(
        self,
        subscription_id: str,
        windows: list[QuotaWindow],
    ) -> list[QuotaWindow]: ...

    async def get_window_spent(
        self,
        subscription_id: str,
        windows: list[QuotaWindow],
    ) -> dict[str, float]: ...


@dataclass(frozen=True)
class PortalScope:
    project_ids: list[str]
    litellm_team_id: str | None


class PortalService:
    def __init__(
        self,
        subscription_repository: SubscriptionRepository,
        subscription_service: SubscriptionService,
        portal_repository: PortalRepository,
        quota_reader: QuotaReader | None = None,
    ):
        self.subscription_repository = subscription_repository
        self.subscription_service = subscription_service
        self.portal_repository = portal_repository
        self.quota_reader = quota_reader

    async def get_subscription(
        self,
        user_api_key_dict: UserAPIKeyAuth,
    ) -> PortalSubscriptionResponse:
        record = await self._current_subscription(user_api_key_dict)
        return PortalSubscriptionResponse(subscription=self._subscription(record))

    async def get_quota(
        self,
        user_api_key_dict: UserAPIKeyAuth,
        timezone_name: str,
    ) -> PortalQuotaResponse:
        if self.quota_reader is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Code Plan quota Redis is not configured",
            )

        tz = self._timezone(timezone_name)
        record = await self._current_subscription(user_api_key_dict)
        now = datetime.now(timezone.utc)
        metadata = self._quota_metadata(record)
        windows = quota_windows_from_metadata(metadata, now=now)
        windows = await self.quota_reader.resolve_windows(record.subscription_id, windows)
        spent_by_name = await self.quota_reader.get_window_spent(record.subscription_id, windows)

        return PortalQuotaResponse(
            subscription_id=record.subscription_id,
            project_id=record.project_id,
            timezone=timezone_name,
            generated_at=now,
            generated_at_local=now.astimezone(tz).isoformat(),
            windows=[
                self._quota_window(window, spent_by_name.get(window.name, 0.0), now, tz)
                for window in windows
            ],
        )

    async def get_usage(
        self,
        user_api_key_dict: UserAPIKeyAuth,
        timezone_name: str,
        start_time: datetime | None,
        end_time: datetime | None,
    ) -> PortalUsageResponse:
        self._timezone(timezone_name)
        record = await self._current_subscription(user_api_key_dict)
        start_at, end_at = self._time_range(start_time, end_time)
        rows = await self.portal_repository.usage_buckets(
            subscription=record,
            start_time=start_at,
            end_time=end_at,
            timezone_name=timezone_name,
        )
        buckets = [self._usage_bucket(row, record) for row in rows]
        totals = PortalUsageTotals(
            request_count=sum(bucket.request_count for bucket in buckets),
            input_tokens=sum(bucket.input_tokens for bucket in buckets),
            output_tokens=sum(bucket.output_tokens for bucket in buckets),
            total_tokens=sum(bucket.total_tokens for bucket in buckets),
            external_credits=round(sum(bucket.external_credits for bucket in buckets), 6),
        )
        return PortalUsageResponse(
            subscription_id=record.subscription_id,
            project_id=record.project_id,
            timezone=timezone_name,
            start_time=start_at,
            end_time=end_at,
            buckets=buckets,
            totals=totals,
        )

    async def get_requests(
        self,
        user_api_key_dict: UserAPIKeyAuth,
        start_time: datetime | None,
        end_time: datetime | None,
        page: int,
        page_size: int,
    ) -> PortalRequestListResponse:
        record = await self._current_subscription(user_api_key_dict)
        start_at, end_at = self._time_range(start_time, end_time)
        total = await self.portal_repository.request_count(record, start_at, end_at)
        rows = await self.portal_repository.request_rows(record, start_at, end_at, page, page_size)
        data = [self._request_record(row, record) for row in rows]
        return PortalRequestListResponse(
            subscription_id=record.subscription_id,
            project_id=record.project_id,
            start_time=start_at,
            end_time=end_at,
            page=page,
            page_size=page_size,
            total=total,
            total_pages=ceil(total / page_size) if total else 0,
            data=data,
        )

    async def list_keys(
        self,
        user_api_key_dict: UserAPIKeyAuth,
    ) -> PortalKeyListResponse:
        record = await self._current_subscription(user_api_key_dict)
        keys = await self.portal_repository.list_keys(record)
        return PortalKeyListResponse(
            subscription_id=record.subscription_id,
            project_id=record.project_id,
            data=keys,
        )

    async def create_key(
        self,
        user_api_key_dict: UserAPIKeyAuth,
        request: PortalKeyCreateRequest,
    ) -> PortalKeyCreateResponse:
        record = await self._current_subscription(user_api_key_dict)
        response = await self.subscription_service.issue_key(
            subscription_id=record.subscription_id,
            data=SubscriptionIssueKeyRequest(
                version=record.version,
                key_alias=request.key_alias,
                metadata=request.metadata,
            ),
            actor=self._portal_actor(user_api_key_dict),
        )
        return PortalKeyCreateResponse(
            subscription=self._subscription(response.subscription),
            key_id=response.key_id or "",
            token_id=response.token_id or response.key_id or "",
            key=response.key or "",
        )

    async def revoke_key(
        self,
        user_api_key_dict: UserAPIKeyAuth,
        key_id: str,
    ) -> PortalKeyRevokeResponse:
        record = await self._current_subscription(user_api_key_dict)
        updated = await self.subscription_service.revoke_key(
            subscription_id=record.subscription_id,
            data=SubscriptionRevokeKeyRequest(
                version=record.version,
                key_id=key_id,
            ),
            actor=self._portal_actor(user_api_key_dict),
        )
        return PortalKeyRevokeResponse(
            subscription=self._subscription(updated),
            revoked_key_id=key_id,
        )

    async def get_integration(
        self,
        user_api_key_dict: UserAPIKeyAuth,
        base_url: str,
    ) -> PortalIntegrationResponse:
        record = await self._current_subscription(user_api_key_dict)
        normalized_base_url = base_url.rstrip("/")
        api_base_url = normalized_base_url + "/v1"
        return PortalIntegrationResponse(
            subscription_id=record.subscription_id,
            project_id=record.project_id,
            base_url=normalized_base_url,
            api_base_url=api_base_url,
            allowed_models=record.plan_snapshot.allowed_models,
            allowed_routes=CODE_PLAN_GATEWAY_ALLOWED_ROUTES,
            sample_config={
                "openai": {
                    "base_url": api_base_url,
                    "api_key": "<your_user_portal_key>",
                },
                "curl": f"curl {api_base_url}/models -H 'Authorization: Bearer <your_user_portal_key>'",
            },
        )

    async def _current_subscription(
        self,
        user_api_key_dict: UserAPIKeyAuth,
    ) -> SubscriptionRecord:
        self._require_jwt(user_api_key_dict)
        scope = self._portal_scope(user_api_key_dict)
        if not scope.project_ids and not scope.litellm_team_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="JWT must include project_id or team_id",
            )
        record = await self.subscription_repository.get_current_for_scope(
            project_ids=scope.project_ids,
            litellm_team_id=scope.litellm_team_id,
        )
        if record is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Active Code Plan subscription not found for this user scope",
            )
        return record

    def _require_jwt(self, user_api_key_dict: UserAPIKeyAuth) -> None:
        claims = getattr(user_api_key_dict, "jwt_claims", None)
        if not isinstance(claims, dict) or not claims:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="JWT auth required",
            )

    def _portal_scope(self, user_api_key_dict: UserAPIKeyAuth) -> PortalScope:
        claims = getattr(user_api_key_dict, "jwt_claims", None)
        claims = claims if isinstance(claims, dict) else {}
        project_ids = self._unique_strings(
            getattr(user_api_key_dict, "project_id", None),
            self._claim(claims, "project_id", "code_plan_project_id", "project"),
        )
        litellm_team_id = self._first_string(
            getattr(user_api_key_dict, "team_id", None),
            self._claim(claims, "team_id", "litellm_team_id", "code_plan_team_id"),
        )
        return PortalScope(project_ids=project_ids, litellm_team_id=litellm_team_id)

    def _portal_actor(self, user_api_key_dict: UserAPIKeyAuth) -> UserAPIKeyAuth:
        claims = getattr(user_api_key_dict, "jwt_claims", None)
        caller_id = self._first_string(
            getattr(user_api_key_dict, "user_id", None),
            getattr(user_api_key_dict, "user_email", None),
            self._claim(claims if isinstance(claims, dict) else {}, "sub", "email"),
            "code-plan-portal",
        )
        return UserAPIKeyAuth(
            api_key="code-plan-portal",
            user_role=LitellmUserRoles.PROXY_ADMIN,
            user_id=caller_id,
            jwt_claims=claims if isinstance(claims, dict) else None,
        )

    def _subscription(self, record: SubscriptionRecord) -> PortalSubscription:
        return PortalSubscription(
            subscription_id=record.subscription_id,
            project_id=record.project_id,
            plan_id=record.plan_id,
            status=record.status.value if hasattr(record.status, "value") else str(record.status),
            plan_snapshot=record.plan_snapshot,
            expires_at=record.expires_at,
            renewed_at=record.renewed_at,
            created_at=record.created_at,
            updated_at=record.updated_at,
            version=record.version,
            key_count=len(record.litellm_key_ids),
            max_keys=record.plan_snapshot.max_keys,
        )

    def _quota_metadata(self, record: SubscriptionRecord) -> dict[str, object]:
        metadata = dict(record.metadata or {})
        metadata["code_plan_subscription_id"] = record.subscription_id
        metadata["code_plan_project_id"] = record.project_id
        metadata["code_plan_quota_5h"] = record.plan_snapshot.quota_5h
        metadata["code_plan_quota_weekly"] = record.plan_snapshot.quota_weekly
        week_anchor = record.renewed_at or record.created_at
        if week_anchor is not None:
            metadata["code_plan_week_anchor_epoch"] = int(self._aware_datetime(week_anchor).timestamp())
        return metadata

    def _quota_window(
        self,
        window: QuotaWindow,
        used: float,
        now: datetime,
        tz: ZoneInfo,
    ) -> PortalQuotaWindow:
        reset_at = datetime.fromtimestamp(window.period_end_epoch, tz=timezone.utc)
        anchor_at = (
            datetime.fromtimestamp(window.anchor_epoch, tz=timezone.utc)
            if window.anchor_epoch is not None
            else None
        )
        return PortalQuotaWindow(
            name=window.name,
            limit=float(window.limit),
            used=round(float(used), 6),
            remaining=round(max(float(window.limit) - float(used), 0.0), 6),
            period_id=window.period_id,
            reset_at=reset_at,
            reset_at_local=reset_at.astimezone(tz).isoformat(),
            seconds_until_reset=max(int(window.period_end_epoch - int(now.timestamp())), 0),
            starts_on_first_success=window.starts_on_first_success,
            anchor_at=anchor_at,
            anchor_at_local=anchor_at.astimezone(tz).isoformat() if anchor_at else None,
        )

    def _usage_bucket(
        self,
        row: dict[str, object],
        record: SubscriptionRecord,
    ) -> PortalUsageBucket:
        input_tokens = self._int(row.get("input_tokens"))
        output_tokens = self._int(row.get("output_tokens"))
        total_tokens = self._int(row.get("total_tokens"))
        return PortalUsageBucket(
            date=str(row.get("date") or ""),
            request_count=self._int(row.get("request_count")),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            external_credits=self._credits(record, input_tokens, output_tokens),
        )

    def _request_record(
        self,
        row: dict[str, object],
        record: SubscriptionRecord,
    ) -> PortalRequestRecord:
        input_tokens = self._int(row.get("input_tokens"))
        output_tokens = self._int(row.get("output_tokens"))
        total_tokens = self._int(row.get("total_tokens"))
        return PortalRequestRecord(
            request_id=self._optional_string(row.get("request_id")),
            start_time=self._optional_datetime(row.get("start_time")),
            end_time=self._optional_datetime(row.get("end_time")),
            request_duration_ms=self._optional_float(row.get("request_duration_ms")),
            model=self._optional_string(row.get("model")),
            api_base=self._optional_string(row.get("api_base")),
            call_type=self._optional_string(row.get("call_type")),
            status=self._optional_string(row.get("status")),
            cache_hit=row.get("cache_hit") if isinstance(row.get("cache_hit"), bool) else self._optional_string(row.get("cache_hit")),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            external_credits=self._credits(record, input_tokens, output_tokens),
        )

    def _credits(self, record: SubscriptionRecord, input_tokens: int, output_tokens: int) -> float:
        return round(
            calculate_credits(
                CreditUsage(input_tokens=input_tokens, output_tokens=output_tokens),
                CreditMultipliers(
                    input_multiplier=record.plan_snapshot.credit_input_multiplier,
                    output_multiplier=record.plan_snapshot.credit_output_multiplier,
                    cache_read_multiplier=record.plan_snapshot.credit_cache_read_multiplier,
                    cache_write_multiplier=record.plan_snapshot.credit_cache_write_multiplier,
                ),
            ),
            6,
        )

    def _time_range(
        self,
        start_time: datetime | None,
        end_time: datetime | None,
    ) -> tuple[datetime, datetime]:
        end_at = self._aware_datetime(end_time) if end_time else datetime.now(timezone.utc)
        start_at = self._aware_datetime(start_time) if start_time else end_at - timedelta(days=30)
        if start_at >= end_at:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="start_time must be before end_time",
            )
        return start_at, end_at

    def _timezone(self, timezone_name: str) -> ZoneInfo:
        try:
            return ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid timezone",
            )

    def _aware_datetime(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _claim(self, claims: dict[str, object], *names: str) -> object:
        for name in names:
            value = claims.get(name)
            if value is not None:
                return value
        return None

    def _unique_strings(self, *values: object) -> list[str]:
        unique: list[str] = []
        for value in values:
            if isinstance(value, list):
                candidates = value
            elif isinstance(value, tuple):
                candidates = list(value)
            else:
                candidates = [value]
            for candidate in candidates:
                text = self._optional_string(candidate)
                if text and text not in unique:
                    unique.append(text)
        return unique

    def _first_string(self, *values: object) -> str | None:
        for value in values:
            text = self._optional_string(value)
            if text:
                return text
        return None

    def _optional_string(self, value: object) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    def _optional_float(self, value: object) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _optional_datetime(self, value: object) -> datetime | None:
        if isinstance(value, datetime):
            return value
        return None

    def _int(self, value: object) -> int:
        if value is None:
            return 0
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0
