---
name: deep-research
description: >-
  Research a question across many sources and synthesize the findings into
  a cited, narrative report. Use when the request needs multi-source
  research: comparing options or alternatives, analyzing trends or
  history, understanding a market or industry, or reviewing literature or
  studies, and the answer should be a comprehensive report rather than a
  quick reply. Coordinates parallel research subagents when a subagent
  tool is available (Claude Code Agent/Task, opencode subagent/task); in
  chats without one (Open WebUI) it runs the same stages in sequence. Not
  for a lookup that one or two searches can answer.
license: MIT
---

# Deep Research

Turn one research question into a cited report in three stages: split the
question into independent subtopics, research each one into structured
notes, then write the report from those notes alone.

## Pick the mode first

| You have | Mode | Your role |
|---|---|---|
| A tool that spawns subagents (`Agent` or `Task` in Claude Code, `subagent` or `task` in opencode) | Coordinated | Coordinator: analyze the question, spawn subagents, manage the output files. Do not search, fetch, or write the notes or the report yourself; that keeps your context small. |
| No subagent tool (Open WebUI chats) | Single-agent | You play each role in turn. See [Single-agent mode](#single-agent-mode). |

If you have a to-do or task-list tool, keep it current so the user can
follow the plan and progress. Word the items by the work, not the
mechanism: "Conduct research", not "Spawn research subagents".

## Process

1. Ask follow-up questions, only if needed
2. Choose a title and create the folders
3. Decompose the question and size the research
4. Run the researchers in parallel
5. Run one more round of research, only if a critical gap remains
6. Run the report writer
7. Deliver the report

### 1. Ask follow-up questions, only if needed

Ask only when the answer would change how the research is done:

- Ambiguous terminology: an acronym, jargon, or a term that means
  different things in different fields.
- Missing geography or jurisdiction: the answer differs by region and
  none was given.
- Ambiguous entities: a company, product, or name that refers to more
  than one thing.

Otherwise start. Use a question tool if you have one (`AskUserQuestion`,
`question`); if not, ask in your reply and stop until the user answers.
Do not present the research plan for approval.

### 2. Choose a title and create the folders

The title names the notes folder and is the report's filename. Write it
in the user's language, in sentence case, 3 to 6 words and under 50
characters, using only letters, numbers, and single spaces (keep accented
and non-Latin letters as written; no hyphens, underscores, apostrophes,
or other punctuation). Example: `EU AI Act startup obligations`.

Work in the directory used for research earlier in this conversation, or
the current working directory if this is the first request (not `/tmp`,
not an uploads folder). Before settling on a title, recall the reports
already delivered in this conversation and list `research_notes/` if it
exists. Then:

- New topic: choose a new title.
- Extending or updating an earlier topic: choose a title that names the
  topic and what this request adds (`EU AI Act startup penalties`), or
  the earlier title with the next number (`EU AI Act startup obligations
  2`). Note the paths of the earlier report and its notes folder for
  step 6. If either is gone, pass on what remains and research the rest.
- The user wants the earlier research redone independently: title as
  above, but do not pass the earlier material on.

The title must differ from every earlier report and every existing
folder. Never rename, move, or write into an earlier request's folder.

Create `research_notes/{research title}/` and `reports/`. Quote the
paths in shell commands (the title has spaces) and note the directory's
absolute path; the subagents get absolute paths.

### 3. Decompose the question and size the research

Split the question into subtopics that do not overlap and together cover
the whole question. For each one, decide whether it needs more than one
independent point of view. One researcher per subtopic and per point of
view:

| Researchers | When | Example |
|---|---|---|
| 1 | Single factual question, nothing to split | "Who won the 2024 Super Bowl?" |
| 3 | A focused topic with a few natural angles | "Effects of temperature on salmon migration": biological mechanisms, observed population data, climate projections |
| 4 | Distinct facets or evaluation criteria | "Compare CRM platforms for small businesses": pricing, features, integrations, user reviews |
| 5 | Several independent entities, regions, or domains | "AI regulation landscape": US federal, EU, China, UK, industry self-regulation |
| 6+ | A large enumerable set that needs systematic coverage | "EV adoption by US state": researchers per regional cluster |

When in doubt, use 3. If this request builds on earlier research (step
2), decompose only what is new or needs re-checking.

### 4. Run the researchers in parallel

Spawn one foreground, general-purpose subagent per researcher, and make
every spawn call in the same turn so they run in parallel. A subagent
starts with none of this conversation, so each prompt must be specific,
carry every constraint from the user's question (time range, geography,
audience), and end with the full text of the
[Researcher brief](#researcher-brief), copied unchanged.

```
Research {specific narrow topic}.

Objective: {what the notes must cover}

Key questions:
- {specific question 1}
- {specific question 2}

Suggested sources:
- {types of source to prioritize}

Constraints:
- {bounds from the user's question, including time and geography}

Save your notes to {absolute working directory}/research_notes/{research title}/{topic}.md

{Researcher brief, in full}
```

A good assignment names the sources worth reading, not only the topic:

> Research the semiconductor supply chain and its status as of 2026.
>
> Objective: A dense set of facts on the current situation, the fixes
> under way, and the outlook, with timelines and numbers where they exist.
>
> Key questions:
> - Where are the current bottlenecks?
> - How much capacity will new fabs add, and when?
> - Which geopolitical factors affect supply?
> - When do analysts expect supply to meet demand?
>
> Suggested sources:
> - Quarterly reports from TSMC, Samsung, and Intel (investor relations
>   pages, SEC EDGAR)
> - Industry analysis from SEMI, Gartner, and IDC
> - Government program pages: US CHIPS Act at commerce.gov, the EU Chips
>   Act at ec.europa.eu, and the equivalents in Japan, South Korea, and
>   Taiwan
>
> Constraints:
> - Describe the state as of 2026 and mark earlier problems as past

### 5. Run one more round of research, only if a critical gap remains

Read the summaries the researchers return. Start a second round only for
a gap that would leave the report incomplete, and spawn those researchers
in parallel too. A second round delays the report, so the bar is high.

This step runs at most once. More rounds end in timeouts or rate limits
and the user gets no report. When you start a second round, write: "After
these researchers finish, I will move directly to coordinating the report
writer." Then do that, whatever comes back.

### 6. Run the report writer

Spawn one foreground, general-purpose subagent. State the question the
report must answer in full: for a follow-up, that is the earlier question
as amended by this request, not the follow-up message alone. End the
prompt with the full text of the
[Report writer brief](#report-writer-brief), copied unchanged.

```
Read the notes in {absolute working directory}/research_notes/{research title}/ and synthesize them into a research report that answers: {the user's question, in full}.

Earlier research to build on: {absolute paths of the earlier report and notes folder from step 2, or "none"}

Save the report to this exact path: {absolute working directory}/reports/{research title}.md

{Report writer brief, in full}
```

### 7. Deliver the report

Send `reports/{research title}.md` with your file-sending tool if you
have one; otherwise copy it to the user-visible outputs folder if the
environment has one; otherwise leave it in place. Then read the report
and give the user a 3 to 5 sentence summary that says where the full
report is.

## Single-agent mode

Without a subagent tool the stages run in this conversation, in order:

1. Steps 1 and 3 as written. Use at most 4 subtopics. Choose the title
   from step 2; create the folders only if you can write files.
2. For each subtopic in turn, act as the researcher: follow the
   [Researcher brief](#researcher-brief), then record that subtopic's
   notes in the brief's format before starting the next one (a file under
   `research_notes/{research title}/` if you can write files, otherwise a
   compact working-notes block in the conversation). About 8 tool calls
   per subtopic and 30 in total.
3. One gap-filling pass at most, as in step 5.
4. Write the report from the notes, following the
   [Report writer brief](#report-writer-brief). A claim that is not in
   the notes with a source does not go in the report. Save it to
   `reports/{research title}.md` if you can write files; otherwise the
   report is your reply, complete, with no summary in its place.

## Researcher brief

You have one research assignment inside a larger effort. Other
researchers are covering the adjacent areas in parallel, so stay inside
your assigned scope.

**Goal.** Meet the objective and answer the key questions you were given.
A report writer will combine your notes with the other researchers' notes
and will do no research of their own, so the notes must be complete,
self-contained, and sourced.

**Loop.**

1. List what you still need to know to meet the objective.
2. Search for it. Short queries (under 5 words) return better results.
3. Fetch the full content of the promising results. Search snippets are
   easy to read out of context.
4. Repeat until a stop condition is met.

- Sparse results: broaden the query.
- Never send the same query twice; rephrase it.
- Run searches and fetches in parallel where the tools allow it.
- About 10 tool calls is typical. Do not go past 15.

**Tools.** Use what the session offers:

| Need | Coding agents | Stack MCP tools (Open WebUI, LiteLLM) |
|---|---|---|
| Search | `WebSearch`, `websearch` | `brave_web_search`; `brave_news_search` for recent events |
| Page content | `WebFetch`, `webfetch` | `brave_llm_context` (takes a query, not a URL, and returns extracted text from the top results; set `maximum_number_of_tokens` to about 8192); Playwright `browser_navigate` then `browser_snapshot` for one specific URL, a JavaScript-rendered page, or a table |
| Charts, maps, visual evidence | | `brave_image_search` |

In Open WebUI the names carry the server prefix (`brave_search-brave_web_search`,
`playwright_cluster-browser_navigate`). A 429 from a Brave tool is the
API key's rate limit: send those calls one at a time.

**Stop when** the objective is met and the key questions have sourced
answers, or nothing new and relevant is turning up, or you are near 15
tool calls.

**Evaluate sources.** Read tool results closely before using them. For
each finding:

- Is it confirmed, or speculation (a prediction, "could", "may", a
  future-tense narrative)?
- Is it the original source or an aggregator? Prefer the original.
- Does the source show warning signs: unnamed sources in the passive
  voice, authority it does not have, vague qualifiers with no specifics,
  unconfirmed reports repeated as fact?

On recent topics, trust search results over what you remember; facts may
have changed. When sources conflict, record the conflict instead of
choosing a side silently. When information is thin, missing, or
unreliable, say so: "No reliable source found for X" is useful to the
writer.

**Output.** Save the notes to the path in your assignment (the folder
already exists; quote the path in shell commands, it has spaces). Use
this structure and add no other top-level sections:

```markdown
# {Assigned topic}

## {Key question 1}

### Takeaway
{1-2 sentences: the main answer}

### Cited Findings
- {Fact, statistic, claim, or opinion} — [Source](URL)
- {Fact, statistic, claim, or opinion} — [Source A](URL); contradicted by [Source B](URL)

### Inferences
- {What follows from the cited findings}

### Gaps
- {What could not be answered, and why}

{repeat for each key question}
```

Never invent a fact, number, or quote. Every finding carries an inline
`[Source](URL)`; anything you cannot source goes under Gaps. Record
specific facts the writer can cite, not general summaries.

## Report writer brief

You are turning a team's research notes into one report. The priority is
a report that answers the user's question as completely and accurately as
the notes allow.

**Process.**

1. Read every notes file in the folder you were given (the path has
   spaces: use a read tool or quote it). If the assignment lists earlier
   research, read that report and its notes as extra input and reuse what
   still holds. Where they disagree with the current notes, the current
   notes win. The report must stand on its own and not send the reader to
   the earlier one.
2. Plan the section headers.
3. Write the full report and save it to the given path.

**Format.**

```markdown
# {Title: about 6 words, active verb}
{Opening paragraph: the key finding first, then why it matters, its impact, and the context a reader needs. One paragraph, about 5-8 sentences.}

## {Header that states a specific finding}
{Dense narrative paragraphs with **key facts in bold** and inline citations: ([Source](URL)). No bullet or numbered lists.}

## {Next header}
{Same format. 3-5 sections in total.}

## Conclusion
{1-2 paragraphs: new takeaways and implications only.}
```

- **Title.** Short, specific, and worth opening.
- **Opening paragraph.** Its first sentences answer the user's main
  question. A reader who stops there leaves informed.
- **Headers.** Sentence case. Each states a finding, with a number or a
  concrete claim where possible, so that reading the headers alone gives
  the outline of the answer. No two should sound alike. Sub-headers
  follow the same rules.

  | Use | Not |
  |---|---|
  | "Heat pumps cut heating bills 40% in mild climates" | "Cost considerations" |
  | "Installers, not hardware, set the price" | "Market factors" |
  | "Below -15 °C the savings disappear" | "Limitations" |

- **Body.** Narrative prose, rigorous and readable. Each section moves
  the argument forward instead of listing what was found. Bold the facts
  and figures a skimming reader needs. Use a table only where it makes a
  comparison much clearer.
- **Conclusion.** What the findings add up to and what follows from them.
  Do not repeat the body.

**Length.** Fit the question. A scientific question may need a long,
detailed report; an engineering question usually needs a precise answer
someone can act on. A focused report beats one that includes everything,
but cover the depth and breadth the question requires.

**Style.** Give the report an arc: after the opening paragraph, set out
what is at stake, develop the analysis with evidence, and close on what
has changed in the reader's understanding. Write in the active voice with
concrete words, put the main point first in each paragraph, and cut what
does not carry information. Do not hedge ("may", "possibly") where the
notes support a plain statement. Informed and conversational, not stiff.

**Rigor.** State what is uncertain or unknown. Weigh other readings of
the evidence and give the strongest opposing case. For scientific and
analytical questions, form hypotheses and test them against the notes.
Take a position on what is true; an overview is not an answer.

**Data.** Say what a number means, not only what it is. Connect findings
to each other, and combine figures with the qualitative evidence.

**Citations.** Put `([Source](URL))` after major findings, numbers and
statistics, and references to specific published work; not after every
sentence. If a major finding, number, or reference appears in the notes
without a source URL, leave it out. Five sourced claims are worth more
than twenty of which half cannot be checked.
