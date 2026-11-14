# The `github_runner` role

Notes for working on this role. The collection's conventions are in the
root [CLAUDE.md](../../CLAUDE.md), which applies here too. The team the
jobs call is the [claude_team](../claude_team/CLAUDE.md) role's, and the
account is set up by `rootless_account`.

- **One container per job, and the unit is the loop.** The runner is
  registered with a JIT config, which makes it ephemeral: it takes one job
  and exits. `Restart=always` brings the unit back, `ExecStartPre` mints a
  new config, and quadlet's `--rm` has already thrown the old container
  away with everything the job wrote. A container the job started on the
  runner's own network (`--network container:…`, a test database on
  `localhost`) would block that removal, and the next start's `--replace`,
  for good, so the unit removes the runner with `--depend` first. So a
  busy runner picks up a changed unit, image or CLAUDE.md with its next
  container by itself. An idle one would wait for a job on the old, so the
  play runs `refresh-runners` (`files/refresh/`), which restarts only the
  runners on something old and with no `Runner.Worker` running. Never
  restart the units by hand without checking: a restart kills the job in
  progress, with everything it did.
- **The image is rebuilt every night, afresh every week.** The play
  writes the build's arguments to a root-owned `build.json` and builds
  through `rebuild-image`, the `rootless_account` role's script, and a
  timer of `coder`'s runs the same at `github_runner_rebuild_time`, then
  `refresh-runners`. The script adds the ISO week as `REFRESH`, declared
  before the first layer, so the first night of a week misses the cache
  for every layer: that is how Debian's security updates, Chromium's among
  them, reach the image at all. The runner and Claude Code versions stay
  what the play last read; a new runner release still needs the play.
- **Cheap lines go at the end of the Containerfile.** A changed or new
  layer rebuilds every layer below it, the runner's download and Claude
  Code's install among them when it sits above those. An `ENV`, a small
  `COPY` or a `RUN` that only writes a file goes at the end of the file,
  just before `ENTRYPOINT`, unless a layer above needs it.
- **Two accounts, so a job cannot reach the key that registers runners.**
  The runner App's key can manage runners on every repository it is
  installed on, which with Administration permission means far more than
  runners. It is readable only by `ghregistrar`. `coder`, the account the
  containers run as, reaches it only through one sudo rule naming one
  root-owned script, and that script takes a name from
  `/etc/github-runner/registrar.json`, never a repository. So an escape
  from a container to `coder` can register runners for the listed
  repositories, and nothing else. Keep the unit's `NoNewPrivileges=` in
  `[Container]` only: on `[Service]` it would stop `ExecStartPre`'s sudo.
- **The registrar script is the one place Python runs as another user.** It
  runs under `python3 -I`, and sudo resets the environment, so nothing
  `coder` sets reaches it. It uses only the standard library and the
  `openssl` binary, so the system Python needs nothing installed.
- **Test services go through coder's podman socket, and paths match on
  both sides.** The job's compose CLI talks to `coder`'s podman over the
  mounted socket. The services it starts belong to `coder` and outlive the
  container. Bind-mount sources resolve on the host, so the work folder,
  `~coder/work/<slot>`, which the registrar names in the JIT config, is
  mounted at its host path: were
  the checkout the container's own, a compose file binding it, as a
  repository's dev servers or ansible container do, would get an empty
  directory. GitHub keeps an absolute `work_folder` as given. The unit
  empties the folder before and after each container, under
  `podman unshare`, so nothing of one job, its tokens in `_temp` included,
  reaches the next. The rules say privileged containers and a missing
  `.env` are the job's to deal with, and allow `make` and `ssh-keygen`,
  since a job left to guess stops there. `UserNS=keep-id:uid=1000` makes
  the runner `coder` on the host, so the socket is the job's to use and
  what it writes there is `coder`'s.
- **The socket is everything `coder` can do, so the account is the
  boundary.** A job can stop or `exec` into any container of `coder`'s,
  another repository's running job and its tokens included, read every
  slot's `gpu-models.env` and `stack.env`, and so act on the executor as
  any slot, write `~coder/image`, the units and every
  repository's cache, and leave a container mounting its own work folder
  that reads the slot's next checkout. Not the slot or the repository:
  what stops a job is the host's sandbox, the registrar's sudo rule and
  the vouch. The rebase runner alone gets no socket. The role has no socket
  proxy and no account per repository, which is sound only while
  everything a job can reach is the same owner's development work and what
  runs in a job is vouched for, so the likelier way in, a poisoned
  package, would run as `coder` anyway. That holds on two conditions. The
  App Claude acts as stays off the repository that configures the host and
  off the shared workflow's, since its key, which a job could take from
  another's mint step, mints tokens wherever the App is installed. And no
  served repository has outside contributors or secrets worth more than its
  own job token; one that does reopens the question.
- **`claude-job-strays` runs after Claude, before every step that mints
  or uses a token.** A step's leftover processes live until the job ends,
  so a background process Claude started could read
  `create-github-app-token`'s `INPUT_PRIVATE-KEY`, the App's key, from
  `/proc`. It kills every process of the account off its own line of
  ancestors, again until none is left, since one that forks while `/proc`
  is read is missed by one pass, and fails the job when some will not
  stop; `claude-job-rebase-runner settle` does the same for itself.
- **Related repositories are a GitHub variable, not an inventory key.**
  The token is minted by the workflow before any of the image's code runs,
  so `CLAUDE_RELATED_REPOS`, a repository variable, is the one list both
  can read: the workflow for `repositories:`, and through the steps'
  environment `claude-job-level` for the lead's prompt, `claude-job-clone`,
  `claude-job-pr-comments` and `claude-job-finish`. With it unset, a job
  behaves as if none of this existed. A clone goes to `../<name>`, in the
  runner's work folder, because `actions/checkout` writes only inside the
  workspace, which is why `settings.json` lists the work folders. Its
  `credential.helper` is `gh auth git-credential`, set in the clone's own
  config: gh reads `GH_TOKEN`, so `claude-job-finish` pushes with the fresh
  token after Claude's expires, and no token sits in a remote's URL.
  `claude-job-pr-comments` exists because the action's comment filter
  covers only the triggering PR. It filters by numeric id, and is on the
  vouch hook's list of what waits for a check. Each script parses the list
  itself: they run under `python3 -I` from `/usr/local/bin`, with nothing
  to import from one another.
- **`prek` is in the image for a repository's commit-msg hook.** `prek run`
  never runs that stage, so without the hook a refused message gets as far
  as CI. The workflow installs it in the checkout and `claude-job-clone` in
  a clone, only that hook type: a pre-commit stage would run on every fold
  and rebase of a job.
- **Bun is in the image at `github_runner_bun_version`.** The workflow
  skips `setup-bun` when the caller's `bun-version` is the image's, since
  GitHub's release downloads fail with a 504 now and then and take the job
  down before Claude starts, where nobody sees it on the PR. Keep it at
  what the callers pin: another version is downloaded as before.
- **The host's sandbox is the network boundary, and it matches numbers.**
  The collection never changes the firewall. The host's own hardening has
  to reject what `coder` and its whole subordinate range send to the host,
  its LAN and its private network, except DNS, replies from the slots'
  ports, the stack ranges of the executor instances its repositories
  name, and, where a repository opts into `gpu-models`, the model server
  at `github_runner_gpu_models_*`. The range matters: a
  `network_mode: host` service runs as a subordinate uid, not as `coder`.
  Hence `github_runner_uid` and `github_runner_subuid_start` are pinned,
  so the sandbox can name them before the account exists, and set in the
  inventory where the firewall reads them too. The role passes
  `github_runner_sandbox_units` to `rootless_account`, which makes
  `coder`'s manager `Requires=` them, so no job runs without them, and it
  refuses to run with none. Test a sandbox with everything a job can do:
  plain `coder`, a job container through pasta, a host-network service,
  and a squatted port outside the range. All should be refused, except the
  slot's own ports, the executor's stack range and the internet.
- **Memory is capped twice.** `MemoryMax=` on the runner unit,
  `github_runner_memory_max`, covers the job. The test services run under
  `podman.socket`'s service, not the job's unit, so a drop-in on the
  system's `user@<uid>.service` caps the whole account,
  `github_runner_account_memory_max`, and its swap. Leave the swap bound
  in: unbounded, a full account pages instead of failing, every runner's
  `Runner.Listener` stalls with it, and after ten minutes GitHub fails
  every running job as "lost communication", which looks like a network
  outage and is not one.
- **Its own subordinate range, except on a test container.** On a real
  host it should not overlap any other account's, so two sets of
  containers share no IDs. A test container may have only one usable
  range, and can give `coder` another account's range and build options
  (`github_runner_build_options`). That overlap is acceptable only because
  nothing there runs a real job.
- **`github_runner_start: false` on a test container**, which has no App.
  The play still builds the image, asks its runner for its version, writes
  the units and checks the generator made them, and leaves `[Install]` out
  so nothing starts at boot. The sudo rule and the registrar can be
  exercised by hand there. They fail at signing, with no key, which is
  expected.
- **The end-to-end check is GitHub's list of runners.** The play asks,
  through the registrar, for each repository's runners, and waits for one
  to be online for every slot, through
  every page of the list: a runner killed while idle stays listed offline
  for a while, and a busy repository can have hundreds, which hide its
  online ones past the first page. The registrar reads a runner's slot from
  the end of its name, `-<name>-<slot>-<time>`, whatever its prefix: a busy
  runner keeps the name it registered with until its job ends, so after a
  rename a slot still serving a job counts under its old one, and the
  slot's next registration deletes its offline ones of either name. That
  one check covers the sudo rule, the key, the App's installation and
  permission, and the container having dialled in.
- **Watch the listeners, not the jobs.** A host that monitors the runner
  should count `Runner.Listener` processes as `coder`, and alert when fewer
  listen than `github_runner_slot_count` for a while; `Runner.Worker` counts
  the jobs. A runner that cannot register loops every ten seconds with the
  reason in its journal
  (`journalctl --user -u github-runner-<name>-<slot>` as `coder`), which
  the unit repeats from the registrar's own output.
- **The JIT config goes to the runner in its environment, never its
  command line.** It is the runner's credential for as long as it lives,
  and `/proc/<pid>/cmdline` is readable by every account on the host, a
  monitor's `pgrep` among them. The runner reads
  `ACTIONS_RUNNER_INPUT_JITCONFIG` as it would `--jitconfig`.
- **Claude Code is in the image, at the action's pin.** The play reads
  `claudeCodeVersion` from the action's `run.ts` at
  `github_runner_claude_action_ref`, bakes that version in, and asserts the
  image has it; the workflow points the action at it with
  `path_to_claude_code_executable`. Downloading it in every job gets jobs
  refused with 429 before Claude starts. When the action moves its pin,
  the next play run rebuilds; until then jobs run the older one, which the
  action warns may miss its newest features.
- **The team is files, the process is the lead's rules.** The roles the
  lead calls are the `claude_team` role's `files/agents/*.md`, Claude Code
  subagents with their own model and tools, shared by every repository and
  the workbench, and mounted read-only. What everyone in a job follows is
  `templates/CLAUDE.md.j2`, mounted as `~/.claude/CLAUDE.md`, which Claude
  Code loads into the lead and every subagent alike. The lead's own rules,
  the levels, the PR format and when to stop, are `templates/lead.md.j2`,
  which the workflow appends to the lead's system prompt with
  `--append-system-prompt-file`. Keep them apart: a lead rule in CLAUDE.md
  is carried through every turn of every role, and a role reading how to
  push and open PRs may do it. What Claude may run without asking is in
  `files/image/settings.json`.
- **The preview is the executor's, and the slot is its caller.** A
  repository whose entry names `executor:` has each work slot registered
  with that instance through the `executor` role's `callers` tasks, as
  `runner-<slot>`, with coder's uid, the slot's repository and `waits`,
  since a job waits for a place where a person would not. The key lands in
  the slot's `stack.env`, root's and coder's to read, handed to the runner
  by the unit's `EnvironmentFile=` as `gpu-models.env` is, and the
  instance's socket is mounted at `/run/stack.sock`; `stack`, the
  executor's client, is in the image. So a job's preview needs nothing of
  coder's podman, and it outlives the slot's next job only as far as the
  stack does: one per slot, replaced by its next `stack up`. There is no
  second, in-job way to start a preview, and parking a PR's preview is a
  plan of its own. The jobs keep coder's socket for the test services
  they start themselves. A local executor must be on the host before this
  role runs; a remote one is named in `executor_remote_instances`, and the
  slot's env file then carries its URL and certificate hash in place of
  the mounted socket. The rebase runner is no
  caller.
- **The preview's procedure is a skill, its values are the executor's.**
  `files/skills/preview/` is how a preview is brought up, loaded by whoever
  starts one; `CLAUDE.md.j2` keeps only "Your stack", whether the
  repository has one and what holds every turn. The workbench has a
  `preview` skill of the same name, so a role told to use "the `preview`
  skill" does the right thing in both. `propose-issue` beside it is the
  runner's other skill; the shared ones are `claude_team`'s.
- **Each slot mounts a skills directory of its own, so a repository can
  opt into a skill.** `files/repo_skills/<name>/` holds the skills a
  repository takes with `skills:` on its entry; `files/skills/` the ones
  every job has. The play fills `~coder/claude/skills/` through
  `claude_team`, copies it into every slot's `~coder/claude/<id>/skills/`,
  adds the opted-in ones, and the unit mounts the slot's. Only the opt-in
  skills are ever removed from a slot, when its repository drops one; the
  copy of the rest does not delete, as the team's notes say. `gpu-models`
  is the first, and the only one that brings more than a file: the GPU
  model server's address and key, `github_runner_gpu_models_*`, written as
  `gpu-models.env` beside the slot's rules, root's and `coder`'s to read,
  and handed to the runner through the unit's `EnvironmentFile=`, which the
  job's steps inherit as they do `DOCKER_HOST`. The file is among those
  `refresh-runners` compares, so a rotated key reaches idle runners. The
  host's sandbox has to let `coder` reach that one server and nothing else
  off the host: every job can reach it, only an opted-in repository's jobs
  are handed the key, though any job could read the file through the
  socket, as `coder` can. The play refuses to write an empty key or URL
  where a repository opted in, unless `github_runner_start` is false.
- **A repository's `instructions:` end its jobs' CLAUDE.md.** One string of
  Markdown from the inventory, carried on each slot and rendered under
  `## <repo> on this box`, so the lead and every role read it with the
  rest. It is for how this host treats the repository; what the repository
  is like belongs in its own CLAUDE.md, or the two drift apart. A change
  to it reaches idle runners through `refresh-runners`, since CLAUDE.md is
  among the files it compares. The workbench has the same key on
  `workbench_repositories`.
- **The rules name the host only through variables.**
  `github_runner_host_name` and `github_runner_host_description` are how
  the jobs' rules say where they run. Nothing in the templates names a
  particular host, and nothing a job writes on GitHub names the preview's
  address: the preview is the roles', and a person tries a pull request
  from the workbench's `try` skill.
- **Slots, not repositories, are the unit.** Every repository has
  `runners` slots, and everything per job is per slot: the unit, the
  registration's name, the rules with the slot's ports, and the
  executor's caller. The play lays them out once, in
  `github_runner_slots`, and every later task loops over that. The
  registrar takes the slot as a second argument and refuses one past the
  repository's count, so an escape to `coder` still gets no more runners
  than the play set up.
- **The shared workflow's repository names nothing of any host.** It says
  neither a host's name nor the name of the repository that configures
  one, in its files, its commit messages or its history, which is why its
  README points nowhere. What the steps are and how to run them stays
  documented here.
- **The bot's name is the consumer's, and this collection names none.**
  `github_runner_bot` is what a comment mentions to start a job and what
  the labels and the workflow's own marks begin with, written `<bot>`
  below. The runner's unit hands it to the job as `CLAUDE_BOT`, each
  `claude-job-*` script reads it for itself and refuses one that is not
  letters, digits and dashes, and the lead's rules are rendered with it.
  The runners' labels default to it. It has to be the name the shared
  workflow triggers on. The team's files are static and so never say it:
  they speak of levels, and `propose-issue` of `$CLAUDE_BOT`.
- **A job proposes a workflow change, and the user pushes it.** The Claude
  App has no Workflows permission, and that is the line that keeps the
  repositories' secrets from a job. A Makefile or a test can change what CI
  runs, but only a workflow file can read `secrets.*`, and a workflow pushed
  to a branch runs with `on: push` before anyone reviews it, on a
  self-hosted runner too if it names one, past the sender and vouch checks
  in the shared workflow. Those secrets include the App's key, which mints
  a token for every repository the App is on, and the Claude token. GitHub
  grants Workflows per App rather than per file, so forbidding a few files
  would forbid nothing. Hence the diff in the PR, inside a command the user
  pastes, in `lead.md.j2`.
- **The workflow is shared, and its steps are scripts in the image.** The
  workflow is a reusable one in the shared workflow's repository, and each
  served repository carries only a small caller, which passes its events
  and secrets on. What the steps do is in the image, in `files/image/`,
  under pytest, and changes when the play runs. A workflow change that
  calls a new command or flag goes out after the image does. Each script:
  - **`claude-job-level`** (`job_level.py`) reads the level from the
    labels or the comment, and the first part's time.
  - **`claude-job-part`** (`job_part.py`) runs a long job in parts, each
    with a fresh App token, which lasts an hour. The lead asks for the
    next with `continue`, and `next` decides, between parts, whether there
    is one and its time, after which the workflow mints a token and
    resumes the session in the action's agent mode. A part cut off at its
    limit gets the next unasked: the lead sees the clock only between its
    calls, and a subagent's turn can overrun a stop time by minutes. The
    action sets no `session_id` for a step it cut off, so `next` names the
    session, the newest transcript in `~/.claude/projects`.
  - **`claude-job-finish`** (`job_finish.py`) ends every job, in this
    order:
    - It commits, pushes and opens a draft for whatever Claude left, when
      it was cut off: past the repository's hooks, since a commit message
      rule must not lose work, and on a PR without the settings the action
      restored from the default branch, which would undo the PR's own
      changes to them.
    - It pushes to `<branch>-job-<run>` when the branch moved on GitHub
      meanwhile, but over it with a lease when it is still what the job's
      own last push left there, which the remote-tracking ref's reflog
      says, and never over it midway through a rebase, which leaves HEAD
      detached on part of the branch and could push a branch named `HEAD`
      and open a second PR.
    - For a Progress left at `working` it comments a marked
      `@<bot> continue` as the App's bot, which the workflow takes in
      place of a person's, when the lead ended well, a failed job used
      half its time, or GitHub lost the runner, which its annotation on
      the job says, and fewer than four did so since someone on the
      allow-list last commented. Otherwise it turns it into `stopped`,
      after the four with a note of what the user can do with work that
      large.
    - It hides as outdated, never deletes, what the bot left on the thread
      and the PR before this job that says nothing now: stopped notes,
      marked `@<bot>` comments and the action's tracking comments
      holding only their placeholder, keeping everything a lead wrote.
    - It leaves the slot's stack alone: the executor stops it when it has
      been idle too long.
  - **`claude-job-rebase`** (`job_rebase.py`) is the workflow's only step
    on a push to the default branch:
    - It comments a marked `@<bot> quick: rebase` as the App's bot on
      each of the bot's ready PRs that now conflicts with it. It leaves
      out one with a job of the workflow running or waiting on it, found
      by the run's title, the PR's or its issue's, since a second job
      waiting in a concurrency group replaces the first, and one asked
      three times since someone on the allow-list last commented. A PR
      only behind merges as it is and is left alone.
    - When the push merged a part of an issue done in parts, the bot's PR
      starting `Part <k>/<n> of #<issue>` with k below n, it comments a
      marked `@<bot> continue` on the issue once per merged part, if
      the issue is open, still carries a working label, and has no job and
      no open PR of the bot's on it, so the next part starts at the
      issue's level with the user's merge as its yes.
  - **The part line is parsed in four places**, which the scripts and the
    workflow each do for themselves: the workflow's concurrency key,
    `claude-job-rebase`, `claude-job-vouch`, which checks a part with its
    issue, and `claude-job-finish`, whose draft for an issue done in parts
    says `Part of #<issue>` and so closes nothing.
  - **`claude-job-retry`** (`job_retry.py`) starts again what only needs
    time. `limit` reads the last part's execution file for the usage limit
    and its reset time, which `claude-job-finish`'s `WAITING` then leaves
    at `working`. `wait` says on the thread when the job starts again and
    labels it `<bot>:waiting`, also for a job that failed before
    Claude. The scheduled `sweep` comments a marked `@<bot> continue`
    on each whose time has come, at the level `claude-job-level`'s `asked`
    names, three in a row at most.
  - **The workflow itself** also relays a review requesting changes
    without a mention as a marked `@<bot>`.
  - **`claude-job-vouch --hook`** blocks any command of Claude's carrying
    any `<bot>:auto-` mark, so no lead starts a job of its own by
    accident. It reads the command's text, so a mark written to a file and
    posted with `--body-file` passes it; what holds is the step's cap,
    `MAX_UNASKED`, 12, on the bot's marked comments since someone on the
    allow-list last commented. The rest of what it does is the next note.
  - **`claude-job-checks`** (`job_checks.py`), which the lead runs after
    its last push, waits for the PR's checks and prints what failed, and
    as a hook refuses `gh pr ready` until they are green and the branch
    has every commit of its base, and while someone on the allow-list has
    asked on the PR, a `@<bot>` comment, review or review comment,
    since the job's run was created: that comment's job waits behind this
    one, and a rebase job that started late must not mark ready a PR the
    user has since asked more of. `--waiting <n>` asks the same as a step,
    and the workflow skips a rebase the bot asked for, the `quickrebase`
    job's and an `auto-escalate` one's, when the answer is yes; when it
    cannot read, the rebase runs.
  - **`claude-job-report --post`** (`job_report.py`) rebuilds the PR's
    cost comment from the action's execution file, keeping earlier jobs'
    rows as JSON in an HTML comment, one row for all of a job's parts. A
    step killed at its limit writes no execution file, so its row says
    "unrecorded". The per-model figures are what Claude Code reports; the
    per-role ones are only call counts, since the messages carry no usage
    per subagent.
- **`claude-job-vouch` runs three times a job, and fails closed.**
  (`files/image/job_vouch.py`.) It refuses an issue or PR that someone
  off `CLAUDE_ALLOWED_IDS` wrote or retitled or edited since the latest
  `<bot>` or `<bot>:*` label someone on it added, and any PR opened by someone off
  it; a PR is checked with the issues it closes. It runs as the step
  before Claude, which comments and fails; as a `PreToolUse` hook in
  `settings.json`, which blocks a push, `gh pr`, `gh issue` or `gh api`
  with exit 2 and lets every other command through without a query; and
  before the push of what Claude left. The hook matches the command's
  text, so it stops a lead that forgot, not one set on getting past it:
  `git -c alias.p=push p` is not a push to it. The step before the push
  of what Claude left is the check that holds. An API error or a missing
  variable is a refusal, so a workflow that stops passing
  `CLAUDE_ALLOWED_IDS` to the Claude step leaves every job unable to push.
  The bot's id is passed too, since it edits its own PRs. Claude Code
  snapshots hooks at start, as its hooks documentation says, so Claude
  rewriting `settings.json` mid-job, which it can, as `claude-code-action`
  merges into the same file, changes nothing.
- **`<bot>:ui` is a modifier `claude-job-level` reads with the level.** It
  is not in the workflow's trigger list, so adding it starts nothing; with
  `<bot>:review` or `<bot>:full` the script adds twenty minutes and
  a phrase to the lead's prompt, through the same `note` output, so the
  workflow needed no new output. The sum has to stay under the shared
  workflow's `timeout-minutes`, or the finishing steps never run.
  `ui-tester` drives the preview with Playwright for Python through `uv`
  and the image's Chromium, so the image needed nothing new; the first run
  downloads the wheel into the repository's uv cache.
- **The cleaner keeps the layers builds use, and the caches jobs use.**
  `clean-runners` (`files/clean/`), on coder's daily timer, removes the
  strays first: a container in no compose project, no systemd unit's and
  not named `github-runner-*`, which only a job's own `docker run` leaves,
  made longer ago than `github_runner_strays_max_age_hours`, which no job
  lasts. Then only dangling images: untagged and with no child. The intermediate layers of
  the image still tagged have one, so the next build stays cached. It
  empties a repository's cache past `github_runner_cache_max_gb` in place,
  never removing the directory, which the idle runners have mounted, and
  only while none of that repository's runners has a `Runner.Worker`. The
  cache is mounted at `/cache`, not `~/.cache`, which holds Claude Code's
  own files in the image.
- **A repository's rebase runner is one more slot, for rebases alone.**
  `rebase_runner: true` on an entry adds slot `runners + 1`, with ports
  like any other, registered with `github_runner_rebase_labels` in place
  of the work runners' (the registrar config's `rebase_slot`), its unit
  setting `CLAUDE_RUNNER_KIND=rebase`, and `rebase.md.j2` as its lead's
  rules. When the repository's `CLAUDE_REBASE_RUNNER` variable is `true`,
  the workflow runs its `rebase` job there, which asks for the rebases
  after a merge and needs only `gh`, and its `quickrebase` job, which
  takes the bot's `@<bot> quick: rebase`; set it only once the slot is
  online, or the requests wait for a runner that never comes.
  `claude-job-rebase-runner` (`job_rebase_runner.py`) refuses to run on
  any other kind:
  - `try` rebases with git, and a clean rebase is pushed at once with a
    lease on the head it started from, Claude never starting. A conflict
    is left midway, for Claude, with `git merge-tree`'s list of the files
    that conflict in the step's `state` output: Claude can write every
    file of the job's, `$RUNNER_TEMP` included, and a state file there
    could have named `main` as the branch to push.
  - Claude's step gets a read-only token and a checkout without
    credentials, so only `settle` pushes, and it trusts nothing of the
    checkout. It kills every process of the account off its own line of
    ancestors and turns itself undumpable before the token is in use,
    checks the PR is still what `try` saw, and fetches Claude's HEAD into
    a bare repository of its own. The checks and the push run there, with
    no global, system or repository config, hooks or attributes: `* -diff`
    in `.git/info/attributes` or `diff.external` would empty the diff the
    count reads, and a `pre-push` hook would run with the token. It pushes
    a rebase containing the base, with no conflict marker, whose PR change
    moved by at most `MAX_MOVED` lines, all in the files that conflicted,
    as the lines the PR adds and removes per file before and after, a
    binary file as its blob. What moves by itself is left out of the
    count: a `.po` catalogue's `#:` references, lock files, and what the
    base's `.gitattributes` marks `linguist-generated`, which must still be
    among the files that conflicted.
  - Anything else is handed on: undone, the PR back to a draft, which
    `claude-job-rebase` leaves alone, and a marked
    `@<bot> review: rebase` (`auto-escalate`), which runs on the work
    runners at `<bot>:review`, `claude-job-level`'s `review:`, where
    the lead's rules have the engineer rebase and the reviewer compare
    the change by `git range-diff`.
- **Podman 5.4 speaks Docker API 1.41, and compose gates features on it.**
  docker-compose reads the version from the server, so no client setting
  gets past it: a compose file with a health check's `start_interval`
  (1.44, Docker 25) fails to start at all. Podman 6 reports 1.44, but
  Debian trixie has 5.4. The executor resets the key in a stack by itself.
  Every compose run of a job's own, an e2e script's among them, adds a
  reset file from `$RUNNER_TEMP` with `-f`, as the job rules say: a script
  names its own files, so an override beside them is not read, and one in
  the checkout would be committed by `claude-job-finish`.
