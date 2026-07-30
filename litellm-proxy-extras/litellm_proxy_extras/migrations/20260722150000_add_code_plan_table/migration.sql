-- CreateTable
CREATE TABLE IF NOT EXISTS "LiteLLM_CodePlanTable" (
    "plan_id" TEXT NOT NULL,
    "name" TEXT NOT NULL,
    "description" TEXT,
    "status" TEXT NOT NULL DEFAULT 'draft',
    "quota_5h" INTEGER NOT NULL,
    "quota_weekly" INTEGER NOT NULL,
    "allowed_models" TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    "rpm_limit" BIGINT,
    "tpm_limit" BIGINT,
    "max_parallel_requests" INTEGER,
    "max_keys" INTEGER NOT NULL DEFAULT 5,
    "default_max_output_tokens" INTEGER,
    "credit_rule_id" TEXT,
    "metadata" JSONB NOT NULL DEFAULT '{}',
    "version" INTEGER NOT NULL DEFAULT 1,
    "created_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "created_by" TEXT,
    "updated_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_by" TEXT,

    CONSTRAINT "LiteLLM_CodePlanTable_pkey" PRIMARY KEY ("plan_id")
);

-- CreateIndex
CREATE UNIQUE INDEX IF NOT EXISTS "LiteLLM_CodePlanTable_name_key" ON "LiteLLM_CodePlanTable"("name");

-- CreateIndex
CREATE INDEX IF NOT EXISTS "LiteLLM_CodePlanTable_status_idx" ON "LiteLLM_CodePlanTable"("status");

-- CreateIndex
CREATE INDEX IF NOT EXISTS "LiteLLM_CodePlanTable_created_at_idx" ON "LiteLLM_CodePlanTable"("created_at");
