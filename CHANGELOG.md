# Changelog

Releases are tags. A breaking change to a role's variables is a new major
version, with the migration in its entry here.

## Unreleased

- `rootless_account`: an account that runs rootless podman containers from
  its systemd manager, with a pinned subordinate ID range and limits.
- `ci_runner`: one-job GitHub Actions runners for private repositories, in
  a rootless account behind the host's own sandbox.
