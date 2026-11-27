# Changelog

Releases are tags, `v<version>`, made by the release workflow from the
commits' Conventional Commits titles. A breaking change to a role's
variables is a new major version, with the migration in its commit's
`BREAKING CHANGE:` footer, which this changelog and the release's notes
carry.

<!-- version list -->

## v0.9.0 (2026-11-27)

### Bug Fixes

- Keep the dispatcher's private tmp out of the runners' podman
  ([`67fa131`](https://github.com/Joonalai/ansible-hostkit/commit/67fa13171f5b6fca94f327aad4b2a27c8d1834ed))

### Features

- Give every github runner job an environment from the inventory
  ([`a91542f`](https://github.com/Joonalai/ansible-hostkit/commit/a91542ff0286f45356bb60619ff7751cfbac80ad))


## v0.8.1 (2026-11-18)

### Bug Fixes

- Open no draft for a branch whose pr was merged while the job ran
  ([`f194129`](https://github.com/Joonalai/ansible-hostkit/commit/f1941296f1ecaffb63bef2f34779e88ebae2065f))

- Push a rebase over the branch another job pushed when it is unmoved
  ([`e85922d`](https://github.com/Joonalai/ansible-hostkit/commit/e85922dde76011c276c55aa1cc7b5aa1a34ec565))

- Settle a rebase whose diff holds bytes that are not utf-8
  ([`ec1a6a3`](https://github.com/Joonalai/ansible-hostkit/commit/ec1a6a38522632687ed8c4bd090c93e2b3e4ea67))

- Tell the team where a stack's model comes from and how to check it
  ([`6aa44fd`](https://github.com/Joonalai/ansible-hostkit/commit/6aa44fde8d7e1ad8fa05b9d0029029910762a8fd))

### Testing

- Give a remote waiting caller's up its repository's secrets
  ([`986aa04`](https://github.com/Joonalai/ansible-hostkit/commit/986aa0400b6d6c5e91b85dd61beff1b109da03b0))


## v0.8.0 (2026-11-17)

### Bug Fixes

- Mark a draft left at done ready through the gate, or ask why not
  ([`37609f1`](https://github.com/Joonalai/ansible-hostkit/commit/37609f1bfc49b53dd186845921ee30da2650c0c9))

- Retry the rebase runner's hand-on and leave it asked when it fails
  ([`fc64c8c`](https://github.com/Joonalai/ansible-hostkit/commit/fc64c8cc9bcd9173cce34561d5bfebb14414112c))

### Features

- Rerun a failed check once per head before it waits for the owner
  ([`26466c1`](https://github.com/Joonalai/ansible-hostkit/commit/26466c1aad4c9af683fdc9f47c6bc56871608da6))


## v0.7.1 (2026-11-16)

### Bug Fixes

- Say asked for a draft past its rounds and done for a ready pr
  ([`533a6d5`](https://github.com/Joonalai/ansible-hostkit/commit/533a6d552a003060f574c718a6e9ddf63c0b05e3))


## v0.7.0 (2026-11-16)

### Bug Fixes

- Give a caller a new port block when the instance's size changes
  ([`e1bd30e`](https://github.com/Joonalai/ansible-hostkit/commit/e1bd30e1b39ba4430731b9f93c5ddc91c2dc2edd))

- Run the gpu-models skill's scripts from a file, not a heredoc
  ([`b279322`](https://github.com/Joonalai/ansible-hostkit/commit/b2793221a9d67b3788f6f98ef7d10a4ff95db92f))

### Features

- Cap the bot's drafts in work at the repository's runners
  ([`1444f5b`](https://github.com/Joonalai/ansible-hostkit/commit/1444f5bf5fae6fa0b3aa9e7444fde835627d4b76))

- Let the browser skill connect to a browser the stack serves
  ([`f28a024`](https://github.com/Joonalai/ansible-hostkit/commit/f28a024d059f7af20b47d0ca16102547a87bcdbf))

- Try each change last with one tester, on the issue's own lines
  ([`8bce893`](https://github.com/Joonalai/ansible-hostkit/commit/8bce89387543b3fc56fa6c17b1247d6205f3e0cc))


## v0.6.0 (2026-11-16)

### Features

- Add the gatus role
  ([`b0e3753`](https://github.com/Joonalai/ansible-hostkit/commit/b0e3753076afb7419bea5f7b2636c73586cee57e))


## v0.5.1 (2026-11-14)

### Bug Fixes

- Keep a job that ended early with no commit from going on
  ([`c12a5c8`](https://github.com/Joonalai/ansible-hostkit/commit/c12a5c889e12373a5d4c0e16103a84f5d1c4fa2b))


## v0.5.0 (2026-11-14)

### Bug Fixes

- Show a remote up its refusal, not a connection reset
  ([#7](https://github.com/Joonalai/ansible-hostkit/pull/7),
  [`519a305`](https://github.com/Joonalai/ansible-hostkit/commit/519a305942b03df35870d5ab82fbcefe8a5215fb))

### Features

- Let workbench repositories opt into the gpu-models skill
  ([`d30ebc7`](https://github.com/Joonalai/ansible-hostkit/commit/d30ebc7cfa71cc72261c9f780f42ca0b0c17991b))


## v0.4.1 (2026-11-14)

### Bug Fixes

- Read an account's cpu quota without a backreference
  ([`44ee279`](https://github.com/Joonalai/ansible-hostkit/commit/44ee27962520eb277d7536abe5a1025001a96799))


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
