"use client";

import useAuthorized from "@/app/(dashboard)/hooks/useAuthorized";
import { CodePlanPlansPage } from "@/components/codeplan";

export default function CodePlanPlans() {
  const { accessToken, userRole } = useAuthorized();

  return <CodePlanPlansPage accessToken={accessToken} userRole={userRole} />;
}
