# targets

Deploy targets behind one protocol (`base.py`): `discover`, `plan`, `preflight`, `apply`,
`verify`, `logs`, `rollback_plan`, `rollback`. `k8s.py` deploys into a `gc-<app>` namespace on a
cluster; `aws_ecs.py` deploys a Fargate service in `groundcrew-deploys` with the connection's
deploy role. `target_for(settings, credentials, connection)` picks one.

`apply()` and `rollback()` need an `ApprovedAction` (`app/policy/actions.py`). The image push
is the first apply step: nothing leaves the sandbox before approval. Details:
docs/phase4-infra.md, Targets.
