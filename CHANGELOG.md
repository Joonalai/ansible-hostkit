# Changelog

Releases are tags, `v<version>`, made by the release workflow from the
commits' Conventional Commits titles. A breaking change to a role's
variables is a new major version, with the migration in its commit's
`BREAKING CHANGE:` footer, which this changelog and the release's notes
carry.

<!-- version list -->

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
