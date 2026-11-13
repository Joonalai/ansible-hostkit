---
name: preview
description: Brings the checkout up as this runner's stack on the executor, the preview the user tries a change on before merging, and runs the repository's end-to-end suites against it. Use, as the lead and never as a role, whenever a web service's change should be tried, and before the tester, the UX designer or the UI tester needs a running service.
---

# The runner's preview

The preview runs on the executor, as this runner's stack, from the
checkout's HEAD. `~/.claude/CLAUDE.md`, under "Your stack", says whether
this repository has one; without it there is no preview, and nothing here
applies.

1. **Commit first.** `stack up` sends HEAD as a git bundle, commits not
   pushed included, and leaves out what is not committed. Commit on your
   branch, as you would to push.

1. **The repository says how it comes up**, in `stack.toml` at its root:

   ```toml
   [profiles.default]
   compose = ["compose.yaml"]          # the compose files, in the repository
   compose_profiles = ["seed"]         # optional: compose profiles to turn on
   page = 1                            # which of the runner's ports is the page
   env = { WEB_PORT = "{port1}", API_PORT = "{port2}", BIND = "{bind}" }

   [suites.e2e]                        # optional: `stack run e2e`
   service = "e2e"                     # a service of the compose files
   command = ["pytest", "tests/e2e"]   # run in it, against the stack
   ```

   `env` is the stack's `.env`, which docker-compose reads for the compose
   files' `${...}` and a service's `env_file: .env` reads too, in place of
   the repository's own: `{port1}` to `{port3}` are this runner's ports on the
   executor, `{bind}` all addresses and `{host}` the executor's host. The
   compose files publish only those ports, on all addresses: a host port
   comes from a variable (`"${WEB_PORT:-8000}:8000"`), and what the user
   need not open gets none. Without a `stack.toml`, or with fixed ports or
   `127.0.0.1` in the compose file, add the change to the PR: it is how
   every job and the user get a preview of the repository from now on.
   With no compose file at all, write one that builds the repository's
   `Dockerfile`, or runs the service's own start command on a stock image,
   with what the service writes in a named volume.

1. **Keep its build cheap.** Every `stack up` builds the images on the
   executor, one at a time, keeping the layers between ups. In the
   repository's Containerfile, copy the manifest and lock file alone and
   install from them before copying the source (`uv sync --frozen --no-install-project`, `bun install --frozen-lockfile`), so a change
   rebuilds only what it changed. Services that run one image share one
   `build:` through a YAML anchor, without an `image:` name, which would be
   shared with the executor's other callers. A Containerfile that copies
   the source first is worth fixing in the PR.

1. **The preview shows what the change changed.** A compose file that
   leaves out the part the change is in, such as a web front end it does
   not serve yet, is a preview of nothing the user can try: add that part,
   a service that builds it and serves it on the page's port, pointed at
   the API the compose file runs, as a profile of its own when the
   repository's default should not have it.

1. **What the executor refuses** it says, and why: privileged containers,
   added capabilities, devices, the host's network, a bind mount from
   outside the repository or a writable one (mount the repository `:ro`,
   and keep what a service writes in a named volume), a volume or network
   not the stack's own, and a port not this runner's. It checks the
   services the stack starts, and those of a compose profile only when
   `compose_profiles` turns it on. What the repository needs that it refuses is
   for the PR to say, never to work around. `start_interval` and fixed
   `container_name`s the executor resets by itself.

1. **Bring it up**, from the checkout:

   ```sh
   stack up
   ```

   or `stack up --profile <name>` for another profile. It builds and
   starts the stack, replacing this runner's last one with its volumes
   kept, and prints the URL for the user, the one for the roles, from
   here, and the phone page's. `stack logs [service]` shows why one that
   failed did. Status 75 is no room on the executor, or a paused one: go
   on without the preview, and say so in the PR.

1. **Check it answers** before saying it does, with the URL from here:

   ```sh
   curl -fsS http://host.containers.internal:<port>/
   ```

   That is the URL the roles use. The user's is the other one.

1. **End-to-end suites run there too**: `stack run <suite>` runs one the
   repository's `stack.toml` names, in its service, against the stack, and
   prints its output.

1. **Say where it is**, with the user's full URL, in the PR description. It
   stays up until this runner's next stack, or until nobody has used it
   for hours, when it is stopped and the phone page starts it again. When
   a later job continues the PR on another runner, the URL moves: update
   it.

If the service needs what the executor refuses, a host device, another
service on this box, say so in a comment rather than working around it.
