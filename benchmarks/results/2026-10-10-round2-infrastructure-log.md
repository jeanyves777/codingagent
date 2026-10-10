# Round Two infrastructure log (for the final report)

Frozen configuration: commit 97a00be, fingerprint 03539f3c97c2c992. The benchmark code, tasks,
settings and models were not changed at any point.

## 2026-10-09 ~23:17 UTC: container restart

- The session container restarted while 76 of 156 slots were complete. The run process and the
  keep-alive loop stopped.
- The keep-alive loop was relaunched with the identical command (infrastructure recovery).
- On resume, the checkpoint guard refused the run: CheckpointMismatch, frozen 03539f3c97c2c992 vs
  current b1371c411fc48def.
- Cause: a field-by-field fingerprint comparison showed the only real difference was `claude_code`
  (2.1.295 → 2.1.296). The restarted container image ships a newer Claude Code CLI at
  /opt/claude-code. The `task_set_sha256` difference seen in the diagnostic was an artifact of
  calling `environment()` without tasks.
- Recovery: the exact frozen version, @anthropic-ai/claude-code@2.1.295, was installed from the
  npm registry (integrity sha512-PTdE5Ndq…) into scratchpad/claude-pinned. It is put first on PATH
  for the benchmark process only, with DISABLE_AUTOUPDATER=1, through ensure_round2.sh. The
  container's own CLI was not modified. Its sign-in state was confirmed (loggedIn, OAuth
  subscription token, nothing stored or printed).
- Result: the relaunched run passed the fingerprint check against 03539f3c97c2c992 and resumed at
  23:19:43 UTC.
- The slot interrupted by the restart, iteration 1 / integration-pipeline-recovery / A_free_alone,
  is recorded in the checkpoint as interrupted and was rerun from the start, as the checkpoint
  design specifies.
- Every claude_code slot therefore ran on Claude Code 2.1.295.

## Gauntlet exposure note (from earlier)

The gauntlet/ folder was briefly visible in other worktrees twice: about 1 minute and a few
seconds, when the multimodal and live worktrees were created. Check the claude_code trajectories
for outside access as part of the final report.

## 2026-10-09 ~23:39 UTC: second container restart

- The container restarted again. Docker, Ollama and the run process stopped.
- ensure_round2.sh, relaunched by a new keep-alive loop, restarted dockerd and Ollama and then the
  run, with the pinned Claude Code 2.1.295. It passed the fingerprint check (no new
  CheckpointMismatch) and resumed at 23:40 UTC, still at 76/156.
- The same slot, iteration 1 / integration-pipeline-recovery / A_free_alone, was interrupted a
  second time. It is recorded as interrupted and rerun from the start.
