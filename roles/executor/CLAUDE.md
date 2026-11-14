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
- **A remote caller is its key and its addresses, nothing else.** The
  TLS port (`remote.socket`, `serve-tls`) runs the same front, with no
  uid: `authenticate` takes a caller with `from` only from its networks
  over TLS, and one without only on the socket, so a local key that leaks
  to another host is useless there and the other way round. The front
  wraps the connection itself and bounds each read by
  `remote_timeout_seconds`, `REMOTE_TIMEOUT_S` by default; a bundle cut off leaves no spool. The certificate
  is pinned by the callers, so `stack` checks its hash before it sends the
  key, and never connects with neither a pin nor a CA. Its key is
  `root:<name>-executor`, the front's to read and no one else's.
- **Answered means exit 0.** `serve` and the agent's `main` return 0 once
  the answer is out, whatever its status, which the answer and the
  journal line carry; only an agent out of reach (`UnreachableError`), an
  answer to a request that could not be written, or an exception before
  it, fails the unit. A peer gone before it asked, or with no TLS, is
  logged and exits 0: it is the peer's doing. A host that alerts on failed units then alerts on a broken
  executor alone. Keep it so: a new refusal is an answer, never an exit
  status.
- **The request id is the front's, never the request's.** `serve` sets
  `REQUEST_ID` from the unit's instance in `/proc/self/cgroup`, or a random
  one, puts it in every line of the answer over whatever the agent's
  events said, starts every journal line with it (`journal`), and hands it
  to the agent with each request (`with_request_id`); a caller's own
  `request` field is never read. The agent starts its journal lines with
  the id it was given, and refuses one that is not an id.
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
  service's `env_file: .env` reads, less the values that hold a secret; it
  replaces the repository's own, and a link there is removed, never written
  through (`write_tree_env`).
  The namespaces are an allow list (`OWN_MODE`); what a service may never
  set is `NEVER` in `executor_agent.py`. A new compose key that widens a
  container's reach goes there with a test before anything relies on it.
- **A secret reaches only its repository's stacks, and never the tree.**
  The play writes `secrets.json`, root's and the front's to read, with
  `no_log`, its own file so `instance.json` stays printable. The front
  adds to an up the secrets of the request's repository alone
  (`Instance.secrets_for`). The agent fills `{secret:<name>}` into the
  stack's `stack.env`, keeps the values in `secrets.json` beside it, and
  writes the tree's `.env` without the keys that hold one
  (`without_secrets`). The tree is the build's context and a service's to
  bind-mount. `stack.json` keeps their names, never their values. The
  agent's config stays free of secrets.
- **Every word the agent sends passes `Out.clean`.** The up hides the
  values it was given, and every action on a stack hides the stack's own
  (`stack_meta`), replacing each value with `{secret:<name>}`. A new path
  to the caller or the journal goes through `Out`, never around it. A
  build holding a value, in an arg, a label or anywhere else, is refused
  (`secret_build_refusals`).
- **`https_port` is the host's, never the stack's.** The front sends it
  with an up; the agent fills `{url}` from it (`page_origin`), refuses a
  `page` other than 1, since the host's proxy forwards to the first port
  whatever the profile, and refuses a service publishing it
  (`https_refusals`). The collection never configures the proxy.
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
- **The images a stack builds are its own** (`own_images`). Every caller
  builds in the exec account's one image store, so the checked config
  names each built image `<project>-<service>`, whatever the file says, and
  a service running a built image by the file's name gets the stack's
  name for it. A service may not run a `stack-` image it does not build
  (`stack_image_refusals`): that is another caller's.
- **An up keeps the volumes only for a commit that follows the last**
  (`followed_commit`): the same repository, and the last stack's commit
  an ancestor of the new one in the new tree. A caller is a runner slot
  or a bench, not a branch; another branch's data was made and migrated
  by code the new commit lacks. A commit the bundle does not carry counts
  as another branch's, so a rebase or a force-push starts empty; a
  refused up removes nothing, and a volume left after the removal fails
  the up rather than start on it. A shallow checkout's bundle is a
  parentless commit dated as HEAD, the same for the same HEAD.
- **The checkout runs no git config of the repository's.** The bundle is
  fetched into a fresh `git init` with `core.hooksPath=/dev/null` and
  `fetch.fsckObjects`, then checked out detached; nothing runs in the tree
  but compose.
- **The checkout runs under umask 022**, the agent under 077. A
  Dockerfile's `COPY` keeps the tree's modes, so a `0600` file in the tree
  is one the image's non-root user cannot read; `TREE_UMASK` sets git's
  usual ones for the checkout alone. The tree sits in the exec account's
  home, which no other account enters.
- **A service's stop and start are compose's own**, from `checked.json`
  (`Agent.service`), for a person trying a page without one service. They
  start nothing else and are not watched; the front keeps the stopped
  names in `stopped_services` for `list` and the page, and an up or the
  page's start, which start everything and watch it, clear them.
- **An up and a start watch the stack** (`Agent.watch`), not compose's
  `--wait`, which fails a stack on any container that ends, status 0 too,
  but one another service waits for. After `up --detach` the agent polls
  the containers until every one runs, healthy where it has a check, or
  ended with 0, and has stayed so `SETTLE_SECONDS`, up to `WAIT_SECONDS`,
  on a monotonic clock, and until the page answers HTTP, where a service
  publishes the profile's `page` port (`page_of`, `page_answers`): a
  service with no health check can run past the settle time and still die
  before it serves. The agent asks the page inside its container's network
  namespace (`podman unshare nsenter --net`, then its own `probe-page`),
  never on the host's loopback, which the host's sandbox keeps the exec
  account off, as it must: a port open to the account is open to every
  stack's pasta. The probe's own failure, other than `PROBE_NO_ANSWER`,
  fails the watch after `PROBE_ERRORS_MAX` in a row, so a host where
  nsenter cannot join says so rather than wait out `WAIT_SECONDS`. A
  stack recorded before `page_port` keeps the settle rule alone. A restart policy brings a dying service back
  before a poll may see it ended, so a container whose `Restarts` grew
  during the watch has failed, and podman's `stopped` is judged by its
  exit code. `compose run`'s leftovers (`ONEOFF_LABEL`) are no service. On
  a failure, the watch's own included, it answers with the failed
  services' log tail and stops the stack, even when the logs cannot be
  read. The smoke test's `app`, its `broken` and `late` profiles and the one-shot
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
  a prefix of its own (`runner-`, `bench-`, `try-`) and never removes another's.
  A removed caller's stack is the reaper's at once: the play starts
  `executor-<name>-reap.service`, as `instance_setup.yml` does for a
  remote caller dropped from `remote_callers`, and `list` and the page
  leave out a caller no longer registered. A caller moved to another
  instance never leaves a stack with no one to take it down.
- **State is the front's `state.json`, under a lock.** Port blocks, stacks
  and their states. A caller's own lock is taken without waiting before
  its bundle is spooled, so a second request of the same caller is refused
  at once rather than piling up connections and disk; the page and the
  reaper wait for it. A
  stack left `starting` past `STARTING_MAX_S`, two hours, is a front that
  died, and the reaper treats it as stopped.
- **A person's stack is never evicted** (`waits` in the front). The
  executor cannot see a page in use: `podman stats`' counters, measured,
  neither saw traffic through the port forwarding nor stayed still
  without it. So a caller that does not wait keeps its place until its
  down or `idle_hours`, eviction picks only among callers that wait, and
  `waiting_reserve` caps people's stacks so a job still finds room. A
  caller no longer registered counts as one that waits.
- **The dev container proves the whole chain** (`make converge TAGS=executor`, then `make smoke`): the accounts, the units, a caller, a
  bundle, a stack answering on its port, a suite, a refused privileged
  profile and a down. Its subordinate ranges are too short for an image
  that owns files as nobody (65534), such as busybox's `/home`, so the smoke
  test uses alpine.
