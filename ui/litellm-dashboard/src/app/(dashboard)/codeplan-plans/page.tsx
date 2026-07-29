"use client";

import useAuthorized from "@/app/(dashboard)/hooks/useAuthorized";
import { CodePlanPlansPage } from "@/components/codeplan";

export default function CodePlanPlans() {
  const { userRole } = useAuthorized();

  return <CodePlanPlansPage userRole={userRole} />;
}
