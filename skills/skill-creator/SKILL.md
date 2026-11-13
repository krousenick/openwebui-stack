---
name: skill-creator
description: >-
  Create or improve a skill for this stack's centralized skills
  (skills/<name>/SKILL.md, published to LiteLLM, Open WebUI, and
  opencode): capture the job and its triggers, write a description that
  loads at the right moments, draft a self-contained body for Claude Code,
  opencode, and Open WebUI, and test it with realistic prompts. Use when
  asked to write, edit, review, or test a skill or SKILL.md, or to turn a
  runbook or workflow into a skill.
license: MIT
---

# Skill Creator

A skill is a set of instructions a model loads only when a task needs
it. The description decides *whether* it loads; the body decides *how
well* the task is then done. Get both right, then prove it with tests.

## How skills work in this stack

- A skill is a directory `skills/<name>/` holding `SKILL.md`: YAML
  frontmatter (`name`, `description`, `license: MIT`) and a Markdown body.
- Clients show the model only each skill's name and description. The
  body is read when the model decides the skill applies, or when an Open
  WebUI user `$`-mentions it or an admin binds it to a model.
- The playbook refuses to publish a checkout with a broken package:
  - `name` equals the directory name: lowercase letters, digits, and
    single hyphens, at most 64 characters.
  - `description` is 1-1024 characters, written as a single line or a
    `>-` folded block (the parser reads only those two shapes).
- Only `SKILL.md` reaches LiteLLM and Open WebUI. Put everything the
  skill needs (subagent briefs, templates, code) in that one file. Files
  beside it (`references/`, `scripts/`) reach opencode only.
- Skills are published, and the discovery index can be public: never
  put secrets, internal hostnames, IP addresses, real domains, or user
  data in one.
- Publishing from the repository: `cd ansible && ansible-playbook
  playbooks/site.yml --tags skills,environment,compose` (`--check` first
  previews what changes). Afterwards the skill appears in
  `GET /v1/skills?custom_llm_provider=litellm_proxy`, in Open WebUI under
  Workspace > Skills, and in opencode after its next role run.

## Process

1. **Capture the job.** What task does the skill do; 5-10 ways a user
   would ask for it; what goes in and what comes out; which clients will
   run it and what tools each has there; what must never happen; what a
   good result looks like. Infer what you can from the conversation or
   the runbook you are converting, and ask only for the rest.
2. **Check for overlap.** List `skills/` and read the descriptions.
   Extend an existing skill instead of adding a near-duplicate, and make
   neighboring descriptions point at each other ("for X use
   other-skill").
3. **Write the description** ([rules](#writing-the-description)).
4. **Write the body** ([rules](#writing-the-body)).
5. **Test** ([below](#testing)) and revise until the tests pass.
6. **Hand it over.** In a checkout, write the file and run the
   frontmatter check. Outside one (an Open WebUI chat), give the complete
   `SKILL.md` in one code block with the path it belongs at, and tell the
   user how it gets published.

## Writing the description

- First sentence: what the skill does, with concrete nouns and verbs
  ("Create and edit Excel workbooks: formulas, formatting, charts...").
- Then "Use when..." with the situations and the words users actually
  say, including synonyms ("spreadsheet", "xlsx", "Excel", "CSV").
- Then the boundary: what it is not for, and which skill to use instead.
- Third person, no "I" or "you", no marketing. Most important words
  first, since some clients truncate. Aim for 400-900 characters.

## Writing the body

- Open with the outcome in one or two sentences.
- Put decisions first: a table of the situations the skill meets (which
  client, which tools are available) and what to do in each. Then the
  procedure as numbered steps, then reference material: formats,
  tables, templates, code.
- Imperative voice. Give the reason behind any rule that is not obvious;
  models apply a rule better when they know why it exists.
- Show, do not describe: an example of a good output, and exact formats
  for anything a later step or another agent consumes.
- Name the tools per client. Claude Code: `Read`, `Edit`, `Bash`,
  `Agent` or `Task`, `WebSearch`, `WebFetch`. opencode: `read`, `edit`,
  `bash`, `subagent` or `task`, `webfetch`, `websearch`. Open WebUI:
  `execute_code` when the user has Code Interpreter on, MCP tools with
  their server prefix (`brave_search-brave_web_search`), no file system
  or shell otherwise.
- When the work splits into independent parts, describe a coordinated
  mode that starts subagents in parallel and a single-agent mode for
  clients without them. Put each subagent brief inline and tell the
  coordinator to copy it unchanged into every prompt: subagents see
  nothing of the conversation.
- Keep it as short as the job allows. Cut what any capable model already
  knows; keep what is specific to the task, to this stack, or to a
  mistake you saw a model make.
- Code in the body must run as written. Test it in the environment the
  skill targets.
- Date facts that will change ("checked 2026-11") instead of stating
  them as permanent.

Template:

```markdown
---
name: <kebab-name>
description: >-
  <What it does, concretely.> Use when <situations and the words users
  say>. <Boundary: not for X; use other-skill for Y.>
license: MIT
---

# <Title>

<One or two sentences: the outcome.>

## Pick the mode first

| You have | Mode | Do |
|---|---|---|
| <tool or client> | <mode> | <what to do> |

## Process

1. <step>
2. <step>

## <Reference: formats, tables, code>
```

## Testing

1. Write 3-5 realistic prompts that should load the skill (vary the
   wording and difficulty) and 2-3 near misses that should not.
2. **Triggering.** For each prompt, ask: reading only the names and
   descriptions of all the skills, would a model pick this one, and only
   this one? Reword the description until the answer is right.
3. **Quality.** Run each prompt with the skill loaded and judge the
   result against the criteria from step 1. With a subagent tool, run
   the same prompt in two parallel subagents, one told to read the new
   `SKILL.md` first and one without it, and compare.
4. Fix the instructions that caused each failure (not the test), then
   run again.
5. In the repository, run the frontmatter check:
   `pytest tests/container_scripts/test_skill_packages.py`, where the
   project says its tests run.

Report what you tested and what changed, and leave publishing to the
user unless they asked you to run the playbook.
