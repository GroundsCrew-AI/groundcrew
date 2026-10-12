# policy

Allowlists for read verbs and approval rules for write and emergency actions.

- `engine.py`: the gate for tools. Write and emergency tools create an approval and run only
  through `execute_tool` with an approved, unused approval for the same arguments.
- `actions.py`: the gate for deploy targets (Phase 4, M3). `Target.apply()` and
  `Target.rollback()` take an `ApprovedAction` that only `issue_approved_action` creates from an
  approved, unused approval (`target_apply` with policy write, `target_rollback` with policy
  emergency) whose `arguments.plan_hash` is the plan's hash. Tokens are signed with
  `APPROVAL_TOKEN_KEY` (required outside dev; dev derives one from `SECRETS_KEY`), so they work
  across workers and restarts (`encode()` / `decode()`). A target spends one before its first
  write with one atomic `UPDATE approvals SET used_at = ... WHERE used_at IS NULL`: a token runs
  once however many workers hold it.
