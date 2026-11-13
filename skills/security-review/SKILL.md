---
name: security-review
description: >-
  Review code changes for exploitable security vulnerabilities: injection,
  path traversal, SSRF, unsafe deserialization, XSS, broken authentication
  or authorization, leaked secrets, and risky Docker, compose, Ansible, or
  CI changes. Reports only issues with a plausible attack path, each with
  the attack and a fix. Use when asked for a security review or audit of a
  diff, branch, PR, or pasted code. For general bugs use code-review.
license: MIT
---

# Security Review

Find the vulnerabilities in a change that an attacker could actually use,
prove the path from attacker-controlled input to impact, and report them
ranked. Hardening ideas without an attack path are not findings.

## Pick the mode first

| You have | Mode | Your role |
|---|---|---|
| A tool that starts subagents (`Agent` or `Task` in Claude Code, `subagent` or `task` in opencode) | Coordinated | Coordinator: build the threat model, start one reviewer per lens in parallel, verify their findings, write the report. |
| No subagent tool (Open WebUI chats) | Single-agent | Work through each lens yourself, in order. |

Never test against live systems: no payloads sent to running services, no
use of credentials you find, no data pulled out of anything. Proof comes
from reading code and from local tests.

## 1. Establish the target

Same as a code review: `git diff HEAD` for uncommitted work, else
`git diff origin/HEAD...HEAD`; `git diff <base>...<branch>` for a branch;
`gh pr diff <n>` and `gh pr view <n>` for a pull request; the files whole
for a path; the pasted text in a chat. Read the change's description for
intent.

## 2. Build a threat model of the change

Write down, briefly, before looking for bugs:

- **Entry points** the change adds or touches: HTTP routes, CLI
  arguments, file and message inputs, webhooks, environment variables,
  configuration files.
- **Who controls each input**: anonymous users, signed-in users, admins,
  other services, the operator.
- **Trust boundaries** the data crosses: browser to server, service to
  service, container to host, tenant to tenant.
- **Assets** at stake: credentials and keys, user data, the ability to
  run code, the ability to change infrastructure.

A finding needs an input someone untrusted controls, a path to a sink,
and an impact on an asset. Use the model to spend time where those meet.

## 3. Review by lens

In coordinated mode start one reviewer per lens that applies to the
change, in the same turn, each prompt carrying the threat model, the
diff command, the lens text below, and the
[Reviewer brief](#reviewer-brief). Skip lenses the change cannot touch
(no web lens for an Ansible-only change).

**Injection and code execution.** Strings built into SQL, shell commands
(`shell=True`, `os.system`, backticks, `eval` of user data), template
engines, LDAP or XPath queries; `eval`/`exec`; dynamic imports from input;
regular expressions with catastrophic backtracking on input an attacker
sends to a hot path.

**Files and network.** Paths joined from input without normalizing and
checking containment (`../`, absolute paths, symlinks); archive extraction
that writes outside the target (zip slip); fetching URLs from input
(SSRF to internal services or metadata endpoints); open redirects; unsafe
deserialization (`pickle`, `yaml.load` without a safe loader, Java or
.NET object streams); XML parsers with external entities enabled.

**Web.** Output not escaped for its context (`innerHTML`,
`dangerouslySetInnerHTML`, `|safe`, `v-html`, `Markup()`); missing CSRF
protection on state-changing requests with cookie auth; CORS that
reflects any origin with credentials; session cookies without `Secure`,
`HttpOnly`, or `SameSite`.

**Authentication and authorization.** New routes or actions without an
auth check; object IDs taken from the request without an ownership check
(IDOR); role or permission fields a user can set on themselves; JWT or
OIDC validation that skips the signature, issuer, audience, or expiry;
comparisons of secrets that are not constant-time; password reset or
invite flows that leak or reuse tokens.

**Secrets and sensitive data.** Keys, tokens, or passwords in code,
committed config, or example files with real values; secrets in logs,
error messages, URLs, command lines, or task output (Ansible tasks
without `no_log`); tokens from `random` instead of `secrets`; weak
password hashing (MD5, SHA-1, unsalted); ECB mode or fixed IVs; personal
data returned or logged beyond what the feature needs.

**Infrastructure.** Containers running as root without need,
`privileged`, `network_mode: host`, the Docker socket mounted, ports
published on all interfaces, `no-new-privileges` missing, internal
services attached to public networks; reverse-proxy routes that expose
admin or debug endpoints; files holding secrets with group or world read;
CI workflows that run untrusted pull-request code with secrets
(`pull_request_target` checking out the PR head), print secrets, or use
unpinned third-party actions with write permissions.

**Dependencies.** New packages: typo-squatted names, abandoned projects,
install scripts; pins removed or loosened; a version with a known
vulnerability in a function the change actually calls.

## 4. Verify and filter

For each candidate, write the attack in one or two sentences: who sends
what, through which entry point, reaching which line, with what result.
Then check what stops it: validation, an auth middleware, escaping by the
framework, a network boundary, the operator being the only one who
controls the input. Keep CONFIRMED (path traced end to end) and PLAUSIBLE
(holds unless a named assumption is false); drop the rest.

Not findings: code that only runs in tests, fixtures, or examples;
theoretical issues with no reachable input; denial of service that needs
far more traffic than the service already accepts; a vulnerable
dependency whose vulnerable code is never called; input only the operator
controls (their own config file), unless the change crosses a privilege
boundary with it.

## 5. Report

```markdown
## Security review: {what was reviewed}

{1-2 sentences: the attack surface the change adds and the verdict}

### 1. {Vulnerability class}: {short claim} ({severity})
`path/to/file.py:88` · CONFIRMED
**Attack:** {attacker} sends {input} to {entry point}; it reaches {sink}
and {impact}.
**Because:** {the missing check or unsafe call, at most 3 quoted lines}
**Fix:** {the specific change, e.g. a parameterized query, a containment
check, an ownership check}
```

Severity: **critical** (remote code execution, authentication bypass,
secrets or data exposed to anyone who can reach the service), **high**
(injection or IDOR with real but bounded impact), **medium** (needs an
authenticated or unusual position), **low** (needs several unlikely
conditions). After the findings, add at most three **hardening notes**
for defense-in-depth gaps that are not exploitable today, marked as such.
No findings is a valid result: list the lenses you checked.

Fix only when the user asks. Do not open issues, post comments, or
contact anyone about a finding unless asked.

## Single-agent mode

Do steps 1, 2, 4, and 5 as written; in step 3 take the applicable lenses
one at a time, following the [Reviewer brief](#reviewer-brief), and keep a
compact list of candidates (file:line, class, one line) between lenses.

## Reviewer brief

You are reviewing a code change through one security lens. Other
reviewers cover the other lenses, so stay on yours, but follow data out
of the changed files when a vulnerability depends on where it goes.

1. Print the diff with the command you were given, then read the changed
   code whole, with the callers and handlers around it.
2. For each place your lens applies, trace backward to where the data
   comes from and forward to where it is used. Note who controls it, per
   the threat model you were given.
3. Keep a candidate only if untrusted input can reach the sink and
   nothing on the path neutralizes it.

Do not edit files, run network tools, or send anything to running
services. Reading code and running the project's existing tests locally
is fine. About 15 tool calls is typical; stop at 25.

Return each finding as:

```
- path/to/file.py:88 [high, confident|unsure] {vulnerability class}
  Attack: {who sends what, through which entry point, to which sink, with what impact}
  Evidence: {at most 3 quoted lines}
  Fix: {specific fix}
```

or `No findings`, followed by one line on what you checked.
