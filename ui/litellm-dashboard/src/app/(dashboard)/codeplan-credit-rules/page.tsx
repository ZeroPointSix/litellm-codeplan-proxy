"use client";

import useAuthorized from "@/app/(dashboard)/hooks/useAuthorized";
import { CodePlanCreditRulesPage } from "@/components/codeplan";

export default function CodePlanCreditRules() {
  const { userRole } = useAuthorized();

  return <CodePlanCreditRulesPage userRole={userRole} />;
}
