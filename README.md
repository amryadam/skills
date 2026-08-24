# skills

Agent skills for backend engineering work.

## Install

Works with any agent — Claude Code, Cursor, Codex, Copilot, OpenCode and 70-odd others:

```bash
npx skills@latest add amryadam/skills
```

Pick specific targets with `-a`, or install everywhere it finds an agent:

```bash
npx skills@latest add amryadam/skills -a claude-code -a cursor
npx skills@latest add amryadam/skills --agent '*'
```

### Claude Code plugin

If you'd rather install it as a Claude Code plugin, so updates arrive with the repo:

```
/plugin marketplace add amryadam/skills
/plugin install amryadam-skills
```

Update later with `/plugin marketplace update amryadam`.

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

## Measurement

`evals/` holds the test cases used to check the skill against realistic backend diffs.
On the current set the skill designs test plans that cover 92.7% of the expected scenarios,
against 60.0% for the earlier version that capped runs at "3–6 test cases".

## Licence

MIT — see [LICENSE](LICENSE).
