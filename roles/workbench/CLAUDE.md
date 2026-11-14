# The `workbench` role

Notes for working on this role. The collection's conventions are in the
root [CLAUDE.md](../../CLAUDE.md), which applies here too. The account's
Claude Code is the [claude_code](../claude_code/CLAUDE.md) role's, the
team is the [claude_team](../claude_team/CLAUDE.md) role's, and the
runner it shares a preview range with is
[github_runner](../github_runner/CLAUDE.md).

- **The gate is the boundary, so it takes nothing on trust.** The write
  token pushes as the user, a guard that watches for pushes by others does
  not fire on the owner's, and GitHub Free gives a private repository no
  branch protection: whatever reaches that token can push the default
  branch, which for a repository that configures a host may be what gets
  deployed as root. So the token is readable by `benchgate` alone, `bench`
  reaches it only through a socket per repository that systemd answers
  with one root-owned script, and that script checks every name, commit
  and word it is handed. Keep it that way: a new action is a new check,
  with its tests, never a pass-through.
- **The repository is the socket's.** `workbench-gate-<name>.socket` runs
  `workbench-gate serve <name>`, and is mounted into `workbench-<name>`
  alone; the client sends only the action and its arguments. A name in the
  request would let one repository's sessions push to another's. The gate
  also checks the peer's uid, `SO_PEERCRED`, against `bench`'s: a
  container's root is a subordinate uid on the host, and is refused even
  before that by the socket's mode.
- **The gate never runs git in the workbench's checkout.** A repository's
  `.git/config` and hooks are its owner's to write, and git in a repository
  runs what they say: `core.fsmonitor`, `core.sshCommand`, a `pre-push` hook.
  Run as `benchgate` in `bench`'s checkout, any of them would hand over the
  token. So what crosses is a bundle on the socket, fetched into the gate's
  own bare mirror with hooks off and fsck on, and git's own
  `safe.directory` check is a second line, not the first.
- **The token is never an argument.** Any account can read any process's
  command line from `/proc`; only its owner can read its environment. The
  gate hands git the token as `GIT_CONFIG_*` variables, and a test checks
  that no argument it runs carries it.
- **`--force` replaces only what the gate itself pushed.** The gate keeps
  `refs/pushed/<branch>` in its own mirror, and a non-fast-forward goes
  through only while GitHub still has exactly that commit. Nothing the
  session says counts: a lease it sent could name any commit it had
  fetched, so a session talked into it could rewrite a branch the user
  pushed from the workstation. A fast-forward onto someone's branch needs
  no record; it adds commits and loses none. A wiped mirror forgets the
  record, and the branch can then only grow until it is deleted.
- **Merges are checked like any commit, with `diff-tree -m`.** Without it
  git lists no paths at all for a merge commit, and a merge can add a file
  neither parent had. Debian trixie's git 2.47 shows it, and a test
  reproduces it.
- **The workflow token is the one thing that changes what CI runs.** It
  is a repository's only with `workflows: true` and
  `workbench_workflow_token` set, and only a push whose new commits touch
  `.github/workflows/` uses it, after every other check. Without it the
  gate pushes as usual and GitHub refuses, so the other repositories need
  no check of their own. A change that only removes a workflow commit
  touches nothing new and is refused the same way; the user pushes that
  one.
- **A public repository's PR opens as a draft.** The gate reads
  `private` from the repository's own answer, which it fetches for the
  default branch anyway, and anything but `true` counts as public. There is
  no action to mark one ready: that is the user's, on GitHub.
- **Privacy is enforced at the gate, not only asked for.** The account's
  settings turn off Claude Code's attribution, and the rules say not to
  write any; a subagent writing a commit message is still the likely leak.
  The gate refuses a commit by anyone but `workbench_git_email`, any message
  or PR text matching `workbench_forbidden`, and any commit touching
  `workbench_forbidden_paths`, `.bench/` and the session's permissions
  among them. Both of those are kept out of git by the clone's
  `.git/info/exclude`, never by a `.gitignore` line, which would itself say
  the workbench was there.
- **The container is for ease, the account is the boundary.** Every
  container mounts the whole home, which is where Claude Code, its login
  and every checkout are, so a session can leave something there that the
  host runs as `bench` (the `claude_code` role runs the binary). That
  reaches nothing new: the host's sandbox fences `bench` and its range
  alike, and the sockets are all of the gate there is. What the container
  buys is root for apt-get, a `localhost` and `/tmp` of its own, and no
  prompts. `~/.config` is mounted read-only over the home anyway, so a
  session cannot quietly change how its own container starts;
  `XDG_CONFIG_HOME` and `GH_CONFIG_DIR` point elsewhere for the tools that
  write config.
- **One image for every repository, slowest layer first.** Chromium and
  its graphics stack are a gigabyte, so the base apt-get comes first and a
  repository's `packages:` come last: adding one then rebuilds one layer
  in seconds rather than the whole image in minutes. Node is its release
  tarball, `workbench_node_version`, checked against its checksums, rather
  than Debian's npm, hundreds of packages to unpack, which roughly halves
  a cold build. Adding a repository's packages rebuilds the image, and the
  play restarts the containers when the image's ID changes. apt runs as
  root inside (`APT::Sandbox::User`): its own drop to `_apt` and nogroup
  fails where the subordinate range is short, as in a test container.
- **The image is rebuilt every night, afresh every week.**
  `workbench-rebuild` (`files/rebuild/`) builds with `rebuild-image`, the
  `rootless_account` role's script the runner shares, from the play's own
  `build.json`, which the play builds with too, so the two never
  differ. That adds the ISO week as `REFRESH`, declared before the first
  layer: a new week misses the cache for all of them, which is how
  Chromium's security updates arrive at all, the base layer being cached
  otherwise. The timer, at `workbench_rebuild_time`, then restarts a
  container still on an older image only if no session is open in it, a
  session being a process with `--sdk-url` in its arguments, as
  `podman top` shows them; a busy one waits for the next night. Enabled
  only where the containers run.
- **Previews share the runner's range, from the top.** One port per
  repository, `workbench_preview_ports_start` plus its place in the list,
  published from the container's `workbench_preview_port` by pasta, which
  runs as `bench`. The host's sandbox lets `bench` answer only from the
  preview range, so a port outside it would be silently unreachable. Where
  a runner shares the host, the runner's play asserts its slots end below
  the workbench's start, and the workbench's that its last port is in the
  range. Reordering the list moves the ports.
- **A second Claude Code account is a second play.** `claude_code` is
  written for one account. Its tasks and includes take the account from
  `claude_code_user`, and its facts (`claude_code_user_env`,
  `claude_code_logged_in`) are host facts. So the consumer's playbook runs
  the workbench in a play of its own, with the account as play vars, which
  outrank the inventory and reach every include and handler there. A role
  parameter would not: an `include_role` of `claude_code` from this role
  would fall back to its default account.
- **The dev server is the lead's to start, through the `preview` skill.**
  `files/skills/preview/` is the workbench's own, installed beside the
  team's shared skills under the same name as the runner's compose
  procedure; `walkthrough` and `pending-review` beside it are the two
  on-request procedures that would otherwise sit in the rules every turn
  carries. A server a role starts in the background ends with the role's
  session, so the rules have the lead start it before calling `tester`,
  `ui-tester` or `ux-designer`, and hand them the `localhost` URL.
- **A repository with `executor:` is the instance's caller**, `bench-<name>`,
  registered through the executor role's `callers.yml` with the prefix
  `bench-`, `waits: false`: a person is refused at once rather than
  queued. Its key is in `~/.config/workbench/<name>-stack.env`, `0600`
  bench's, which only that repository's unit reads as an
  `EnvironmentFile=`, with the instance's `api.sock` mounted at
  `/run/stack.sock`, or for a remote instance in
  `executor_remote_instances`, its URL and certificate hash in the env file
  and no socket. The other containers share the home but see neither
  file nor socket in their environment; a session could still read
  another repository's env file from the home, which is the same account
  and so the same boundary, and reaches only that repository's stack. The
  `preview` skill's executor half applies where `STACK_KEY` is set.
- **A try is a caller of its own, never a repository's preview.**
  `workbench_try` registers `try-<instance>` through `callers.yml` with the
  prefix `try-`, so neither prefix prunes the other's, and its env file
  is in `~/.config/workbench/try/`, a directory no repository's name can
  reach. Every container mounts each local try instance's socket at
  `/run/stack-try/<instance>.sock`, which the env file names. The `try`
  skill runs `stack` with `env -i` and that file alone, so the
  container's own `STACK_URL` cannot send a try to the wrong instance,
  and refuses a fork's pull request, which would run with what the
  instance gives the repository. A repository's `instructions` in
  `try.json` are the one text of a try the skill follows, since the
  person wrote them in the inventory: `try_repos.yml` checks the two
  shapes and lays them out, and nothing in a repository or a pull request
  can add to them.
- **A repository's `instructions:` are a section of the one CLAUDE.md.**
  Every container shares the home and its rules, so a repository's
  instructions from the inventory go in under
  `## The repositories on this box`, one `###` per repository, with a line
  saying which section holds in which container. Mounting a file per
  container over `~/.claude/CLAUDE.md` would keep the others out, but a
  bind mount of one file keeps the old inode when the play replaces it, so
  the sessions would read the rules the container started with. A new
  session reads the shared file as it is now.
- **Parallel sessions are worktrees, so nothing a session needs may live
  only in the checkout.** The server runs `--spawn worktree`, and a
  worktree gets the tracked files and nothing else. So the settings are
  in the account's `~/.claude/settings.json`, not a `settings.local.json`;
  the notes are in the main checkout's `.bench/`, which the rules name by
  its absolute path; and the exclude file is the clone's
  `.git/info/exclude`, which every worktree reads. A new piece of state
  goes in one of those, or it works in one session out of
  `workbench_capacity`.
- **The host's sandbox fences `bench` as it does `coder`, by number.** The
  collection never changes the firewall. The host's own hardening has to
  hold `workbench_uid` and the pinned subordinate range so the sessions
  reach DNS and the internet, and nothing on the host, its LAN or its
  private network but the runner's preview range. The range matters as
  much as the uid: unfenced, any user but root in a container `bench`
  started could reach every service on the host, since podman is there for
  anyone. The role passes `workbench_sandbox_units` to `rootless_account`,
  which makes `bench`'s manager, and so every session and container,
  `Requires=` them, and it refuses to run with none. In a test container
  the range may have to be another account's, as the runner's may: a uid
  past 65536 does not exist in there, and a firewall rule naming one can
  make the firewall reject its whole rule file and leave the container
  with no network.
- **What a session can still do is in the threat model**: send out what
  it can read, run code in CI with a repository's secrets, since its
  branches are the repository's own, and be told what to do by a pull
  request it tries or reviews, which only the rules and the `try` skill
  answer. A new check in the gate should say
  which of those it narrows; none of them is closed by this role alone.
- **A test container proves everything but GitHub and Remote Control.**
  It has no tokens and no login (`workbench_tokens_required: false`), so it
  clones nothing and starts no container; the play still builds the image,
  starts the sockets and asks each gate through its socket, which answers
  that it has no token. A container shaped like the unit, with a shell in
  place of Claude, is
  `podman run --rm --userns keep-id -v /home/bench:/home/bench -v /home/bench/.config:/home/bench/.config:ro -e HOME=/home/bench -v /run/workbench-gate/example.sock:/run/workbench-gate.sock localhost/workbench:latest bash`,
  as `bench` inside the test container. The gate itself is the pytest suite
  beside it, against a local bare repository standing in for GitHub.
