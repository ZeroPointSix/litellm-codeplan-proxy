"use client";

import useAuthorized from "@/app/(dashboard)/hooks/useAuthorized";
import { CodePlanSubscriptionsPage } from "@/components/codeplan/CodePlanSubscriptionsPage";

export default function CodePlanSubscriptions() {
  const { accessToken, userRole } = useAuthorized();

  return <CodePlanSubscriptionsPage accessToken={accessToken} userRole={userRole} />;
}
