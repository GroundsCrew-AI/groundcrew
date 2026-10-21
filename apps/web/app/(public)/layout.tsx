import type { ReactNode } from "react";

import { PublicShell } from "@/components/public/public-shell";

/** Pages anyone can read without signing in: the welcome page, policies, security, and docs. */
export default function PublicLayout({ children }: { children: ReactNode }) {
  return <PublicShell>{children}</PublicShell>;
}
