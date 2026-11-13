# The `executor` role

Notes for working on this role. The collection's conventions are in the
root [CLAUDE.md](../../CLAUDE.md), which applies here too. Its callers are
[github_runner](../github_runner/CLAUDE.md)'s slots and
[workbench](../workbench/CLAUDE.md)'s repositories; how it works for a
consumer is [docs/executor.md](../../docs/executor.md).

- **The front never opens the code; the agent never sees a key.** The
  front (`files/front/`) runs as `<name>-executor`, which has no podman,
  holds only the keys' SHA-256 and passes the bundle to the agent as bytes.
  The agent (`files/agent/`) runs as `<name>-exec`, sees only the caller's
  name the front resolved, and runs git and compose. A change that lets the
  front read the tree, or the agent read `callers/`, joins the two halves
  the split exists to keep apart.
- **The caller is the key's.** Nothing in a request names a caller, a
  project or a path: the agent derives the compose project, the tree and
  the ports from the caller the front resolved. A new action follows that
  rule, with its tests, never a pass-through of a field.
- **The agent's socket is the front's alone.** It is `0666` inside a
  directory `0750` `<name>-exec:<name>-executor`, which tmpfiles makes; a
  caller's socket is the front's, `api.sock`. Never mount the agent's into
  a caller.
- **Refusals are checked on `docker-compose config`'s output**, the
  merged and interpolated model, so an override file or a `${VAR}` cannot
  slip past them, and only with the compose profiles the stack profile
  turns on (`compose_profiles`): the others' services are left out of the
  output, and so out of `checked.json`, which every later command runs
  from with the same `--profile`s, so nothing can start them. A suite's
  service must be in it. `security_opt` passes only as `no-new-privileges`.
- **The stack's env is the tree's `.env` too**, compose's default, which a
  service's `env_file: .env` reads; it replaces the repository's own, and a
  link there is removed, never written through (`write_tree_env`).
  The namespaces are an allow list (`OWN_MODE`); what a service may never
  set is `NEVER` in `executor_agent.py`. A new compose key that widens a
  container's reach goes there with a test before anything relies on it.
- **`config` reads files, so it runs confined.** `include`, `extends` and
  `env_file` take any path, and `env_file`'s contents land in the
  container's environment. `CONFINE` runs it in a user and mount namespace
  of its own, with the home and the stacks behind an empty tmpfs and only
  the stack's own directory bound back. It needs unprivileged user
  namespaces, which Debian has on.
- **What was checked is what runs.** The checked config is
  `~/stacks/<caller>/checked.json`, outside the tree, and every later
  compose command reads it alone (`Stack.compose`), never the tree's
  files: a container that writes the tree could rewrite them, or swap a
  bind mount's source for a link out of the tree before the next start.
  That is also why bind mounts from the tree must be read-only.
- **The checkout runs no git config of the repository's.** The bundle is
  fetched into a fresh `git init` with `core.hooksPath=/dev/null` and
  `fetch.fsckObjects`, then checked out detached; nothing runs in the tree
  but compose.
- **The checkout runs under umask 022**, the agent under 077. A
  Dockerfile's `COPY` keeps the tree's modes, so a `0600` file in the tree
  is one the image's non-root user cannot read; `TREE_UMASK` sets git's
  usual ones for the checkout alone. The tree sits in the exec account's
  home, which no other account enters.
- **An up and a start watch the stack** (`Agent.watch`), not compose's
  `--wait`, which fails a stack on any container that ends, status 0 too,
  but one another service waits for. After `up --detach` the agent polls
  the containers until every one runs, healthy where it has a check, or
  ended with 0, and has stayed so `SETTLE_SECONDS`, up to `WAIT_SECONDS`,
  on a monotonic clock. A restart policy brings a dying service back
  before a poll may see it ended, so a container whose `Restarts` grew
  during the watch has failed, and podman's `stopped` is judged by its
  exit code. `compose run`'s leftovers (`ONEOFF_LABEL`) are no service. On
  a failure, the watch's own included, it answers with the failed
  services' log tail and stops the stack, even when the logs cannot be
  read. The smoke test's `app`, its `broken` profile and the one-shot
  `seed` and `job` prove it.
- **Builds run one at a time** (`COMPOSE_PARALLEL_LIMIT=1` in the agent's
  unit): the legacy builder shares nothing between builds running at once,
  so services with one build section each built it. One after another,
  all but the first are cache hits; the smoke test's `app` and `worker`
  check they get one image, from a build its nonce makes new every run.
- **No `NoNewPrivileges` on the agent's unit.** Rootless podman starts the
  account's user namespace with the setuid `newuidmap` and `newgidmap`,
  which it refuses, and the play's check then fails with "newuidmap: write
  to uid_map failed". The front's unit keeps it and every other hardening.
- **`callers.yml` is for the client roles.** It keeps a key per caller in
  `keys/`, root's alone, writes the hash to `callers/` for the front, and
  the key itself into the caller's env file, which its unit reads. It
  removes its prefix's callers no longer listed, so each client role passes
  a prefix of its own (`runner-`, `bench-`) and never removes another's.
- **State is the front's `state.json`, under a lock.** Port blocks, stacks
  and their states. A caller's own lock is taken without waiting before
  its bundle is spooled, so a second request of the same caller is refused
  at once rather than piling up connections and disk; the page and the
  reaper wait for it. A
  stack left `starting` past `STARTING_MAX_S`, two hours, is a front that
  died, and the reaper treats it as stopped.
- **The dev container proves the whole chain** (`make converge TAGS=executor`, then `make smoke`): the accounts, the units, a caller, a
  bundle, a stack answering on its port, a suite, a refused privileged
  profile and a down. Its subordinate ranges are too short for an image
  that owns files as nobody (65534), such as busybox's `/home`, so the smoke
  test uses alpine.
