# Changelog

Releases are tags, `v<version>`, made by the release workflow from the
commits' Conventional Commits titles. A breaking change to a role's
variables is a new major version, with the migration in its commit's
`BREAKING CHANGE:` footer, which this changelog and the release's notes
carry.

<!-- version list -->

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
