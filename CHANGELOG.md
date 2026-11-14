# Changelog

Releases are tags, `v<version>`, made by the release workflow from the
commits' Conventional Commits titles. A breaking change to a role's
variables is a new major version, with the migration in its commit's
`BREAKING CHANGE:` footer, which this changelog and the release's notes
carry.

<!-- version list -->

## v0.4.0 (2026-11-14)

### Bug Fixes

- Carry a changed memory cap to an account's live cgroup
  ([`42903d7`](https://github.com/Joonalai/ansible-hostkit/commit/42903d78e06772c93e14e17e34bd972dc4c8b399))

- Finish an up only once its page answers
  ([`d916857`](https://github.com/Joonalai/ansible-hostkit/commit/d916857b7044d1f30dd9ec58dd1d019c58962acb))

- Take down a removed caller's stack when the play removes it
  ([`c30bfd5`](https://github.com/Joonalai/ansible-hostkit/commit/c30bfd5b70f98f10caeddac8d3363f9f630379ae))

### Chores

- Pipeline the dev container's runs and scope idempotence to a play
  ([`1143740`](https://github.com/Joonalai/ansible-hostkit/commit/1143740b73a2214cd2b2710ee4b271e5aa26ff14))

### Documentation

- Keep cheap lines at the end of the runner's containerfile
  ([`8a1c625`](https://github.com/Joonalai/ansible-hostkit/commit/8a1c6258712ef9a3e31c3ecc36160b776a0012b5))

### Features

- Give an instance's secrets to the stacks of chosen repositories
  ([`b278355`](https://github.com/Joonalai/ansible-hostkit/commit/b2783558f01ed284c00a009046bff1949582f277))

- Leave the preview's address out of a runner's pull requests
  ([`e12be85`](https://github.com/Joonalai/ansible-hostkit/commit/e12be85bf6c6acb8cdb34d41b8b4d4a335f25479))

- Let a caller stop and start one service of its stack
  ([`9ba2d58`](https://github.com/Joonalai/ansible-hostkit/commit/9ba2d582061bc6a5846809f0135b68905e927c69))

- Let a person try a pull request from any workbench session
  ([`3637b7b`](https://github.com/Joonalai/ansible-hostkit/commit/3637b7b24af59dda559b38c6a7a4bf043d61d4da))

- Let a try follow the person's instructions for a repository
  ([`9ed75f3`](https://github.com/Joonalai/ansible-hostkit/commit/9ed75f358fdeb3300d362d5c483a32aa53e2fe86))

- Let an account yield the cpu to the host's other accounts
  ([`7afa880`](https://github.com/Joonalai/ansible-hostkit/commit/7afa88086c807961f0b03523da8ddcc921fe4044))

- Let an instance set how long its tls port waits for a caller
  ([`7d7b838`](https://github.com/Joonalai/ansible-hostkit/commit/7d7b83845d204a8356b70d3ff7d9ff4b0fd51789))

- Let callers on another host use an executor instance
  ([`3dd1866`](https://github.com/Joonalai/ansible-hostkit/commit/3dd18663d18d5062c35a9088fb3b81cf25cad22a))

- Never stop a person's stack to make room for another
  ([`49cc199`](https://github.com/Joonalai/ansible-hostkit/commit/49cc1990487ebcae7ffacd43b98bb3947d311083))

- Tell a stack its page's https origin where the host serves one
  ([`ad99f93`](https://github.com/Joonalai/ansible-hostkit/commit/ad99f93ebb5b8388fe08424a1c557192506205d8))

- Tie a request's answer to its journal lines by an id
  ([`205b4ec`](https://github.com/Joonalai/ansible-hostkit/commit/205b4ec0a4cd3e3b8207bd35292469b033b96baf))


## v0.3.1 (2026-11-13)

### Bug Fixes

- Keep a repository's bun packages in its runner cache
  ([`1e821f3`](https://github.com/Joonalai/ansible-hostkit/commit/1e821f3c9d3eed1cb7484302ca984fc85c2302d0))

- Let a runner job's commands run up to thirty minutes
  ([`e17d120`](https://github.com/Joonalai/ansible-hostkit/commit/e17d1201cece51a98e20b5949a8d1a402a4fa703))

- Start a stack's volumes empty when its branch changes
  ([`6b50a6b`](https://github.com/Joonalai/ansible-hostkit/commit/6b50a6be5fa50963e0e0775c065407faa3895f5e))


## v0.3.0 (2026-11-13)

### Features

- Let a group of the host's read the ci timing report
  ([`6dc8c42`](https://github.com/Joonalai/ansible-hostkit/commit/6dc8c420b3e2590d8c0d32daac0105df599bed69))

- Record when each ci runner's container starts, ends and goes
  ([`988e0e0`](https://github.com/Joonalai/ansible-hostkit/commit/988e0e0ccf87dc01ffa69921417f945a8d8b10e9))

- Record where each ci job's time goes
  ([`224cff6`](https://github.com/Joonalai/ansible-hostkit/commit/224cff615fd0c116ebbf721ba88e2622c517863e))

- Report where the ci jobs' time went
  ([`88220e6`](https://github.com/Joonalai/ansible-hostkit/commit/88220e63bb234b1be37dae0a2e09ddaae809f254))


## v0.2.0 (2026-11-13)

### Bug Fixes

- Build a stack's images one at a time
  ([`00a42d5`](https://github.com/Joonalai/ansible-hostkit/commit/00a42d5b63b1ed6654abf63cc65bd70a4bbbc26d))

- Check a stack's tree out with git's usual file modes
  ([`cbadf06`](https://github.com/Joonalai/ansible-hostkit/commit/cbadf0610874a6df291a659b581a83ee5d3e9415))

- Log every refused or failed executor request to the journal
  ([`aed17c9`](https://github.com/Joonalai/ansible-hostkit/commit/aed17c94950503a95aa7bb6c9ff7d43f981d71d8))

- Name every image a stack builds after the stack
  ([`1f4d211`](https://github.com/Joonalai/ansible-hostkit/commit/1f4d211086ce50ceaa0f90031a76a0ffe5001a0a))

- Watch a stack's services after an up and fail one that does not run
  ([`5e4aeb4`](https://github.com/Joonalai/ansible-hostkit/commit/5e4aeb41bb67f4340beedee472bc01669541b03e))

### Features

- Tell repositories how to keep a stack's build cheap
  ([`73fe4ac`](https://github.com/Joonalai/ansible-hostkit/commit/73fe4ac68f4c5a913cbe50f0789571ad5f9a6184))

### Testing

- Keep the tests' git off the repository a hook runs them in
  ([`5e9220e`](https://github.com/Joonalai/ansible-hostkit/commit/5e9220e09b7e5b9997046705a300d2ce2437a3df))


## v0.1.0 (2026-11-12)

- Initial Release
