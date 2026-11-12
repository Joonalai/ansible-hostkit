# hostkit

`joonalai.hostkit`, an Ansible collection of roles for self-hosted Debian
servers. Each role is configured entirely from the consumer's inventory.

| Role                                           | What it does                                                                                       | What it requires                                                                                                            |
| ---------------------------------------------- | -------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| [`rootless_account`](docs/rootless_account.md) | An account that runs rootless podman containers from its systemd manager                           | An account name and the start of its subordinate ID range                                                                   |
| [`ci_runner`](docs/ci_runner.md)               | One-job GitHub Actions runners for private repositories, in such an account                        | The host's own sandbox unit for the account, a subordinate ID range, repositories, a GitHub token                           |
| [`github_runner`](docs/github_runner.md)       | One-job GitHub Actions runners for Claude Code to work issues on, with previews                    | The host's own sandbox units, a pinned uid and subordinate ID range, a GitHub App that registers runners, repositories      |
| [`executor`](docs/executor.md)                 | Compose stacks for the runner's and the workbench's previews, from git bundles, with no credential | The host's own sandbox units, a pinned uid and subordinate ID range, a port range                                           |
| [`workbench`](docs/workbench.md)               | A container per repository with a Remote Control session, writing to GitHub only through a gate    | The host's own sandbox units, a pinned uid and subordinate ID range, a Claude login, a read and a write token, repositories |
| [`claude_code`](docs/claude_code.md)           | Claude Code as a Remote Control server, driven from the Claude app                                 | A Claude subscription login, done once by a person                                                                          |
| [`claude_team`](docs/claude_team.md)           | The subagent roles and skills the runner's jobs and the workbench's sessions share                 | A directory to install into                                                                                                 |

All roles need Debian with systemd, and Ansible 2.18 or newer with
`community.general` on the controller.

**Hardening is the host's.** No role here changes the firewall, sshd or
anything else that hardens the host. A role that runs untrusted code says
what the host must hold off, takes the host's own unit for it as a variable,
and refuses to run without it.

## Installing

In the consumer's `requirements.yml`, pinned to a tag or a commit:

```yaml
collections:
  - name: https://github.com/Joonalai/ansible-hostkit.git
    type: git
    version: <tag or commit>
```

```sh
ansible-galaxy collection install -r requirements.yml
```

## Development

```sh
uv sync --locked
.venv/bin/prek install
.venv/bin/prek run --all-files
```

## Licence

MIT, see [LICENSE](LICENSE). The agent skills vendored under
`.agents/skills/`, which the collection's package leaves out, keep their
own: the `nls-*` skills are MIT, copyright 2026 National Land Survey of
Finland (Maanmittauslaitos), and the `leogallego-*` skills GPLv3, each with
its `THIRD_PARTY_LICENSE`; `.agents/skills/EXTERNAL_SKILLS.md` names their
sources.
