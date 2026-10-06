import type { Metadata } from "next";

import { MapIllustration } from "@/components/states/illustrations";
import { PlaceholderPage } from "@/components/states/placeholder";

export const metadata: Metadata = { title: "Map" };

export default function MapPage() {
  return (
    <PlaceholderPage
      title="Map"
      illustration={<MapIllustration />}
      heading="One graph of everything you run"
      description="Scout reads each connection, turns AWS, Azure, Kubernetes, VM, and Vercel resources into one shape, and works out how they talk to each other."
      points={[
        "Resources from every connection, grouped by cloud and region",
        "Edges Groundcrew inferred, each with its evidence: env vars, security groups, DNS",
        "Coverage per resource: observed, diagnosable, or actionable",
      ]}
      phase="Arrives in Phase 3, after connections."
    />
  );
}
