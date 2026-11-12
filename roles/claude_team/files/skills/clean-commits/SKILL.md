---
name: clean-commits
description: Folds a fix into the commit it belongs to, squashes a branch when the user asks, rebases it onto the default branch, and finds the commit the user last approved, which is the line never rewritten. Use before pushing a fix to an earlier commit, when asked to squash or rebase, and when continuing a branch that carries a WIP commit.
---

# Clean commits

The PR's commits are what the user reviews and what lands on the default
branch: the commits the change should have been made in, no "fix the
previous commit", "address review" or "WIP" among them.

## The line not to rewrite

Commits up to the user's latest approval are fixed. Find it first:

- On the runner, from the PR's reviews:
  `gh pr view <n> --json reviews --jq '[.reviews[] | select(.state == "APPROVED")] | last | .commit.oid'`.
- On the workbench, GitHub does not let the user approve their own PR, so
  the approval is said in the chat and the branch's notes record the
  commit it was said at.
- On the user's workstation, it is said in the chat too, and the lead's
  notes record it.

With no approval, every commit on the branch may be rewritten, and
`<base>` below is the default branch. With one, `<base>` is that commit:
rebase only what came after it, and a fix to an approved commit is a new
commit on top, with a message saying what it fixes. Once the user
approves again, that is the new line.

## Pushing a rewritten branch

Only your own PR's branch, and only with the lease:
`git push --force-with-lease` on the runner, `bench-git push --force` on
the workbench, whose gate keeps the lease itself and refuses when someone
else has pushed since. A refusal means fetch, look at what they pushed,
and build on it.

On the user's workstation nothing is pushed: every "push" below is the
user's step. Stop at the rewritten branch, and say it needs
`git push --force-with-lease` when it was pushed before.

## Folding a fix into its commit

```sh
git commit --fixup=<sha>
git -c sequence.editor=true rebase -i --autosquash <base>
```

Then push as above. A `WIP: interrupted` commit a cut-off job left is
folded the same way when the work continues: `--fixup` of the commit it
belongs to, or its own commit with a real message when it is a change of
its own.

## Squashing on request

When the user asks to squash, do it, approved commits included: one commit
per change a reviewer would want to see apart, each with a message saying
what and why, and the tests passing at every commit
(`git rebase -i <base>` with `sequence.editor=true` and a todo written
first, or `git reset --soft` and recommit in order). Push as above.

## Rebasing onto the default branch

Before an approval, rebase when the default branch has moved under you:
`git fetch origin && git rebase origin/<default>`. After one, merge it in
only if the user asks: a rebase would rewrite what they approved.
