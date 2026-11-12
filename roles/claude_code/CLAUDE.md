# The `claude_code` role

Notes for working on this role. The collection's conventions are in the
root [CLAUDE.md](../../CLAUDE.md), which applies here too.

Claude Code on the host as a Remote Control server: an account, the
native binary, and a `claude-remote-control@<dir>` user unit template
that runs a session in `~/projects/<dir>`, which the phone app's Code tab
or claude.ai/code opens. This role owns the account, the binary and the
template. What runs in a session, which checkout and which permissions, is
a consumer role's: the workbench, or a role of the host's own, each a
`claude-remote-control@<dir>` instance and not a second copy of any of it.

- **It dials out, which is why it needs no port.** Remote Control registers
  with Anthropic over outbound HTTPS and polls; nothing listens, so it
  works behind CGNAT and needs no firewall entry and no VPN. Claude Code on
  the web is the opposite shape: a sandbox in Anthropic's cloud that cannot
  reach the host's private network.
- **A subscription login, not an API key, and a human does it once.**
  `sudo -iu claude claude auth login` prints a URL and takes back a code.
  The role checks for `~/.claude/.credentials.json` and refuses to run a
  session without it, since the unit would only loop on *"You must be
  logged in to use Remote Control"*. A test container never logs in:
  `claude_code_login_required: false` there installs and enables the
  session but leaves it stopped, so a green test run says nothing about
  the session itself. The login also expires on its own, and nothing
  re-runs the play when it does; the unit then loops on that message until
  someone logs in again. A host that depends on a session should watch for
  running `claude remote-control` processes and alert when there are none.
- **The version is the play's decision, every run.** A native install
  reports its own auto-updater off, so `claude_code_version` names a channel
  or a version; a channel is resolved through the release service the
  installer uses, and the binary is told to install that release when it is
  not already it. Running the role is therefore also how it is upgraded,
  and `claude-update.timer` does the same every night, so a play nobody runs
  does not leave it on an old release. Both restart the sessions onto the
  new binary, since a running one keeps the binary it started with. The
  workbench's play sets `claude_code_update_restarts` empty, so the timer
  never ends a session working in a container, and each takes the new
  binary at its next start.
- **Every restart is a new session on the phone.** `Restart=always` brings
  the process back after a crash, an upgrade or a long outage, and Remote
  Control cannot resume the old session from a unit. A project whose
  conversation matters keeps it itself, outside Claude Code.
- **Two first-run questions have nobody to answer them.** Claude Code asks
  once per directory whether to trust its files, and `claude remote-control`
  asks once per account *"Enable Remote Control? (y/n)"* and waits; a unit
  with no stdin sits on that for good, with a running process, nothing in
  the journal and nothing on the phone. Both answers live in
  `~/.claude.json`, which Claude Code rewrites whenever it likes, so
  `tasks/state.yml` merges just those keys into it, `remoteDialogSeen` and
  `hasCompletedOnboarding` for the account and `hasTrustDialogAccepted` for
  each checkout, and a merge restarts every session through the
  `Restart Claude Code sessions` handler topic, since an answer given after
  the process started is one it never saw. A consumer role includes
  `tasks_from: state` for its own checkout.
- **No auto mode and no bypass, for every session on the account.**
  `claude_code_settings` is the account's `~/.claude/settings.json`. The
  unit starts a session in the mode a consumer names, but that is only the
  starting mode: auto mode hands each call to a classifier and drops the
  broad allow rules, so a consumer's allow list stops being the boundary
  the moment the mode changes. Deny rules are what bind in every mode, and
  a consumer carries those in its checkout's `.claude/settings.local.json`.
  A Python interpreter, or any shell, is the whole account to a session,
  its Anthropic credentials included: deny them unless the session needs
  them.
- **The phone lists the machine, not the session name.** What `--name`
  gives is what claude.ai/code shows for the pre-created session; the app's
  Code tab shows the host and offers a new session on it. The name uses the
  machine's own hostname rather than `inventory_hostname`, which an
  inventory may set to anything.
- **The account can be sandboxed like the others.** The unit is a user
  unit, so it runs under the account's `user@<uid>.service`. The workbench
  includes `rootless_account` for its account, which ties that manager to
  the host's sandbox units; a session then stops with the sandbox too.
