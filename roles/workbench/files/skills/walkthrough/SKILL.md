---
name: walkthrough
description: Makes a PR easy to review from a phone, off GitHub, with a one-page tour of the change served on the preview port, then a short quiz on its behaviour once the user has read it. Use when the user says "walk me through #<n>" or asks for help reviewing a PR.
---

# Walking the user through a PR

Reading a diff on a phone is the hard part of the user's review. A change
small enough to explain in a few lines gets those lines in the chat
instead; say so.

1. **Write a review page**: one HTML file in `/tmp/review-<n>/`, never in
   the checkout, laid out for a phone. In this order:
   - **What and why**, two or three sentences.
   - **The tour**: the files in the order to read them, each with a line
     on what it does and what to look at, linked to its diff at
     `https://github.com/<owner>/<repo>/pull/<n>/files#diff-<sha256 of the path>`.
     Then what can be skimmed, lockfiles, renames, generated or mechanical
     changes, in one list with a word each.
   - **Where to look hardest**: the judgement calls, with the alternative
     not taken and why; what the tests do not cover; what would break
     first. A handful at most: this is where the user's attention goes.
   - **The tests in plain words**: a sentence per new or changed test,
     naming the behaviour it pins ("a push is refused once GitHub has
     moved"). Read together, they are what the code is checked against.
   - **A diagram, only when the change has a shape**: a flow, a state
     machine, an algorithm, data passing between parts. Mermaid, drawn in
     the page by `mermaid@11.4.1` from `cdn.jsdelivr.net`. Draw the change,
     not the whole system, and mark what is new. When `engineer` did the
     work, start from the diagram it drew and `reviewer` checked. A rename
     or a config change gets no diagram, and the page does not apologise
     for it.
   - **The commits in order**, a line each, when there is more than one.
1. **Look at it before the user does.** Serve it with
   `python3 -m http.server $WORKBENCH_PREVIEW_PORT --bind 0.0.0.0 --directory /tmp/review-<n>`
   as a background task, which is the `preview` skill's port and its
   rules: free first, stopped after. Open it in your browser at a phone's
   width and screenshot it: a Mermaid mistake shows as an error box, not a
   diagram.
1. **Give the link**, `$WORKBENCH_PREVIEW_URL`, and stop the server when
   the user is done with the page.
1. **Then a quiz, once the user says they have read it**, never before:
   three to five questions, one at a time, on behaviour and reasons, never
   on names or line numbers: "what happens now when ...?", "why is this
   checked before that?", "which test fails if this line goes?". After
   each answer, say whether it is right and why, briefly. "Skip" skips
   one question or the rest.
1. **A wrong answer is a finding about the code first.** If the code, a
   name, a comment or the page led the user astray, say so and propose the
   fix, which goes the way of a review comment: with `reviewer` on review
   and full-team work, folded into the commit it belongs to. If the code
   was clear and the answer just wrong, explain and move on.

Record in the notes the commit the walkthrough was given at and what the
quiz found.
