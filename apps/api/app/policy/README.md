# policy

Allowlists for read verbs and approval rules for write and emergency actions.

- `engine.py`: the gate for tools. Write and emergency tools create an approval and run only
  through `execute_tool` with an approved, unused approval for the same arguments.
- `actions.py`: the gate for deploy targets (Phase 4, M3). `Target.apply()` and
  `Target.rollback()` take an `ApprovedAction`: a single-use token that only
  `issue_approved_action` creates from an approved, unused approval (`target_apply` with policy
  write, `target_rollback` with policy emergency) whose `arguments.plan_hash` is the plan's hash.
  Issuing marks the approval used; the target consumes the token before its first write. The
  seal key lives in the process, so issue and consume in the same worker process.
