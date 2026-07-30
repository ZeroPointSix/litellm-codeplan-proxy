"use client";

import useAuthorized from "@/app/(dashboard)/hooks/useAuthorized";
import { CodePlanUsageLedgerPage } from "@/components/codeplan";

export default function CodePlanUsageLedger() {
  const { accessToken, userRole } = useAuthorized();

  return <CodePlanUsageLedgerPage accessToken={accessToken} userRole={userRole} />;
}
