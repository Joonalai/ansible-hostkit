# Changelog

Releases are tags. A breaking change to a role's variables is a new major
version, with the migration in its entry here.

## Unreleased

- `rootless_account`: an account that runs rootless podman containers from
  its systemd manager, with a pinned subordinate ID range, limits, and its
  manager tied to the host's sandbox units.
- `ci_runner`: one-job GitHub Actions runners for private repositories, in
  a rootless account behind the host's own sandbox.
- `claude_code`: Claude Code as a Remote Control server.
- `claude_team`: the subagent roles and skills the runner's jobs and the
  workbench's sessions share.
- `github_runner`: ephemeral GitHub Actions runners for Claude Code, one
  rootless container per job.
- `workbench`: Claude Code's team in a container per repository, writing to
  GitHub only through a gate.
