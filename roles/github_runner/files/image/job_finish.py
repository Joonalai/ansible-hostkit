#!/usr/bin/python3 -I
"""Push what a Claude job left behind, for the workflow's step after Claude.

    claude-job-finish

Whatever stopped Claude, a usage limit, the time limit or a crash, nothing
it did is lost: uncommitted work is committed, the branch pushed, and a
draft PR opened if there is none. It does nothing when Claude left
everything pushed, and never pushes the default branch: work left on it
moves to wip/<number>-interrupted-<run>. The draft closes the issue, or,
for an issue done in parts, says only that it is a part of it, which the
lead numbers: merging it must not close the issue. Once there is a PR, the
conversation is there, so its notes go on the PR, and on the issue only
when nothing was pushed and no PR exists.

Any related repository the job cloned beside the checkout gets the same, in
its own PR, which says what it is part of and closes nothing, since the
issue is in another repository. One that fails stops none of the others:
the script goes on, and exits 1 at the end.

A branch that moved on GitHub while the job worked, by another job or a
push, refuses the push, and nothing on GitHub's has what the job had. That
goes to <branch>-job-<run> instead, and the PR says so: nothing is lost,
and nothing is overwritten. A branch GitHub still has as this job last
pushed it is the job's own, and what Claude rewrote since, a rebase or a
fix folded into a commit, is pushed over it with a lease on that commit.

A job cut off midway through a rebase leaves HEAD detached, on the
commits replayed so far. The branch is the one the rebase names, never
"HEAD", and since HEAD lacks the commits not yet replayed, GitHub's branch
is never pushed over then: the work goes to <branch>-job-<run>, and the PR
says the rebase is to finish. A HEAD detached otherwise is work on no
branch, and goes to wip/<number>-interrupted-<run> as on the default one.

A lead that stopped with work left writes Status: working in Progress, as
it does while a job goes on. If the job ends there, on its time or cut off,
the work goes on by itself: this comments `@<bot> continue` on the PR,
marked as the workflow's own, which the workflow takes from the App's bot
as from a person. A lead that ended well with work left goes on whenever it
stopped, since one stopped its first part 23 minutes early without asking
for the next. A failed one goes on only after at least half the job's
time, a part cut off at its limit, so that a crash or a usage limit early
on does not go round again. A cancelled one goes on when it was cut off at
its part's limit, which GitHub ends as cancelled, or when GitHub gave up on
the runner, which a wifi stall on the box does to every job at once and
GitHub says in the job's annotation, never when someone cancelled the
run. Either way only ROUNDS times since someone on
the allow-list last commented on the PR. A job that goes on says only
that: no note that it stopped, which the continue right after it would
make false.
A job whose Claude stopped at the usage limit, which WAITING says, is left
at working, with no note that it stopped: claude-job-retry says when it
starts again, once the limit resets.
Otherwise it says in Progress, stopped, and on the PR, that the work waits
for someone, so no PR reads as being worked on when nothing is. When it
waits because it went on ROUNDS times and is still not done, the note says
what the user can do with work that large: another round, the rest in
parts, or a session of their own with the labels off. A PR the
lead marked ready is left alone.

Then it hides as outdated the bot's comments that earlier jobs left on
the thread and the PR and that say nothing now: a stopped note, a marked
`@<bot>` that started a job, and the action's tracking comment where it
holds no more than its placeholder. What a lead wrote, the cost comment
and anything this job posted stay. Hidden, not deleted: the marks are what
the rounds are counted from, and the API still lists them.

The slot's stack, if its repository has one, is the executor's: it stays
up, for the user to try the change, until the slot's next up or until it
has been idle too long.

A PR marked ready goes back to a draft before anything is pushed to it, as
the lead's rules have the lead do: a repository may run its checks on a
ready PR's every push and skip a draft's, and work left over is no PR ready
for review. The lead marks it ready again when the work is done, which runs
the checks once.

Those commits skip the repository's hooks: nothing is lost means nothing,
not even to a commit message rule, and the lead folds them away when it
continues.

On a PR, claude-code-action replaces the repository's Claude settings with
the default branch's before Claude starts, since the PR's head is not
trusted, and leaves them changed in the checkout. Those are not work: saved,
they would undo the PR's own changes to them. So a job on a PR leaves them
out of what it commits, as the action does, and an uncommitted change to one
of them is not saved.

It reads from the environment: OUTCOME (the Claude step's), RUNNER_TEMP
(for claude-job-part's record of when the last part's limit fell), NUMBER (the
issue's or PR's), DEFAULT_BRANCH, LEVEL (the labels' level, for the PR it
opens), RUN_URL, BOT_NAME, BOT_EMAIL and GITHUB_RUN_ID, and for the related
repositories CLAUDE_RELATED_REPOS, GITHUB_REPOSITORY and GITHUB_WORKSPACE,
and GITHUB_EVENT_PATH for whether the job is on a PR. Going on by itself
takes STARTED and MINUTES (claude-job-level's), CLAUDE_ALLOWED_IDS and
CLAUDE_APP_BOT_ID; without them the work waits. gh finds its token in GH_TOKEN.

Standard library only, on the image's python3.
"""

import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from datetime import datetime

# The bot's name, the consumer's: what a comment mentions to start a job,
# and what the labels and the workflow's own marks begin with. The runner's
# unit sets it. Letters, digits and dashes only, since it goes into patterns.
BOT = os.environ.get("CLAUDE_BOT") or "claude"
if not re.fullmatch(r"[A-Za-z0-9-]+", BOT):
    raise SystemExit(f"CLAUDE_BOT is not a name: {BOT!r}")

NAME = re.compile(r"[A-Za-z0-9._-]+")
# claude-job-part's record of a later part's limit, its first part's
# length, and how near the limit a part ends to have been cut off by it.
ENDS = "claude-part-ends"
PART_MINUTES = 52
CUT_OFF_SLACK = 60
TRACKING = re.compile(r"^(\*\*Claude (finished|encountered)|Claude Code is working)")
PLACEHOLDER = "I'll analyze this and get back to you."
COMMENTS = """
query($owner: String!, $name: String!, $number: Int!, $endCursor: String) {
  repository(owner: $owner, name: $name) {
    issueOrPullRequest(number: $number) {
      ... on Issue { comments(first: 100, after: $endCursor) { ...page } }
      ... on PullRequest { comments(first: 100, after: $endCursor) { ...page } }
    }
  }
}
fragment page on IssueCommentConnection {
  pageInfo { hasNextPage endCursor }
  nodes { id createdAt isMinimized body author { __typename login } }
}
"""
HIDE = "mutation($id: ID!) { minimizeComment(input: {subjectId: $id, classifier: OUTDATED}) { clientMutationId } }"
# GitHub's annotation on a job whose runner it stopped hearing from.
LOST = "lost communication with the server"
# What claude-code-action puts back from the default branch on a PR.
RESTORED = (
    ".claude", ".mcp.json", ".claude.json", ".gitmodules", ".ripgreprc", "CLAUDE.md", "CLAUDE.local.md", ".husky",
)

# What runs a command and gives its output, sh or a test's stand-in, and
# the environment the steps hand over.
Run = Callable[..., str]
Env = Mapping[str, str]


def sh(*argv: str) -> str:
    """Run a command and give what it printed; a failure raises."""
    return subprocess.run(argv, check=True, capture_output=True, text=True).stdout.strip()


def on_a_pr(env: Env) -> bool:
    """Whether the event that started the job is on a PR."""
    path = env.get("GITHUB_EVENT_PATH", "")
    if not path:
        return False
    with open(path) as file:
        event = json.load(file)
    return bool(event.get("pull_request") or (event.get("issue") or {}).get("pull_request"))


def parted(run: Run, number: str) -> bool:
    """Whether the issue is done in parts: a PR says on its first line that
    it is one of them; a stranger's saying so only keeps a draft from
    closing the issue.
    Best effort: what cannot be read is no.
    """
    part = re.compile(rf"^Part \d+/\d+ of #{number}\s*$")
    try:
        lines = run(
            "gh", "pr", "list", "--state", "all", "--search", f"#{number} in:body", "--json", "body",
            "--jq", '.[] | .body | split("\\n")[0]',
        )
    except subprocess.CalledProcessError as error:
        print(f"::warning::Could not tell whether #{number} is done in parts: {getattr(error, 'stderr', '') or error}")
        return False
    return any(part.match(line.strip()) for line in lines.splitlines())


def body(number: str, outcome: str, level: str, part: bool = False) -> str:  # noqa: FBT001, FBT002 (the callers pass it by position)
    """The draft's body for work Claude left without a PR, which closes the issue or is only a part of it."""
    first = f"Part of #{number}" if part else f"Closes #{number}"
    return f"""{first}

Opened by the workflow: Claude stopped ({outcome}) before it opened a PR.
Comment `@{BOT} continue` here to pick the work up.

<details><summary>Progress</summary>

Level: {level} · Status: stopped: {outcome}
Next: read the commits on this branch and the issue, and carry on.
</details>
"""


def sibling_body(primary: str, number: str, outcome: str, level: str) -> str:
    """The draft's body in a related repository, which points at the issue in the job's own."""
    return f"""Part of {primary}#{number}.

Opened by the workflow: Claude stopped ({outcome}) before it opened a PR here.
Comment `@{BOT} continue` on {primary}#{number} or its PR to pick the work up.

<details><summary>Progress</summary>

Level: {level} · Status: stopped: {outcome}
Next: read the commits on this branch and {primary}#{number}, and carry on.
</details>
"""


WORKING = re.compile(r"(Status: )working\b[^\n<]*")
# What marks the workflow's own "@<bot> continue", which the workflow
# takes from the App's bot. The lead's commands may not carry it: see
# claude-job-vouch --hook.
AUTO = f"<!-- {BOT}:auto-continue -->"
# How many jobs in a row go on by themselves before the work waits for
# someone on the allow-list, and go_on's word for a work that used them up.
# Four: a full issue often needs three, and with three a PR can stop one
# round short.
ROUNDS = 4
SPENT = -1


def rounds(run: Run, pr: str, env: Env) -> int:
    """The PR's jobs that went on by themselves since someone on the
    allow-list last commented on it.
    """
    number = pr.rstrip("/").rsplit("/", 1)[-1]
    allowed = {str(each) for each in json.loads(env["CLAUDE_ALLOWED_IDS"])}
    lines = run(
        "gh", "api", f"repos/{env['GITHUB_REPOSITORY']}/issues/{number}/comments", "--paginate",
        "--jq", f'.[] | "\\(.user.id) \\(.body | contains("{AUTO}"))"',
    )
    count = 0
    for line in lines.splitlines():
        user, marked = line.split()
        if user in allowed:
            count = 0
        elif user == env["CLAUDE_APP_BOT_ID"] and marked == "true":
            count += 1
    return count


def lost(run: Run, env: Env) -> bool:
    """Whether GitHub ended this job for losing its runner, as its
    annotation says. Best effort: what it cannot read is no.
    """
    repo = env.get("GITHUB_REPOSITORY", "")
    attempt = env.get("GITHUB_RUN_ATTEMPT") or "1"
    try:
        jobs = json.loads(run("gh", "api", f"repos/{repo}/actions/runs/{env['GITHUB_RUN_ID']}/attempts/{attempt}/jobs"))
        ids = [job["id"] for job in jobs["jobs"] if job.get("runner_name") == env.get("RUNNER_NAME")]
        if len(ids) != 1:
            return False
        notes = json.loads(run("gh", "api", f"repos/{repo}/check-runs/{ids[0]}/annotations"))
        return any(LOST in (note.get("message") or "") for note in notes)
    except (KeyError, TypeError, ValueError, subprocess.CalledProcessError) as error:
        print(f"::warning::Could not read why the job was cancelled: {getattr(error, 'stderr', '') or error}")
        return False


def part_ends(env: Env) -> float:
    """When the last part had its limit, as claude-job-part records it."""
    try:
        with open(os.path.join(env["RUNNER_TEMP"], ENDS)) as ends_file:
            return float(ends_file.read())
    except (KeyError, OSError, ValueError):
        return int(env["STARTED"]) + min(int(env["MINUTES"]), PART_MINUTES) * 60


def go_on(run: Run, pr: str, outcome: str, env: Env, now: float) -> int:
    """The round the work goes on in by itself, 0 when it waits, or SPENT
    when it would go on but has gone on ROUNDS times already.
    """
    if outcome not in ("success", "failure", "cancelled") or not env.get("STARTED", "") or not env.get("MINUTES", ""):
        return 0
    if outcome == "failure" and now - int(env["STARTED"]) < int(env["MINUTES"]) * 60 / 2:
        return 0
    if outcome == "cancelled" and abs(now - part_ends(env)) > CUT_OFF_SLACK and not lost(run, env):
        return 0
    try:
        done = rounds(run, pr, env)
    except (KeyError, ValueError, subprocess.CalledProcessError) as error:
        print(f"::warning::Could not count the rounds, so the work waits: {error}")
        return 0
    return done + 1 if done < ROUNDS else SPENT


def own_push(git: Run, branch: str) -> str:
    """What this checkout last pushed to branch, when nothing has updated
    its remote-tracking ref since; '' otherwise. Read before any fetch,
    which would update it.
    """
    tracking = f"refs/remotes/origin/{branch}"
    try:
        if git("reflog", "show", "-n", "1", "--format=%gs", tracking).startswith("update by push"):
            return git("rev-parse", tracking)
    except subprocess.CalledProcessError:
        pass
    return ""


def push(git: Run, branch: str, run_id: str, midway: bool = False) -> str:  # noqa: FBT001, FBT002 (the callers pass it by position)
    """Push HEAD to branch, or when GitHub's has moved on to what HEAD
    lacks, to a branch of its own. The branch pushed to; '' when GitHub's
    branch already has everything. GitHub's still at what this job pushed
    itself, which Claude has rewritten since, is pushed over. Midway
    through a rebase, HEAD lacks the commits not yet replayed, so GitHub's
    branch, when there is one, is never pushed over.
    """
    mine = ""
    if midway and git("ls-remote", "origin", f"refs/heads/{branch}"):
        git("fetch", "-q", "origin", f"refs/heads/{branch}")
    else:
        try:
            git("push", "--no-verify", "origin", f"HEAD:refs/heads/{branch}")
        except subprocess.CalledProcessError:
            mine = own_push(git, branch)
            git("fetch", "-q", "origin", f"refs/heads/{branch}")
        else:
            return branch
    try:
        git("merge-base", "--is-ancestor", "HEAD", "FETCH_HEAD")
    except subprocess.CalledProcessError:
        pass
    else:
        return ""
    if mine and mine == git("rev-parse", "FETCH_HEAD"):
        try:
            git("push", "--no-verify", f"--force-with-lease=refs/heads/{branch}:{mine}", "origin", f"HEAD:refs/heads/{branch}")
        except subprocess.CalledProcessError:
            pass
        else:
            return branch
    rescue = f"{branch}-job-{run_id}"
    git("push", "--no-verify", "origin", f"HEAD:refs/heads/{rescue}")
    return rescue


def rebasing(git: Run) -> str:
    """The branch a rebase stopped midway is rebasing, or ''. HEAD is
    detached then, and `rev-parse --abbrev-ref HEAD` says only "HEAD":
    a job cut off in a rebase once pushed its work to a branch of that
    name, and opened a second PR for its own.
    """
    for state in ("rebase-merge", "rebase-apply"):
        path = git("rev-parse", "--path-format=absolute", "--git-path", f"{state}/head-name")
        try:
            with open(path) as head_file:
                name = head_file.read().strip()
        except OSError:
            continue
        return name.removeprefix("refs/heads/") if name.startswith("refs/heads/") else ""
    return ""


def checked_out(git: Run) -> tuple[str, bool]:
    """The branch the work is on, '' for none, and whether a rebase of it
    stopped midway.
    """
    if midway := rebasing(git):
        return midway, True
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    return ("" if branch == "HEAD" else branch), False


def unpushed(git: Run, branch: str) -> bool:
    """Whether HEAD is not what GitHub's branch already is. A branch the
    lead pushed and marked ready is ahead of the default branch too, and
    pushing it again changes nothing, so its PR stays ready.
    """
    remote = git("ls-remote", "origin", f"refs/heads/{branch}").split()
    return not remote or remote[0] != git("rev-parse", "HEAD")


def to_draft(run: Run, pr: str) -> None:
    """A ready PR back to a draft, before a push to it that changes its
    branch. Best effort: a PR left ready costs a check run, never the work.
    """
    try:
        if not json.loads(run("gh", "pr", "view", pr, "--json", "isDraft") or "{}").get("isDraft", True):
            run("gh", "pr", "ready", pr, "--undo")
    except (ValueError, subprocess.CalledProcessError) as error:
        print(f"::warning::Could not make {pr} a draft again: {getattr(error, 'stderr', '') or error}")


def rescued_note(branch: str, rescue: str, url: str, midway: bool = False) -> str:  # noqa: FBT001, FBT002 (the callers pass it by position)
    """What the thread is told when the work went to a branch of its own, and what to do with it."""
    if midway:
        return (
            f"This job stopped midway through a rebase of `{branch}`, so what it had then is on `{rescue}`, and "
            f"`{branch}` is as it was, with the commits the rebase had not reached yet. Finish the rebase from "
            f"`{branch}` again, taking what `{rescue}` resolved. [Job run]({url})"
        )
    return (
        f"`{branch}` moved on GitHub while this job worked, by another job or a push, so what this job had at its "
        f"end is on `{rescue}` instead of lost. Compare the two and take what is missing. [Job run]({url})"
    )


def settle(run: Run, pr: str, outcome: str, env: Env, now: float | None = None) -> bool:
    """A Progress left at working, now that this job is done with it: the
    next job goes on, or it says the work waits. Whether it went on. A PR
    the lead marked ready is done, whatever its Progress still says: that
    only the lead forgot to update it, and a note that it stopped would be
    false. Whether it said on the PR what comes next: that the work goes
    on, or, when it went on ROUNDS times and is still not done, what the
    user can do with work that large.
    """
    if env.get("WAITING", ""):
        # Stopped at the usage limit: claude-job-retry starts it again once
        # the limit resets, and says so, so Progress stays at working.
        return False
    url = env["RUN_URL"]
    found = json.loads(run("gh", "pr", "view", pr, "--json", "body,isDraft") or "{}")
    if not found.get("isDraft", True):
        return False
    text = found.get("body") or ""
    if not WORKING.search(text):
        return False
    again = go_on(run, pr, outcome, env, time.time() if now is None else now)
    if again > 0:
        note = (
            f"@{BOT} continue\n\n{AUTO}\nThe job's time ran out with work left, so the work goes on in a new "
            f"job by itself, {again} of {ROUNDS} in a row. [Job run]({url})"
        )
        run("gh", "pr", "comment", pr, "--body", note)
        return True
    reason = "the job's time ran out" if outcome == "success" else outcome
    if again == SPENT:
        reason = f"not done after {ROUNDS} jobs in a row"
    run("gh", "pr", "edit", pr, "--body", WORKING.sub(rf"\1stopped: {reason}", text, count=1))
    if again == SPENT:
        run("gh", "pr", "comment", pr, "--body", spent_note(url))
        return True
    if outcome == "success":
        note = f"Stopped with work left: the job's time ran out. Comment `@{BOT} continue` to go on. [Job run]({url})"
        run("gh", "pr", "comment", pr, "--body", note)
    return False


def spent_note(url: str) -> str:
    """What the PR is told when the work went on ROUNDS times and is still not done."""
    return (
        f"Stopped with work left: the work went on by itself {ROUNDS} times in a row and is still not done, "
        "so it waits for you. Work that takes this long is more than one PR can carry here. Comment "
        f"`@{BOT} continue` to give it another round; or `@{BOT}` asking to split what is left into "
        "parts, each a PR of its own; or take it to a session on your own machine, after removing the working "
        f"labels from this PR and its issue. [Job run]({url})"
    )


def siblings(env: Env) -> list[tuple[str, str]]:
    """The related repositories the job cloned, as (name, path)."""
    names = [name.strip() for name in env.get("CLAUDE_RELATED_REPOS", "").split(",")]
    names = [name for name in names if NAME.fullmatch(name) and name.strip(".")]
    if not names:
        return []
    base = os.path.dirname(env["GITHUB_WORKSPACE"].rstrip("/"))
    paths = [(name, os.path.join(base, name)) for name in names]
    return [(name, path) for name, path in paths if os.path.isdir(os.path.join(path, ".git"))]


def create_pr(run: Run, create: list[str], title: str) -> str:
    """Open the PR as a draft, or marked WIP in its title where a draft is refused; its URL."""
    try:
        return run(*create, "--draft", "--title", title)
    except subprocess.CalledProcessError:
        # In case GitHub refuses a draft for this repository.
        return run(*create, "--title", f"[WIP] {title}")


def finish_sibling(name: str, path: str, env: Env, run: Run) -> str:
    """Push what the job left in a related repository; its PR's URL, if any."""
    outcome, number = env["OUTCOME"], env["NUMBER"]
    primary = env["GITHUB_REPOSITORY"]
    repo = f"{primary.split('/')[0]}/{name}"

    def git(*argv: str) -> str:
        return run("git", "-C", path, *argv)

    default = git("rev-parse", "--abbrev-ref", "origin/HEAD").removeprefix("origin/")
    branch, midway = checked_out(git)
    dirty = git("status", "--porcelain")

    if not branch or (branch == default and (dirty or git("log", "--oneline", f"origin/{default}..HEAD"))):
        branch = f"wip/{primary.split('/')[1]}-{number}-interrupted-{env['GITHUB_RUN_ID']}"
    if dirty:
        git("add", "-A")
        git("commit", "--no-verify", "-m", f"WIP: interrupted ({outcome}), work in progress on {primary}#{number}")
    if not git("log", "--oneline", f"origin/{default}..HEAD"):
        return ""

    pr = run("gh", "pr", "list", "-R", repo, "--head", branch, "--state", "open", "--json", "url", "--jq", ".[0].url // empty")
    if pr and unpushed(git, branch):
        to_draft(run, pr)
    pushed = push(git, branch, env["GITHUB_RUN_ID"], midway)
    if pushed != branch and pushed:
        run("gh", "pr" if pr else "issue", "comment", pr or number, *([] if pr else ["-R", primary]),
            "--body", rescued_note(branch, pushed, env["RUN_URL"], midway))
        return pr
    if pr or not pushed:
        return pr
    text = sibling_body(primary, number, outcome, env["LEVEL"])
    create = ["gh", "pr", "create", "-R", repo, "--head", branch, "--base", default, "--label", env["LEVEL"], "--body", text]
    return create_pr(run, create, f"WIP: {primary}#{number}, interrupted")


def outdated(body: str) -> bool:
    """Whether a comment of the bot's says nothing once a later job ran."""
    if body.startswith(("Stopped (", "Stopped with work left")) or f"<!-- {BOT}:auto-" in body:
        return True
    if body.startswith("Claude Code is working"):
        return True
    if TRACKING.match(body):
        said = body.split("---", 1)[1] if "---" in body else ""
        return said.strip() in ("", PLACEHOLDER)
    return False


def tidy(run: Run, env: Env, number: str) -> int:
    """Hide the bot's outdated comments from before this job on a thread;
    how many. Best effort: a comment left showing costs nothing.
    """
    if not env.get("STARTED", ""):
        return 0
    owner, name = env["GITHUB_REPOSITORY"].split("/")
    bot = env["BOT_NAME"].removesuffix("[bot]")
    hidden = 0
    try:
        lines = run(
            "gh", "api", "graphql", "--paginate", "-F", f"owner={owner}", "-F", f"name={name}",
            "-F", f"number={number}", "-f", f"query={COMMENTS}",
            "--jq", ".data.repository.issueOrPullRequest.comments.nodes[] | @json",
        )
        for line in lines.splitlines():
            comment = json.loads(line)
            author = comment.get("author") or {}
            if comment["isMinimized"] or author.get("__typename") != "Bot" or author.get("login") != bot:
                continue
            created = datetime.fromisoformat(comment["createdAt"]).timestamp()
            if created < int(env["STARTED"]) and outdated(comment["body"]):
                run("gh", "api", "graphql", "-f", f"query={HIDE}", "-f", f"id={comment['id']}")
                hidden += 1
    except (KeyError, ValueError, subprocess.CalledProcessError) as error:
        print(f"::warning::Could not hide the outdated comments on #{number}: {getattr(error, 'stderr', '') or error}")
    return hidden


def main(env: Env = os.environ, run: Run = sh) -> int:
    """Save the work in the checkout and the related repositories, and tidy the threads."""
    failed, pr = False, ""
    try:
        pr = finish_checkout(env, run)
    except subprocess.CalledProcessError as error:
        print(f"::error::Could not save the checkout's work: {(error.stderr or '').strip()[:300]}")
        failed = True
    saved = []
    for name, path in siblings(env):
        try:
            if url := finish_sibling(name, path, env, run):
                saved.append(url)
        except subprocess.CalledProcessError as error:
            print(f"::error::Could not save the work in {name}: {(error.stderr or '').strip()[:300]}")
            failed = True
    if saved and env["OUTCOME"] != "success":
        note = "Its work in the related repositories: " + ", ".join(saved)
        run("gh", "pr" if pr else "issue", "comment", pr or env["NUMBER"], "--body", note)
    for number in dict.fromkeys([env["NUMBER"], *([pr.rstrip("/").rsplit("/", 1)[-1]] if pr else [])]):
        if hidden := tidy(run, env, number):
            print(f"Hid {hidden} outdated comment(s) on #{number}.")
    return 1 if failed else 0


# One job's end from top to bottom, in the order the module says: split up, the order would be harder to follow.
def finish_checkout(env: Env, run: Run) -> str:  # noqa: C901
    """Push what the job left in the checkout; its PR's URL, if any."""
    outcome, number, default = env["OUTCOME"], env["NUMBER"], env["DEFAULT_BRANCH"]
    run("git", "config", "user.name", env["BOT_NAME"])
    run("git", "config", "user.email", env["BOT_EMAIL"])

    def git(*argv: str) -> str:
        return run("git", *argv)

    branch, midway = checked_out(git)
    work = ()
    if on_a_pr(env):
        run("git", "reset", "-q", "--", *RESTORED)
        work = ("--", ".", *(f":(exclude){path}" for path in RESTORED))
    dirty = run("git", "status", "--porcelain", *work)

    def ahead() -> str:
        return run("git", "log", "--oneline", f"origin/{default}..HEAD")

    if not branch or (branch == default and (dirty or ahead())):
        branch = f"wip/{number}-interrupted-{env['GITHUB_RUN_ID']}"
    if dirty:
        run("git", "add", "-A", *work)
        run("git", "commit", "--no-verify", "-m", f"WIP: interrupted ({outcome}), work in progress on #{number}")

    pr = run("gh", "pr", "list", "--head", branch, "--state", "open", "--json", "url", "--jq", ".[0].url // empty")
    if not ahead():
        went_on = bool(pr) and settle(run, pr, outcome, env)
        if outcome != "success" and not env.get("WAITING", "") and not went_on:
            note = f"Stopped ({outcome}) before changing anything. [Job run]({env['RUN_URL']})"
            run("gh", "pr" if pr else "issue", "comment", pr or number, "--body", note)
        return pr

    if pr and unpushed(git, branch):
        to_draft(run, pr)
    pushed = push(git, branch, env["GITHUB_RUN_ID"], midway)
    if pushed != branch:
        if pushed:
            run("gh", "pr" if pr else "issue", "comment", pr or number, "--body", rescued_note(branch, pushed, env["RUN_URL"], midway))
        if pr:
            settle(run, pr, outcome, env)
        return pr
    if not pr:
        text = body(number, outcome, env["LEVEL"], not on_a_pr(env) and parted(run, number))
        create = ["gh", "pr", "create", "--head", branch, "--label", env["LEVEL"], "--body", text]
        pr = create_pr(run, create, f"WIP: #{number}, interrupted")
    if not settle(run, pr, outcome, env) and outcome != "success" and not env.get("WAITING", ""):
        note = (
            f"Stopped ({outcome}). The work so far is on this PR; comment `@{BOT} continue` to go on. "
            f"[Job run]({env['RUN_URL']})"
        )
        run("gh", "pr", "comment", pr, "--body", note)
    return pr


if __name__ == "__main__":
    sys.exit(main())
