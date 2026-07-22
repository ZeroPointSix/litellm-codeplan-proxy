import json
from typing import Any, Optional

from pydantic import BaseModel

from litellm.product.plans.models import PlanRecord


class PlanRepository:
    table_name = "litellm_codeplantable"

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

    async def create(self, data: dict[str, Any]) -> PlanRecord:
        record = await self.table.create(data=self._serialize(data))
        return self._to_model(record)

    async def get(self, plan_id: str) -> Optional[PlanRecord]:
        record = await self.table.find_unique(where={"plan_id": plan_id})
        if record is None:
            return None
        return self._to_model(record)

    async def list(self, status: Optional[str] = None) -> list[PlanRecord]:
        where = {"status": status} if status else {}
        records = await self.table.find_many(where=where, order={"created_at": "desc"})
        return [self._to_model(record) for record in records]

    async def update(self, plan_id: str, data: dict[str, Any]) -> PlanRecord:
        record = await self.table.update(where={"plan_id": plan_id}, data=self._serialize(data))
        return self._to_model(record)

    async def update_if_version(self, plan_id: str, version: int, data: dict[str, Any]) -> Optional[PlanRecord]:
        updated = await self.table.update_many(
            where={"plan_id": plan_id, "version": version},
            data=self._serialize(data),
        )
        if self._updated_count(updated) == 0:
            return None
        return await self.get(plan_id)

    def _to_model(self, record: Any) -> PlanRecord:
        if isinstance(record, BaseModel):
            data = record.model_dump(exclude_none=False)
        elif isinstance(record, dict):
            data = dict(record)
        else:
            data = {key: getattr(record, key) for key in PlanRecord.model_fields if hasattr(record, key)}

        metadata = data.get("metadata")
        if isinstance(metadata, str):
            data["metadata"] = json.loads(metadata or "{}")
        elif metadata is None:
            data["metadata"] = {}

        return PlanRecord.model_validate(data)

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
