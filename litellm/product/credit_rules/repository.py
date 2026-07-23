from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

from litellm.product.credit_rules.models import CreditRuleRecord


class CreditRuleRepository:
    table_name = "litellm_creditruletable"

    def __init__(self, prisma_client: Any):
        self._prisma_client = prisma_client

    @property
    def prisma_client(self) -> Any:
        if self._prisma_client is None:
            raise RuntimeError("No DB Connected. See - https://docs.litellm.ai/docs/proxy/virtual_keys")
        return self._prisma_client

    @property
    def table(self) -> Any:
        return getattr(self.prisma_client.db, self.table_name)

    async def create(self, data: dict[str, Any]) -> CreditRuleRecord:
        record = await self.table.create(data=self._serialize(data))
        return self._to_model(record)

    async def get(self, credit_rule_id: str, version: int | None = None) -> CreditRuleRecord | None:
        if version is None:
            return await self.get_latest(credit_rule_id)
        record = await self.table.find_first(where={"credit_rule_id": credit_rule_id, "version": version})
        if record is None:
            return None
        return self._to_model(record)

    async def get_latest(self, credit_rule_id: str) -> CreditRuleRecord | None:
        records = await self.table.find_many(
            where={"credit_rule_id": credit_rule_id}, order={"version": "desc"}, take=1
        )
        first = self._first_row(records)
        if first is None:
            return None
        return self._to_model(first)

    async def get_active(self, credit_rule_id: str) -> CreditRuleRecord | None:
        records = await self.table.find_many(
            where={"credit_rule_id": credit_rule_id, "status": "active"}, order={"version": "desc"}, take=1
        )
        first = self._first_row(records)
        if first is None:
            return None
        return self._to_model(first)

    async def list(self, status: str | None = None, credit_rule_id: str | None = None) -> list[CreditRuleRecord]:
        where = self._list_where(status=status, credit_rule_id=credit_rule_id)
        records = await self.table.find_many(where=where, order={"created_at": "desc"})
        return [self._to_model(record) for record in records]

    async def archive_active_versions(self, credit_rule_id: str, actor_id: str | None = None) -> int:
        result = await self.table.update_many(
            where={"credit_rule_id": credit_rule_id, "status": "active"},
            data={"status": "archived", "updated_by": actor_id},
        )
        return self._updated_count(result)

    async def update_status(
        self,
        credit_rule_id: str,
        version: int,
        status: str,
        actor_id: str | None = None,
    ) -> CreditRuleRecord | None:
        result = await self.table.update_many(
            where={"credit_rule_id": credit_rule_id, "version": version},
            data={"status": status, "updated_by": actor_id},
        )
        if self._updated_count(result) == 0:
            return None
        return await self.get(credit_rule_id, version=version)

    def _list_where(self, status: str | None, credit_rule_id: str | None) -> dict[str, str]:
        where: dict[str, str] = {}
        if status is not None:
            where["status"] = status
        if credit_rule_id is not None:
            where["credit_rule_id"] = credit_rule_id
        return where

    def _first_row(self, rows: Any) -> Any | None:
        if rows is None:
            return None
        if isinstance(rows, (list, tuple)):
            return rows[0] if rows else None
        return rows

    def _to_model(self, record: Any) -> CreditRuleRecord:
        if isinstance(record, BaseModel):
            data = record.model_dump(exclude_none=False)
        elif isinstance(record, dict):
            data = dict(record)
        else:
            data = {key: getattr(record, key) for key in CreditRuleRecord.model_fields if hasattr(record, key)}

        metadata = data.get("metadata")
        if isinstance(metadata, str):
            data["metadata"] = json.loads(metadata or "{}")
        elif metadata is None:
            data["metadata"] = {}

        return CreditRuleRecord.model_validate(data)

    def _serialize(self, data: dict[str, Any]) -> dict[str, Any]:
        serialized = dict(data)
        metadata = serialized.get("metadata")
        if isinstance(metadata, dict):
            serialized["metadata"] = json.dumps(metadata)
        return serialized

    def _updated_count(self, result: Any) -> int:
        if isinstance(result, int):
            return result
        if isinstance(result, dict) and "count" in result:
            return int(result["count"])

        count = getattr(result, "count", None)
        if count is not None:
            return int(count)
        return int(result)
