CREATE TABLE IF NOT EXISTS "LiteLLM_CreditRuleTable" (
    "id" TEXT NOT NULL,
    "credit_rule_id" TEXT NOT NULL,
    "name" TEXT NOT NULL,
    "description" TEXT,
    "status" TEXT NOT NULL DEFAULT 'draft',
    "input_multiplier" DOUBLE PRECISION NOT NULL,
    "output_multiplier" DOUBLE PRECISION NOT NULL,
    "cache_read_multiplier" DOUBLE PRECISION NOT NULL,
    "cache_write_multiplier" DOUBLE PRECISION NOT NULL,
    "effective_at" TIMESTAMP(3),
    "metadata" JSONB NOT NULL DEFAULT '{}',
    "version" INTEGER NOT NULL DEFAULT 1,
    "created_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "created_by" TEXT,
    "updated_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_by" TEXT,

    CONSTRAINT "LiteLLM_CreditRuleTable_pkey" PRIMARY KEY ("id")
);

CREATE UNIQUE INDEX IF NOT EXISTS "LiteLLM_CreditRuleTable_credit_rule_id_version_key" ON "LiteLLM_CreditRuleTable"("credit_rule_id", "version");

CREATE INDEX IF NOT EXISTS "LiteLLM_CreditRuleTable_credit_rule_id_idx" ON "LiteLLM_CreditRuleTable"("credit_rule_id");

CREATE INDEX IF NOT EXISTS "LiteLLM_CreditRuleTable_status_idx" ON "LiteLLM_CreditRuleTable"("status");

CREATE INDEX IF NOT EXISTS "LiteLLM_CreditRuleTable_effective_at_idx" ON "LiteLLM_CreditRuleTable"("effective_at");

CREATE INDEX IF NOT EXISTS "LiteLLM_CreditRuleTable_created_at_idx" ON "LiteLLM_CreditRuleTable"("created_at");

CREATE INDEX IF NOT EXISTS "LiteLLM_CodePlanTable_credit_rule_id_idx" ON "LiteLLM_CodePlanTable"("credit_rule_id");
