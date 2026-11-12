# The `ci_runner` role

Notes for working on this role. The repository's rules are in the root
[CLAUDE.md](../../CLAUDE.md), which applies here too.

**The sandbox is the host's, and the role fails closed without it.** The
role writes no firewall rule. It takes `ci_runner_sandbox_unit`, asserts the
unit is loaded and active before it makes the account, ties the account's
`user@<uid>.service` and the dispatcher to it with `Requires=`, and the
dispatcher asks `systemctl is-active` before every round of starts. A
firewall's rules can be flushed while their unit still reads active, so the
host may add `ci_runner_sandbox_check`, a command of its own that looks at
the rules. The dispatcher runs it as root, and anything but exit 0, a
missing command or a timeout included, starts nothing. What the sandbox
must hold is in [docs/ci_runner.md](../../docs/ci_runner.md).

**A restart of the sandbox unit stops every job,** since the account's
manager requires it. A host that changes its rules reloads the unit instead.

**dockerd, not Podman, inside the runner.** Podman runs health checks from
systemd timers, and the runner's container has no systemd, so
`docker compose up --wait` waits forever. GitHub's runner image already
carries dockerd.

**Not `--privileged`.** Rootless, it still hands the container every device
node of the host that the account may open, and some drivers make theirs
world writable. The runner gets all capabilities of its user namespace, no
seccomp filter, unmasked paths and `--systemd=always`, which makes its
cgroups writable. `ci-runner-dockerd` then moves its processes into a leaf
cgroup and hands the controllers down, as Docker's dind does, or a container
with limits fails with "cgroupv2 ... is in threaded mode". The options are
in `dockerd_options()` and `slot_options()` of the dispatcher and nowhere
else.

**Everything is named after `ci_runner_name`.** The dispatcher is installed
as `<name>-dispatcher` and takes the name from that: its config in
`/etc/<name>`, the units and containers `<name>-<pool>-<slot>`, the volumes,
the labels. `vars/main.yml` holds the paths so the inventory cannot move one
without the others. Changing the name under a running dispatcher orphans its
containers and stores: take CI off first.

**65536 IDs for the account.** The images the jobs pull own files as nobody
(65534). A test container's account may have fewer, so the play skips the
image, the stores and the dispatcher there (`is_container_environment`) and
installs and checks the rest.

**One container per slot, by name.** The unit and the container of a slot
are both named `<name>-<pool>-<slot>`, so systemd and podman refuse a second
one.

**The jobs' images are pulled once, not into each store.** `ci-runner-warm`
pulls them into a store of its own and saves each to `images` in the
account's home. The dispatcher, as root, checks the index and copies the
files to `/var/lib/<name>/images`, which root owns, and each runner loads
what its store lacks from there before its job and checks the image's ID
against the file's name. Read only is not enough: a runner has every
capability of its user namespace and can remount its read-only mount
writable, so over a directory of the account's a pull request could change
what the default branch loads. Over root's files the remounted mount still
refuses the write. A file is saved under the image's tag, its reference
without the digest: saved by digest alone it loads with no name, and a job
pulls the image again.

**Runners start in the account's own manager**
(`systemd-run --machine=<account>@.host --user`), not from the dispatcher's
service, so the account's memory and CPU limits hold them and restarting the
dispatcher leaves running jobs alone.

**The runner's one-time config goes in a file,** not an argument, which
every account would see in `/proc`. The file is the account's, 0400, under
`/run/<name>`, and the dispatcher removes it two minutes later. Inside the
container the job runs as root beside the runner and can read it. That is
true of any self-hosted runner, and the config registers only this one
runner for this one job. `ci_runner_job_env` does go into the argument list,
so it never holds a secret.

**The private check is in two places.** The workflows pick the runner only
when `github.event.repository.private` is true, and the dispatcher serves
only a repository GitHub says is private. A pull request in a public
repository can write its own `runs-on`, so only the second one holds against
it.

**Rate limit.** Every GET is conditional on its ETag, and a 304 does not
count against the token's calls for the hour.

**The image's build files are root's.** The account builds from
`image/` in its home, which root owns, so nothing the account runs can
change what the next build makes. `ci_runner_image_extra` is the consumer's
own Containerfile lines and is trusted as the inventory is.

**Removal leaves nothing.** Everything the role or `rootless_account` puts
on the host for the account is in `tasks/absent.yml`, and its last task
fails on anything left. A new file, unit or setting the role adds goes into
both, in the same change. Two things stay on purpose: the packages, and
`rootless_account`'s image rebuild script, which other accounts share. The
sandbox is the host's to remove afterwards.
