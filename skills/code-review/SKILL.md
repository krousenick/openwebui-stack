---
name: code-review
description: >-
  Review code changes for correctness bugs: a working-tree diff, branch,
  pull request, commit, or pasted code. Proves each defect with a concrete
  failing scenario and ranks findings by severity. Use when asked to
  review a diff, PR, or file for bugs, or before merging. Starts parallel
  reviewer subagents when the client has them; runs in sequence in Open
  WebUI. For cleanups use simplify; for vulnerabilities, security-review.
license: MIT
---

# Code Review

Find the bugs a careful reviewer would block a merge for, prove each one,
and report them ranked by severity. Style, naming, formatting, and missing
comments are out of scope unless they hide a bug.

## Pick the mode first

| You have | Mode | Your role |
|---|---|---|
| A tool that starts subagents (`Agent` or `Task` in Claude Code, `subagent` or `task` in opencode) | Coordinated | Coordinator: collect the diff and its context, split the review, start the reviewers in parallel, verify what they report, write the report. |
| No subagent tool (Open WebUI chats) | Single-agent | Run every pass yourself, in order. See [Single-agent mode](#single-agent-mode). |

If you have a to-do or task-list tool, keep it current so the user can
follow along.

## 1. Establish the target

| The user names | Review this |
|---|---|
| Nothing, in a git checkout | Uncommitted and staged changes: `git diff HEAD`. If that is empty, the branch against its base: `git diff origin/HEAD...HEAD` (three dots diff from the merge base). |
| A branch | `git diff <base>...<branch>`; the base is the default branch unless the user says otherwise (`git rev-parse --abbrev-ref origin/HEAD`). |
| A pull request | `gh pr diff <number>`, plus `gh pr view <number>` for the description. |
| A commit | `git show <sha>` |
| A file or directory | The files as they are, whole. |
| Code pasted into the chat | What was pasted. Ask for surrounding code only when a finding depends on it. |

Then read what defines "correct" for this change: the PR description or
commit messages (intent), and the project's instruction files
(`AGENTS.md`, `CLAUDE.md`, `CONTRIBUTING.md`) for its rules.

Effort follows the user's words ("quick look", "thorough"); default to
standard:

| Effort | Reviewers | Report |
|---|---|---|
| Quick | One pass over the whole diff | Confirmed findings only, at most 5 |
| Standard | One reviewer per area, at most 4 | Up to 10 findings |
| Thorough | One reviewer per area plus a cross-cutting reviewer for contracts and state, at most 8 | Every confirmed or plausible finding |

## 2. Gather context

A hunk alone rarely shows a bug. For each changed function, read the whole
function, its callers (search for the name), and the tests that exercise
it. For configuration and schema changes, find every consumer of the
changed keys. Note the size of the diff (`git diff --stat`): over about
1,500 changed lines, split it into more areas instead of skimming.

## 3. Run the reviewers (coordinated mode)

Group the changed files by area (one module, feature, or layer per group)
so each reviewer can hold its area in mind. Start one reviewer subagent per
group, all in the same turn so they run in parallel. A subagent starts
with none of this conversation, so its prompt must carry everything it
needs and end with the full text of the
[Reviewer brief](#reviewer-brief), copied unchanged:

```
Review these changes for correctness bugs.

Intent of the change: {PR description, commit messages, or the user's words}
Repository root: {absolute path}
Files in your area: {paths}
Command that prints your part of the diff: {e.g. git diff origin/HEAD...HEAD -- path/a.py path/b.py}
Project rules that define correct behavior: {from AGENTS.md and similar, or "none found"}

{Reviewer brief, in full}
```

Pick a read-only subagent type where the client offers one (Claude Code
`Explore`, opencode `explore`); otherwise the general-purpose one.

## 4. Verify every finding

Reviewers over-report. Before anything reaches the report, check each
candidate yourself (for a long list, give batches to verifier subagents
with the same verification steps):

1. Re-read the code path the finding describes, from the entry point to
   the faulty line.
2. Make the failing scenario concrete: the input, state, or call order,
   and the wrong result or crash it produces.
3. Look for what might already prevent it: validation upstream, types, a
   caller that never passes that value, a test that covers it.
4. If commands are allowed and a check is cheap, prove it: run the
   existing test, or a throwaway test or one-liner outside the tracked
   files. Never commit it.

Mark each finding CONFIRMED (traced end to end), PLAUSIBLE (holds unless
an assumption you could not check is false; name the assumption), or drop
it. Merge duplicates that several reviewers found.

## 5. Report

```markdown
## Code review: {what was reviewed}

{1-2 sentences: what the change does, and the verdict: ready to merge, or
how many blocking findings}

### 1. {The defect in a short claim} ({severity})
`path/to/file.py:123` · CONFIRMED
**Fails when:** {input or state} → {wrong result, crash, or data loss}
**Because:** {the code fact that causes it, quoting at most 3 lines}
**Fix:** {the smallest change that removes the defect}
```

Order by severity, then confidence. Severity: **critical** (data loss or
corruption, a crash on a common path, a security-relevant wrong result),
**high** (a wrong result on a realistic path), **medium** (an edge case
or error path), **low** (latent: correct today, wrong after a likely
change). No findings is a valid result: say what you checked.

After the report, fix only what the user asks you to fix. Posting PR
comments, pushing, or changing anything outside the working tree needs
the user's explicit request.

## Single-agent mode

Without a subagent tool, do steps 1, 2, and 4 as written and replace
step 3 with one pass per area, in order, following the
[Reviewer brief](#reviewer-brief) yourself. Keep compact working notes
(file:line and one line per candidate) between areas so the report step
sees every candidate. In a chat with no repository, review the pasted
code and say which context you did not have.

## Reviewer brief

You are reviewing one area of a code change for correctness bugs. Other
reviewers cover the rest of the change, so stay inside your files, but
follow calls out of them when a bug depends on what the callee does.

**Process.**

1. Print your part of the diff with the command you were given.
2. Read each changed function whole, then its callers and the tests that
   exercise it.
3. Check the changes against the list below.
4. For each candidate, trace the failing path once more before you keep
   it.

**Look for.**

- Logic: an inverted or wrong condition, off-by-one, the wrong variable,
  operator precedence, a branch that can never run, a loop that exits
  early or never.
- Unhandled inputs: `None` or null, empty collections, zero, negative
  numbers, missing keys, unexpected types, Unicode, time zones and DST,
  very large inputs.
- Error handling: exceptions swallowed or reported as success, a partial
  write left behind, cleanup skipped on the error path, retries that
  repeat a side effect.
- State and concurrency: shared mutable state, races between check and
  use, ordering assumptions, caches that go stale, operations that are
  not idempotent but are retried.
- Contracts: a changed signature, return shape, default, or config key
  that callers or deployed data still rely on; a migration that is
  missing.
- Resources: files, connections, or locks not released; unbounded
  growth; network calls without timeouts.
- Data: wrong units, rounding or precision loss, encoding errors, a
  regular expression or query that matches more or less than intended.
- Tests: a test that asserts the buggy behavior, or one that no longer
  exercises what its name says.

**Not findings.** Style, naming, formatting, missing documentation,
speculative future requirements, and "consider adding tests" without a
concrete bug the test would catch.

**Rules.** Do not edit files. Read-only commands and the project's
existing tests are fine; nothing that changes the checkout, the network,
or other systems. About 15 tool calls is typical; stop at 25.

**Return** each finding as:

```
- path/to/file.py:123 [high, confident|unsure]
  Defect: {one sentence}
  Fails when: {concrete input or state} -> {wrong outcome}
  Evidence: {at most 3 quoted lines}
  Fix: {smallest fix}
```

or `No findings`, followed by one line on what you checked.
