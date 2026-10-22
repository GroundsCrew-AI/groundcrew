Groundcrew agent: a small program installed on an environment (first a VM reachable from the
internet; later Kubernetes, ECS, and similar) that watches the services listed in its
configuration file, sends their signals to Groundcrew, and runs deploys and Medic's approved fixes
for them. It dials out to Groundcrew, like a monitoring agent does, so nothing has to reach in.

Decisions (2026-10-22): written in Go; the first runtime is a VM running Docker / Compose /
systemd services. Plan first (docs/agent-plan.md), then one draft PR per milestone, stopping for
review after each. Same rules as Phase 6: no AWS write, server change, or production deploy
without an explicit OK in the chat; user-facing wording describes Groundcrew as a product.
