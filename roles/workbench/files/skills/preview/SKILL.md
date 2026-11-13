---
name: preview
description: Starts the repository's dev server on the workbench's preview port, where the user opens it from their phone and the roles reach it on localhost, and stops it when done. Use when the user would want to try what was built or asks to, and before the tester, the UX designer or the UI tester needs a running service.
---

# The workbench's preview

One port per repository: whatever serves on
`0.0.0.0:$WORKBENCH_PREVIEW_PORT` in this container is at
`$WORKBENCH_PREVIEW_URL` on the user's private network, and nothing else started
here is reachable by them.

1. **Check the port is free**: `curl -s localhost:$WORKBENCH_PREVIEW_PORT`.
   The repository's sessions share it; if another session's preview holds
   it, say so and ask the user rather than taking it.
1. **The lead starts it, never a role.** A server lives as long as the
   session that started it, and a role's session ends with its answer.
   Start the dev server with the Bash tool's background option, on that
   port and on all addresses, not `127.0.0.1`; never detach it with
   `nohup`, `setsid`, `disown` or a trailing `&`, which would leave it
   holding the port until the container restarts.
1. **Check it answers**: `curl -fsS http://localhost:$WORKBENCH_PREVIEW_PORT/`.
   That is the URL to give a role; the user's is `$WORKBENCH_PREVIEW_URL`.
1. **Give the user the link** when they would want to try it, and **stop
   the server** when they and the roles are done with it.

## On the executor

Where `STACK_KEY` is set, the repository's stacks run on the host's
executor, and the preview is better there: the whole compose stack, from
the checkout's HEAD, on ports of the repository's own.

1. **Commit first.** `stack up` sends HEAD as a git bundle and leaves out
   what is not committed; it warns when something is.

1. **The repository says how it comes up**, in `stack.toml` at its root:
   `[profiles.<name>]` with `compose` (the compose files),
   `compose_profiles` (compose profiles to turn on), `page` (which port is
   the page, 1 to 3) and `env` (the stack's `.env`, also for a service's
   `env_file: .env`, where
   `{port1}` to `{port3}` are the repository's ports, `{bind}` all
   addresses, `{host}` the executor's host and `{url}` the page's origin
   as the user opens it, `https://` where the executor has it), and optionally
   `[suites.<name>]` with `service` and `command` for `stack run`. The
   compose files publish only those ports, from variables. What the
   executor refuses (privileged containers, added capabilities, devices,
   the host's network, bind mounts from outside the repository or writable
   ones, ports not the repository's) it says; never work around it. Mount
   the repository `:ro` and keep what a service writes in a named volume.

1. **Bring it up**: `stack up`, or `stack up --profile <name>`. It prints
   the user's URL, the one from here and the phone page's. `stack logs [service]` shows why it failed; status 75 is no room, or a paused
   executor: say so and go on with the dev server above.

   **A failure is reported in full**, never only in your own words: the
   command as run, its exit status, the `stack: failed (request <id>): ...`
   line as `stack` printed it, the time it failed in the user's timezone
   (`date`, with the zone's name), and for a failed up the tail `stack`
   printed, the failed service's log. The request id finds every line of it
   in the executor's journals; whoever runs the host needs nothing else.

1. **Check it answers** with the URL from here, and give that one to the
   roles; the user's is the other.

1. **End-to-end suites**: `stack run <suite>`.

1. **A page without one service**, its error state: `stack stop <service>`,
   then `stack start <service>` to bring it back; `stack up` starts them
   all.

1. **Take it down** with `stack down` when the user is done; otherwise it
   is stopped after hours unused, and the phone page starts it again.
