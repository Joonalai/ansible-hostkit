# hostkit

`joonalai.hostkit`, an Ansible collection of roles for self-hosted Debian
servers. Each role is configured entirely from the consumer's inventory.

| Role                                           | What it does                                                                | What it requires                                                                                  |
| ---------------------------------------------- | --------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| [`rootless_account`](docs/rootless_account.md) | An account that runs rootless podman containers from its systemd manager    | An account name and the start of its subordinate ID range                                         |
| [`ci_runner`](docs/ci_runner.md)               | One-job GitHub Actions runners for private repositories, in such an account | The host's own sandbox unit for the account, a subordinate ID range, repositories, a GitHub token |

All roles need Debian with systemd and rootless podman, and Ansible 2.18 or
newer on the controller.

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

MIT, see [LICENSE](LICENSE).
