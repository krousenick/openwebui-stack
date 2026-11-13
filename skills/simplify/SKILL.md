---
name: simplify
description: >-
  Clean up changed code without changing its behavior: reuse existing
  helpers, remove needless abstraction and dead code, flatten convoluted
  logic, cut wasted work, then re-run the project's checks. Use when asked
  to simplify, clean up, tidy, refactor, or shrink a diff, or after a
  change works and before review. Not for finding bugs (code-review) or
  vulnerabilities (security-review).
license: MIT
---

# Simplify

Make a working change smaller and clearer while keeping its behavior
exactly the same, then prove nothing broke.

## Scope

Work on the changed code and its immediate surroundings, not the whole
codebase:

| The user names | Scope |
|---|---|
| Nothing, in a git checkout | `git diff HEAD`; if empty, `git diff origin/HEAD...HEAD` |
| A branch, commit, or PR | Its diff (`git diff <base>...<branch>`, `git show <sha>`, `gh pr diff <n>`) |
| A file or function | That code |
| Code pasted into the chat | That code; return the revised version instead of editing files |

Read the project's instruction files (`AGENTS.md`, `CLAUDE.md`) and the
surrounding code first: the house style decides what "simpler" means.

## What to look for

**Reuse.** New code that re-implements something the codebase already
has: a helper, a utility module, a standard-library function, a framework
feature. Search for it before deciding (`grep -rn` for the key verb or
noun, the module where similar code lives, the imports neighbors use).

**Simplification.** Abstractions with one caller and no second one in
sight; wrapper functions that only forward their arguments; flags that
are always passed the same value; nested conditionals that early returns
or a lookup table would flatten; duplicated branches; dead code, unused
variables, parameters, and imports; checks for conditions the types or a
caller already rule out.

**Efficiency.** Work repeated inside a loop that could happen once; a
query or request per item where one batched call would do; reading a
whole file or collection to use one element; copies made only to be
thrown away. Only where the gain is real for the data sizes involved.

**Altitude.** Logic at the wrong layer: business rules in a request
handler, parsing spread across callers, configuration hard-coded in the
middle of a function. Comments that restate what the code does (delete
them) as opposed to why it does it (keep them).

## Rules

- Behavior stays identical, including error messages, ordering, and
  edge cases. If a cleanup would change behavior, leave it and mention it.
- Do not change public interfaces (exported names, signatures, config
  keys, CLI flags) unless the user asked.
- No new dependencies. Match the project's style, even where you would
  write it differently.
- Leave code outside the scope alone, even if it could be simpler.
- A cleanup must pay for itself: fewer lines, fewer concepts, or less
  work, without making the code harder to follow. When in doubt, skip it.

## Process

1. Read the scope and list candidate cleanups, each with the file, the
   change, and what it buys.
2. For a diff over about 300 lines, when you have a subagent tool, start
   read-only subagents in parallel, one per lens (reuse, simplification,
   efficiency, altitude), each given the diff command, the lens text from
   [What to look for](#what-to-look-for), and the [Rules](#rules); ask
   each for candidates in the step 1 format. Decide and apply yourself.
3. Apply the cleanups in order of value, one logical change at a time.
4. Run the project's checks after each group of related edits: tests,
   linter, formatter, type checker (whatever the project uses; see its
   instruction files, `Makefile`, `package.json`, or `pyproject.toml`).
   If a check fails because of a cleanup, revert that cleanup rather than
   "fixing" the test.
5. Re-read the final diff once as a reviewer would.

Do not commit unless the user asks.

## Report

```markdown
## Simplified: {scope}

**Applied**
- `path/file.py:40` Replaced the hand-written retry loop with the existing
  `with_retries()` helper (-18 lines).
- ...

**Considered and skipped**
- `path/other.py:12` Merging the two parsers would change the error text
  callers match on.

**Checks:** {commands run and their result}
```

In a chat without a repository, give the revised code in full, then the
same Applied and Skipped lists.
