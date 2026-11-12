# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

`hostkit`, an Ansible collection of roles for self-hosted Debian
servers: rootless podman accounts, ephemeral CI runners, and the runners
and workbench that Claude Code works in. Each role is meant to be reused by
any host, configured entirely from the consumer's inventory.

Consumers pin a tag or a commit in their `requirements.yml`. A local
checkout may stand in for the pin while a change is tried, on a test host
or a real one.

## Security comes first

These roles run as root, and several of them exist to decide what untrusted
code can reach. A mistake here is not a bug in one service: it opens every
host that moves its pin. Weigh every change by what it lets through.

- **Hardening is the consumer's.** No role here changes the firewall,
  sshd, the kernel's network settings or anything else that hardens the
  host. The consumer owns all of it, sandboxing the accounts these roles
  create included. A role never calls `ufw`, `nft`, `iptables` or
  `firewalld`, and never writes their configuration.
- **Say what the host must provide, and fail closed without it.** A role
  whose safety rests on the consumer's hardening documents exactly what it
  needs: which account or ID range to hold, what it must reach and what it
  must not. It takes the consumer's unit as a variable, ties the account's
  systemd manager to it (`Requires=`) so the account does not run without
  it, and `assert`s it is there rather than assuming it.
- **Untrusted code gets no credentials.** Tokens that register runners, push
  or deploy stay with root, in files only root reads. A job, a container or
  a sandboxed account never sees them, not even briefly through an
  environment variable or a mounted directory.
- **Least privilege by default.** Rootless containers, `ReadOnly=true`,
  `DropCapability=all` and `NoNewPrivileges=true` unless a role can say why
  not. No `--privileged`, no host devices, no host network for untrusted
  work.
- **Pin the supply chain.** Container images by digest, GitHub Actions by
  commit SHA, hooks by tag or SHA, Python through `uv.lock`. Never
  `curl | sh`, never an unpinned `latest` for anything that runs as root or
  runs untrusted code.
- **Secrets never enter the repository**, not even an example value that
  works. A role takes a secret as a variable that defaults to empty and
  fails clearly when it is needed and missing. Where the value comes from
  (Ansible Vault, SOPS, anything else) is the consumer's business.
- **Defaults are safe on their own.** A role applied with only its required
  variables set must leave the host no more open than before. Anything that
  widens access is opt-in and named for what it opens.

A change that touches what a role asks of the host's hardening, an
account's privileges,
a token's reach, a runner's isolation or the supply chain gets its own
"Security-sensitive changes" section in the PR, one line per file saying
what it loosens or tightens, so the reviewer can check that list against
the diff.

## Public from the first commit

The repository is meant to be made public, and doing so publishes its
whole history. Everything committed must be fit for that already:

- **Never name a host, a user's network or a consumer repository**, in
  files or in commit messages: no hostnames, tailnet names, IP addresses,
  account names chosen by a consumer, or memory sizes of a particular
  machine. Defaults and examples use `example.ts.net`, `192.0.2.0/24` and
  the like.
- **No personal details**: no email addresses, no employer, no names beyond
  the GitHub account that owns the repository.
- **No copied history.** A role comes in as its current state, never with
  its history from another repository.

## Commands

```sh
uv sync --locked                    # the venv: ansible, ansible-lint, prek, pytest, ruff, ty
.venv/bin/prek install              # the pre-commit and commit-msg hooks
.venv/bin/prek run --all-files      # every hook, which must pass before a commit
.venv/bin/pytest                    # the roles' Python tests alone
.venv/bin/ruff check                # every ruff rule, as pyproject.toml sets them
.venv/bin/ty check                  # every ty rule, as errors
```

The hooks include gitleaks, ansible-lint's `production` profile, ruff, ty
and the tests. Run them before every commit, and fix what they report rather than
skipping a hook.

## Layout and conventions

- `galaxy.yml` at the root, roles under `roles/<name>/`. Each role with
  traps of its own keeps them in a `CLAUDE.md` beside its tasks.
- Every Python file a role carries under `files/` has pytest tests beside
  it in `tests/`, and passes ruff and ty with every rule on. What is
  ignored for all of them is in `pyproject.toml` with its reason; a
  `# noqa` or `# ty: ignore` in a file names its rule and says why. The
  scripts run on Debian's python3, so both tools target that version, not
  the venv's.
- FQCN everywhere. `become: true` per task, not per play.
- Role variables and `register:` names carry the role's name as a prefix.
- Prefer a module over `command`. Where `command` is unavoidable it needs
  `changed_when`.
- A task that reads back what an earlier task did fails under `--check`;
  gate it with `not ansible_check_mode`.
- Two flags gate what a test container cannot do, each with a default in
  every role that reads it: `is_container_environment` for anything that
  needs real hardware or a real kernel, and `install_dependencies` for
  `apt` tasks.
- A breaking change to a role's variables is a new major version, with the
  migration in the tag's notes.

## Commits

Conventional Commits titles in lower case after the type, at most 72
characters (`.gitlint`, checked by the commit-msg hook): `feat: add the ci runner role`.

## Skills

The skills in `.agents/skills/` are this repository's procedures. Claude
Code loads them through `.claude/skills`, a symlink to that directory. Use
the one whose task matches:

- `nls-ansible`: Ansible conventions for roles and playbooks. Read it before
  writing or changing a role.
- `nls-python`: Python conventions for the scripts the roles carry: type
  hints, docstrings, naming and pytest. Read it before writing or changing
  one.
- `ansible-new-role`: scaffolding a new role.
- `ansible-good-practices`: reviewing roles, playbooks or the collection
  against the Red Hat CoP good practices.
- `ansible-new-molecule`: adding molecule tests to a role or the collection.
- `nls-security-review`: an in-depth security review, only when one is
  asked for. It is written with Python in mind; the Ansible side is this
  file's "Security comes first".
- `nls-ai-housekeeping`: auditing and refreshing these instructions and the
  documentation.

The skills are vendored by copier from a template. Never edit them by hand:
`copier update --trust --answers-file .copier-answers.agent-instructions.yml`
overwrites them. `.agents/skills/EXTERNAL_SKILLS.md` lists their sources
and pinned versions. The hooks skip `.agents/` and the copier answers file,
which stay as the template writes them.
