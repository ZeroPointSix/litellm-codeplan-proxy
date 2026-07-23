CREATE TABLE IF NOT EXISTS "LiteLLM_CodePlanSubscriptionTable" (
    "subscription_id" TEXT NOT NULL,
    "project_id" TEXT NOT NULL,
    "plan_id" TEXT NOT NULL,
    "status" TEXT NOT NULL DEFAULT 'active',
    "plan_snapshot" JSONB NOT NULL,
    "litellm_team_id" TEXT,
    "litellm_key_ids" TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    "expires_at" TIMESTAMP(3),
    "renewed_at" TIMESTAMP(3),
    "paused_at" TIMESTAMP(3),
    "canceled_at" TIMESTAMP(3),
    "metadata" JSONB NOT NULL DEFAULT '{}',
    "version" INTEGER NOT NULL DEFAULT 1,
    "created_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "created_by" TEXT,
    "updated_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_by" TEXT,

    CONSTRAINT "LiteLLM_CodePlanSubscriptionTable_pkey" PRIMARY KEY ("subscription_id")
);

CREATE INDEX IF NOT EXISTS "LiteLLM_CodePlanSubscriptionTable_project_id_idx" ON "LiteLLM_CodePlanSubscriptionTable"("project_id");

CREATE INDEX IF NOT EXISTS "LiteLLM_CodePlanSubscriptionTable_plan_id_idx" ON "LiteLLM_CodePlanSubscriptionTable"("plan_id");

CREATE INDEX IF NOT EXISTS "LiteLLM_CodePlanSubscriptionTable_status_idx" ON "LiteLLM_CodePlanSubscriptionTable"("status");

CREATE INDEX IF NOT EXISTS "LiteLLM_CodePlanSubscriptionTable_litellm_team_id_idx" ON "LiteLLM_CodePlanSubscriptionTable"("litellm_team_id");

CREATE INDEX IF NOT EXISTS "LiteLLM_CodePlanSubscriptionTable_expires_at_idx" ON "LiteLLM_CodePlanSubscriptionTable"("expires_at");

CREATE INDEX IF NOT EXISTS "LiteLLM_CodePlanSubscriptionTable_created_at_idx" ON "LiteLLM_CodePlanSubscriptionTable"("created_at");
