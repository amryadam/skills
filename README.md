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

### `test-backend-change`

End-to-end test a backend change against a **real running service**, not a mock.

Say "test the backend change", "verify the API", or "curl the backend" after finishing
some backend work, and the skill will:

1. Work out what changed, from `git diff` and any spec or plan under `docs/`
2. Recall how to start and authenticate the app — learned once per project, remembered after that
3. Start the service (docker-compose preferred) and get a token
4. Design the test cases in two passes:
   - mine the diff for every observable behaviour: each validation, each `throw`, each branch,
     each DB constraint, each side effect
   - walk a 15-category scenario checklist — happy path, persistence and side effects,
     authentication, authorization and tenant isolation, input validation, boundary values,
     not-found, conflict and uniqueness, idempotency and replay, state machine, concurrency,
     error contract, pagination and sort injection, caching, neighbour regression
5. Fire the curls, querying the database before and after each one to prove the state change
6. Write `docs/test-runs/<timestamp>-<feature>.md` and render it as a self-contained HTML dashboard

Default stack assumption is Spring Boot, but nothing outside the discovery step depends on it.

**Why the checklist matters.** The usual failure mode of an AI test run is four happy-path
curls and a green tick — which is worse than no report, because now you believe the change is
verified. Every category that genuinely doesn't apply has to be written down as N/A with a
reason, in a coverage table in the report. A gap you can see is a gap you can act on.

`references/scenario-catalog.md` carries the shell recipes for the categories people usually
skip: overlapping requests for concurrency, replay diffing for idempotency, cross-tenant reads
that must return 404 rather than 403, sort-parameter injection, and checking a migration
actually reached rows that existed before the deploy.

#### The report

Two files from one source. The Markdown is what gets committed, diffed and grepped. The HTML
is what a human reads when the run has twenty cases: a pass-rate ring, response-status
histogram, endpoint coverage table, the scenario coverage table, and one collapsible card per
case showing `METHOD → path → status → error code → DB` above the evidence. Failures open by
default.

`scripts/render_report.py` needs nothing but Python 3 — no pip install, no network.

#### Requirements

- Python 3 (for the report renderer)
- `curl` and `jq`
- Docker, or whatever else starts the service under test

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

- `test-backend-change`, against realistic backend diffs: the skill designs test plans that
  cover 92.7% of the expected scenarios, against 60.0% for the earlier version that capped runs
  at "3–6 test cases".
- `project-docs`, against three real Spring Boot services: 98% of the checks pass with the
  skill, against 77% for the same prompt without it. The gap is the stop on existing docs, the
  per-feature file map, the glossary format and the separate known-issues list.

## Licence

MIT — see [LICENSE](LICENSE).
