# The `gatus` role

Notes for working on this role. The repository's rules are in the root
[CLAUDE.md](../../CLAUDE.md), which applies here too. What it does is in
[docs/gatus.md](../../docs/gatus.md).

- **Gatus expands every `$NAME` and `${NAME}` in its config** from its
  environment, and an unset one becomes empty, not an error. An external
  endpoint whose token went empty would take any heartbeat. The role finds
  every name the config files use and fails without a value for each; keep
  that check in step with how Gatus expands (`os.ExpandEnv`, after turning
  `$$` into a literal dollar sign).
- **Secrets stay out of the config files.** They are diffed and may be shown
  by a dry run with `--diff`. Only `gatus.env` holds values, written with
  `no_log`, 0400, the account's.
- **Gatus panics with no endpoint, external endpoint or suite,** and systemd
  then restarts it in a loop. The role refuses such a config before writing
  anything, and waits for `/health` after a start.
- **`/health` is served as `text/plain`,** so `uri` leaves its JSON in
  `content` unparsed.
- **On the dev container a start takes 90 seconds:** podman's user units
  wait for the system's `network-online.target`, which a container booting
  systemd never reaches. The wait for `/health` allows two minutes. A real
  host starts it at once.
- **The image is `FROM scratch`:** no shell and no curl, so the unit has no
  `HealthCmd`. It runs as root inside, which is the account on the host, so
  the config and data directories are the account's with no user mapping.
- **The firewall is the host's.** The default publishes on loopback alone.
  Never add a firewall task, and never widen the default.
