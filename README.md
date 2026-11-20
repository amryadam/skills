# skills

Claude Code skills for backend engineering work. Also installable into any other agent.

## Install

In Claude Code:

```
/plugin marketplace add amryadam/skills
/plugin install amryadam-skills
```

The skills are then available in every session, and updates arrive with the repo —
run `/plugin marketplace update amryadam` to pull them.

### Other agents

The same repo installs into Cursor, Codex, Copilot, OpenCode and 70-odd others
through the [skills CLI](https://github.com/vercel-labs/skills):

```bash
npx skills@latest add amryadam/skills
```

Pick specific targets with `-a`, or install everywhere it finds an agent:

```bash
npx skills@latest add amryadam/skills -a cursor -a codex
npx skills@latest add amryadam/skills --agent '*'
```

## Skills

### `project-docs`

Build the documentation set an engineer — or a coding agent — needs to change an existing
codebase safely on day one, modelled on
[tinycast's `docs/`](https://github.com/abue-ammar/tinycast/tree/main/docs).

Manual only: run `/amryadam-skills:project-docs [repo-path]` (current directory when omitted). The skill writes:

```
AGENTS.md                 the short version: folder map, non-negotiables, definition of done
CLAUDE.md                 one line: @AGENTS.md
CONTEXT.md                domain glossary, in the domain-modeling format
docs/
  README.md               index: Document | Covers | Edit it when
  architecture.md         layers, ownership, request lifecycle, topology, folder tree
  standards.md            naming, style, errors, persistence — as the code already does it
  testing.md              definition of done, suites, manual checks, coverage gaps
  development.md          requirements, setup, build, run, configuration
  <topic>.md              release, configuration, security, data, integrations … when warranted
  known-issues.md         suspected defects, each with the code involved and how to confirm it
  features/<feature>.md   one per business feature, opening with ## Invariants
```

**Grounded, never invented.** Every class, file, command and config key the docs name must
exist; `scripts/check_docs.py` checks links, anchors, the index, the `## Invariants` rule and
every backticked name against the repo. Secrets are named by key, never copied.

**Features come from the entry points.** The skill lists every endpoint, job, consumer, trigger
and UI route, traces each one, and links them into flows; each flow is one feature doc, and every
entry point has exactly one owner or a stated reason for having none. In a workspace of several
repos the result is one docs set, with features that cross projects.

**Names, not citations.** Docs name code by symbol (`PaymentService.pay`) and keep file paths in
each feature's `## Layout` table. `scripts/check_docs.py` fails a doc that cites a file with a
line number. A second agent verifies each doc against the code and keeps its evidence out of
the page.

**Existing docs stop the run.** If the repo already has `docs/`, `AGENTS.md`, `CLAUDE.md` or
`CONTEXT.md`, the skill writes nothing, shows a keep / move / merge plan and waits for approval.

**Bugs stay out of the feature docs.** Reading a whole codebase always turns up suspected
defects; they go in `docs/known-issues.md` with an ID, a severity and a way to confirm, so the
feature docs stay true after the fix.

#### Requirements

- Python 3 (for `scripts/survey.py` and `scripts/check_docs.py`)
- `git`

## Measurement

`evals/` holds the test cases used to check each skill.

- `project-docs`, against three real Spring Boot services: 98% of the checks pass with the
  skill, against 77% for the same prompt without it. The gap is the stop on existing docs, the
  per-feature file map, the glossary format and the separate known-issues list.

## Licence

MIT — see [LICENSE](LICENSE).
