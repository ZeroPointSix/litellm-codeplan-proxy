ALTER TABLE "LiteLLM_CodePlanSubscriptionTable" ADD COLUMN IF NOT EXISTS "window_5h_start" TIMESTAMP(3);
ALTER TABLE "LiteLLM_CodePlanSubscriptionTable" ADD COLUMN IF NOT EXISTS "window_week_start" TIMESTAMP(3);

CREATE TABLE IF NOT EXISTS "LiteLLM_CodePlanUsageLedgerTable" (
    "event_id" TEXT NOT NULL,
    "request_id" TEXT NOT NULL,
    "subscription_id" TEXT NOT NULL,
    "project_id" TEXT,
    "user_id" TEXT,
    "api_key_id" TEXT,
    "model" TEXT,
    "event_type" TEXT NOT NULL,
    "credits" DOUBLE PRECISION NOT NULL,
    "input_tokens" INTEGER NOT NULL DEFAULT 0,
    "output_tokens" INTEGER NOT NULL DEFAULT 0,
    "cache_read_tokens" INTEGER NOT NULL DEFAULT 0,
    "cache_write_tokens" INTEGER NOT NULL DEFAULT 0,
    "input_multiplier" DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    "output_multiplier" DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    "cache_read_multiplier" DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    "cache_write_multiplier" DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    "rule_version" INTEGER NOT NULL DEFAULT 1,
    "window_5h_period_id" TEXT,
    "window_5h_start" TIMESTAMP(3),
    "window_5h_end" TIMESTAMP(3),
    "window_week_period_id" TEXT,
    "window_week_start" TIMESTAMP(3),
    "window_week_end" TIMESTAMP(3),
    "metadata" JSONB NOT NULL DEFAULT '{}',
    "created_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT "LiteLLM_CodePlanUsageLedgerTable_pkey" PRIMARY KEY ("event_id")
);

CREATE UNIQUE INDEX IF NOT EXISTS "LiteLLM_CodePlanUsageLedgerTable_subscription_request_event_key" ON "LiteLLM_CodePlanUsageLedgerTable"("subscription_id", "request_id", "event_type");
CREATE INDEX IF NOT EXISTS "LiteLLM_CodePlanUsageLedgerTable_subscription_created_at_idx" ON "LiteLLM_CodePlanUsageLedgerTable"("subscription_id", "created_at");
CREATE INDEX IF NOT EXISTS "LiteLLM_CodePlanUsageLedgerTable_project_created_at_idx" ON "LiteLLM_CodePlanUsageLedgerTable"("project_id", "created_at");
CREATE INDEX IF NOT EXISTS "LiteLLM_CodePlanUsageLedgerTable_user_created_at_idx" ON "LiteLLM_CodePlanUsageLedgerTable"("user_id", "created_at");
CREATE INDEX IF NOT EXISTS "LiteLLM_CodePlanUsageLedgerTable_model_created_at_idx" ON "LiteLLM_CodePlanUsageLedgerTable"("model", "created_at");
CREATE INDEX IF NOT EXISTS "LiteLLM_CodePlanUsageLedgerTable_event_type_created_at_idx" ON "LiteLLM_CodePlanUsageLedgerTable"("event_type", "created_at");
CREATE INDEX IF NOT EXISTS "LiteLLM_CodePlanUsageLedgerTable_request_id_idx" ON "LiteLLM_CodePlanUsageLedgerTable"("request_id");
CREATE INDEX IF NOT EXISTS "LiteLLM_CodePlanUsageLedgerTable_created_at_idx" ON "LiteLLM_CodePlanUsageLedgerTable"("created_at");
