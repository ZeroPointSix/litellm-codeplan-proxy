"use client";

import useAuthorized from "@/app/(dashboard)/hooks/useAuthorized";
import { CodePlanUsageLedgerPage } from "@/components/codeplan";

export default function CodePlanUsageLedger() {
  const { userRole } = useAuthorized();

  return <CodePlanUsageLedgerPage userRole={userRole} />;
}
