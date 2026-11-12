# The `claude_team` role

Notes for working on this role. The collection's conventions are in the
root [CLAUDE.md](../../CLAUDE.md), which applies here too. How the team is
used is in [github_runner](../github_runner/CLAUDE.md) and
[workbench](../workbench/CLAUDE.md).

- **The team is files, and two roles install the same ones.**
  `files/agents/*.md` are Claude Code subagents, each with its own model
  and tools, which the lead calls one at a time. `github_runner` copies
  them to `~coder/claude/agents/`, root-owned and mounted read-only into
  every job; `workbench` to `~bench/.claude/agents/`, in the home its
  containers share. One source, so a role changed here changes for both.
  What differs between the two is the rules around the team, each role's
  `CLAUDE.md.j2`, which say what a role's talk of issues, PRs and previews
  means there.
- **The process is not here.** A role knows only its prompt and these
  files. How the lead runs the rounds, opens PRs and stops is in the
  runner's `templates/lead.md.j2` and the workbench's rules under "The
  lead", on purpose: a role reading how to push and open PRs may do it.
- **A role answers the lead, not a person.** Every role ends with the same
  paragraph: terse, fragments, exact where exactness matters, and the lead
  writes what the user reads. A new role keeps the paragraph, and its
  frontmatter says in `description` when the lead should call it, since
  that is what Claude Code shows the lead.
- **The frontmatter is the definition.** `name`, `description`, `model`,
  and `tools`, which left out means every tool, as `engineer` needs to
  edit. mdformat keeps the header as YAML (`mdformat-frontmatter`), and
  markdownlint takes `name:` as the title (`MD041` in
  `.markdownlint-cli2.yaml`).
- **Skills are procedures, rules are CLAUDE.md.** A skill,
  `<name>/SKILL.md`, is loaded only when Claude decides the task matches
  it, so it holds what is done sometimes and takes many lines of
  commands; what must hold always, the pushing rules, what GitHub may see,
  the levels, stays in the rules every turn carries. A rule that needs a
  skill names it at the point of use, since a description alone is not
  always enough for it to be loaded.
- **Skills come from three places, into one `skills/` directory.** This
  role's `files/skills/` holds what both environments share; the runner's
  and the workbench's own `files/skills/` hold what only they have, passed
  in `claude_team_skill_sources`. A skill of the same name in both is how
  a role's instructions can say "the `preview` skill" and mean the runner's
  compose procedure in a job and the dev server on the workbench.
- **The runner adds a fourth place, per repository.** Its
  `files/repo_skills/` holds skills a repository opts into with `skills:`
  on its inventory entry; the runner copies this role's result into a
  directory per slot and adds those, and each job mounts its slot's. So
  what this role installs under `~coder/claude/skills/` is read by no job
  directly; it is the source of every slot's copy. The workbench has no
  such step: its sessions read this role's directory as it is.
- **A workstation may read these files in place.** A Claude Code plugin
  can link `files/agents` and `files/skills` from an installed copy of this
  collection, for the user's own sessions, with no play and no copy. So a
  role's text stays true for a session the user sits in, too; where it
  cannot, the plugin says what differs, as the workbench's rules do.
- **The copy does not delete.** A role file removed from `files/agents/`,
  or a skill from any of the sources, stays at both destinations until
  removed by hand.
