"use client";

import useAuthorized from "@/app/(dashboard)/hooks/useAuthorized";
import { CodePlanSubscriptionsPage } from "@/components/codeplan";

export default function CodePlanSubscriptions() {
  const { userRole } = useAuthorized();

  return <CodePlanSubscriptionsPage userRole={userRole} />;
}
