import type { Metadata } from "next";

import { RunView } from "./run-view";

export const metadata: Metadata = { title: "Run" };

export default async function RunPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <RunView runId={id} />;
}
