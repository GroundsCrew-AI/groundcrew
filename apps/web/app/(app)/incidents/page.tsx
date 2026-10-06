import type { Metadata } from "next";

import { IncidentsIllustration } from "@/components/states/illustrations";
import { PlaceholderPage } from "@/components/states/placeholder";

export const metadata: Metadata = { title: "Incidents" };

export default function IncidentsPage() {
  return (
    <PlaceholderPage
      title="Incidents"
      illustration={<IncidentsIllustration />}
      heading="Find the cause, then fix it with a pull request"
      description="Medic lines up deploys, alarms, and log spikes from every cloud on one timeline, finds the likely cause, and proposes a fix. Every fix is verified before it is called done."
      points={[
        "A timeline of what changed around the incident, across clouds",
        "The root cause with the evidence Medic used",
        "The fix as a pull request, plus the check that proved it worked",
      ]}
      phase="Arrives in Phase 5."
    />
  );
}
