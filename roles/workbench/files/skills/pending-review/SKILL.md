---
name: pending-review
description: Reads the user's pending, unsubmitted review on a PR through the API, makes the changes it asks for, and answers each comment in the chat, never on GitHub. Use when the user says "address my review on #<n>" or asks what their review comments were.
---

# Addressing a pending review

The user reviews the PR on GitHub without submitting the review: their
comments sit in a pending review that only they, and their token, can
see.

1. **Find the pending review**:
   `gh api repos/{owner}/{repo}/pulls/<n>/reviews --jq '.[] | select(.state == "PENDING") | .id'`.
   With none, say so and ask; never fall back to the PR's submitted
   comments unless the user says to.
1. **Read its comments**:
   `gh api repos/{owner}/{repo}/pulls/<n>/reviews/<id>/comments`. Each has
   a `path`, a `body` and a `diff_hunk`, the diff ending at the line it is
   on, and the `commit_id` it was made at. It has no `line` while the
   review is pending: find the line by the hunk's last line, in that file.
1. **Skip the comments** whose ids the branch's notes say were dealt with.
1. **Make the changes**, with `reviewer` on review and full-team work,
   fold them into the commits they belong to with the `clean-commits`
   skill, and push.
1. **Answer in the chat, comment by comment**: what you changed, or why
   not. Never on GitHub. Record the ids in the notes.

GitHub does not let the user approve their own PR, so approval is said in
the chat: "#12 is approved". Note in the branch's notes the commit it was
said at; that is the line not to rewrite.
