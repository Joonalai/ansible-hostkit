---
name: propose-issue
description: Opens an issue for work found but not to be done now, labelled as proposed so that nothing starts on it until the user decides. Use for a bug beside the one being fixed, a follow-up the criteria put out of scope, a "Not agreed" item worth its own discussion, or whatever the user asks to have written up.
---

# Proposing an issue

The user decides whether anyone works on it; you only write it up.

1. **Check that none exists**:
   `gh issue list --search "<words>" --state all`. One that does gets a
   comment at most, never a duplicate.
1. **Open it**:
   `gh issue create --label "$CLAUDE_BOT:proposed" --title <title> --body-file <file>`.
   The body says what is wrong or missing, where (`file:line`), why it
   matters, and a suggested approach, short enough to read in a minute.
   End it with where it came from: `Found while working on #<n>`.
1. **Never add a working label, `$CLAUDE_BOT` alone or with `:plan`, `:review` or `:full`**
   to it, or to any issue. Those start work, and only the user starts work;
   your own label would fail the sender check anyway.
1. **Link it** in the PR description, so the user sees it from the work
   it came from.
