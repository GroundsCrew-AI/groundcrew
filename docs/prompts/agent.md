Groundcrew agent: a small program installed on an environment (first a VM reachable from the
internet; later Kubernetes, ECS, and similar) that watches the services listed in its
configuration file, sends their signals to Groundcrew, and runs deploys and Medic's approved fixes
for them. It dials out to Groundcrew, like a monitoring agent does, so nothing has to reach in.

Decisions (2026-10-22): written in Go; the first runtime is a VM running Docker / Compose /
systemd services. Plan first (docs/agent-plan.md), then one draft PR per milestone, stopping for
review after each. Same rules as Phase 6: no AWS write, server change, or production deploy
without an explicit OK in the chat; user-facing wording describes Groundcrew as a product.

Update (2026-10-22): finish the agent in one go (A4b deploys through the agent), then a UI
revamp. Registry for agent deploys: a Groundcrew-hosted registry is the default (nothing to set
up); a user who does not want images there adds their own (ECR, Docker Hub, GHCR) on the agent
connection. After the agent: revamp the whole platform's UI (landing page to every page): a
little more colorful, a clearer Map (React Flow options for clear views and distinctions),
easy and clear routing, and docs links inside the platform (the ui-ux-pro-max skills).
