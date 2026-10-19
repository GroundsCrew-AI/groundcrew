import type { Metadata } from "next";

import { OverviewHome } from "./overview-home";

export const metadata: Metadata = { title: "Overview" };

export default function OverviewPage() {
  return <OverviewHome />;
}
