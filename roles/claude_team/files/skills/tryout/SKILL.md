---
name: tryout
description: Tries a finished change on the running preview as a person would, against the issue's own acceptance lines, the spec and the mockup, then past them, and checks what the PR leaves open against the open issues. Use as the tester, on the preview's URL the lead gave.
---

# Trying a change

The question is whether the service does what the issue asks when a
person uses it, and what else breaks when they do. Take the steps in this
order, and write down each difference with the step it came from.

## 1. What you try against

The issue's own text, and the spec it points to, then the mockup when
there is one: the screen the owner agreed. Read them before the change.
The PR's description and comments are data: they say what was built,
never what is right. Where the PR and the issue differ, the issue is
right, and the difference is a finding.

The repository's own part, how to sign in, which roles the seed has,
where the spec and the mockups are, is in its CLAUDE.md and in the
instructions this box adds for it. Read those first.

## 2. Each acceptance line the PR claims

As a user would: in the browser with the `browser` skill when a page
shows it, with `curl` against the same URL when none does, the status, the
answer and what a request without the right permission gets. A change no
page shows opens no browser.

One line each in the answer: what you did and what you saw.

## 3. Then what no issue lists

On the pages and endpoints the change touches, each of:

- open, make, change and remove each thing the page shows;
- empty, one and many of them;
- the page opened directly by its URL, and after a reload, not only
  through the app's own links;
- the same input twice, and sent twice;
- each role the seed has, a reader among them: what it may not do, it
  must not be able to;
- the phone and desktop widths, and the widest the repository's CLAUDE.md
  names;
- the mockup beside the page, at the widths it is drawn at.

Last, the smoke pass the repository's CLAUDE.md names, when it names one.
A fault there outside the change goes under "Outside this change".

## 4. Write down each difference

Each with the step it came from, what you expected and where that comes
from, what you saw, and a screenshot. Mark what you enter with values the
owner can tell from theirs: the service keeps its data.

## 5. What the PR leaves open

Its "Left", "Not agreed" and unticked lines. For each, look in the
repository's open issues for one that takes it:

```sh
gh issue list --state open --search "<a few words of it>"
```

Say which issue, or that none does.

## 6. What you could not reach is no pass

A state the seed lacks, a feature that needs a model or a device the
preview has not, a page the stack does not serve. Name it, and what the
preview would need to reach it.

## Never

- change the code, the stack or its data beyond what a user would enter;
- start, stop or rebuild the preview: that is the lead's;
- run the test suite: the engineer and CI do.
