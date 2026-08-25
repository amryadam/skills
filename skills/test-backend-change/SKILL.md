---
name: test-backend-change
description: End-to-end test a Spring Boot backend change with curl plus database verification against a real running service, producing a runnable Markdown report plus a rendered HTML dashboard under docs/test-runs/. Use this whenever the user wants proof that a backend change works — including phrases like "test this endpoint", "curl the backend", "verify the API", "test what I just built", "test the backend change", "fire curls", "edge cases on this endpoint", "what could go wrong with this API", "thorough backend test", "smoke test the backend", or any time the user has just finished a backend implementation (endpoint, DTO, migration, auth rule, error handling) and wants it exercised for real. The skill discovers the change from git diff and spec/plan files under docs/, recalls (or learns and remembers per-project) how to start the service and authenticate, designs a case list from every behaviour in the diff plus a 15-category checklist (auth, tenant isolation, boundaries, conflict, idempotency, state machine, concurrency, error contract, pagination, caching, neighbour regression), sizes it with a depth knob (smoke / standard / thorough), runs the cases in parallel worker subagents with DB queries before and after every call, and writes one report with a scenario-coverage table that names every category tested, N/A, or not run. Default stack assumption is Spring Boot.
---

# Test Backend Change

End-to-end test a backend change by:

1. Discovering what changed (from git, any spec/plan under `docs/`, and one confirmation question that also states the run size)
2. Understanding the app — recalled from per-project memory if available, otherwise discovered fresh and saved to memory for next time
3. Starting the service (docker-compose preferred) and authenticating
4. Designing the case list from every behaviour in the diff plus a 15-category checklist, sized by the `depth` knob
5. Firing every case with a DB query before and after, in parallel workers when the list is big
6. Assembling one report under `docs/test-runs/<timestamp>-<feature>.md` and rendering it as an HTML dashboard

**Default stack:** Spring Boot. The patterns below assume Spring unless evidence in the repo says otherwise.

## Knobs

| Knob | Values | Default | Effect |
| --- | --- | --- | --- |
| `depth` | `smoke` / `standard` / `thorough` | `standard` | how many cases survive the cap — smoke = P1 only (≤ 8), standard ≤ 20, thorough = no cap |
| `max` | integer | from `depth` | explicit cap; overrides the number `depth` implies |
| `workers` | 1–4 | 2 | how many execution subagents run at once |
| `model` | `haiku` / `sonnet` / `opus` / `fable` | `sonnet` | model for the workers |
| `planner` | same | inherit | model for the planner (omit the `model` parameter → the subagent inherits the session's model) |
| `only` | `3,7,12` | — | rerun only these case numbers from the most recent report for the same feature |

Read the knobs in this order, first hit wins:

1. `key=value` tokens in the invocation — `/test-backend-change depth=thorough workers=3`
2. Plain words in the request — "quick", "smoke", "sanity" → `smoke`; "thorough", "deep", "everything", "all scenarios", "edge cases" → `thorough`
3. A `## Test-run defaults` block in the project's `reference_backend_setup.md` memory (`depth: standard`, `workers: 2`, `model: sonnet`)
4. The defaults in the table

Echo the resolved knobs in the Step 1 confirmation so the user can change them in the same breath.

Why workers default to `sonnet`: executing a case is mechanical — run the curl, run the query, compare with the expectation the planner wrote down. The judgment is in the plan. Why the planner inherits the session model: reading a diff for behaviours and deciding which checklist rows apply is where a stronger model pays for itself.

## Execution model: planner → workers → assemble

A run produces a lot of intermediate output — startup logs, full responses, repeated DB dumps. None of it belongs in the main conversation; only the case list, the report path and the pass/fail/coverage summary do.

**Main session:** Step 1 (discovery + one confirmation), the dispatches, the assemble step, Step 8 (relay).

**Planner subagent** (`model` = the `planner` knob; omit the parameter to inherit): Steps 2–5 — recall memory, start the service, authenticate, design and size the case list, write the report skeleton. Returns the numbered case list with its worker assignment.

**Worker subagents** (`model` = the `model` knob; `workers` of them, dispatched **in the same turn** so they overlap): Step 6 for their subset only. Each writes its cases to its own part file. A worker never touches another worker's cases or the skeleton.

**Concurrency pass:** if the plan has concurrency cases, dispatch one more worker for them *after* the others return. Load from parallel workers skews the very thing those cases measure.

**Assemble:** run `scripts/assemble_report.py` on the skeleton. It stitches the parts in case order, fills the verdicts into the case list, writes the summary, and renders the HTML. Deterministic, no model involved.

Use the `Agent` tool with `subagent_type: "general-purpose"` and the `model` parameter for every dispatch. Subagents inherit no context, so each brief must be self-contained.

**Planner brief — must include:**

- The exact change under test (file paths, endpoints, behaviour) as discovered in Step 1 and confirmed by the user. Don't make the planner re-derive this.
- Pointer to any spec/plan file under `docs/` that's relevant.
- The resolved knobs (`depth`, `max`, `workers`, `only`).
- The project's memory directory path so it can read/write `reference_backend_setup.md`.
- The absolute path to this skill directory (for `references/scenario-catalog.md`), and the instruction to execute Steps 2–5 of this file only — no curls beyond the auth check.
- The instruction to walk the full Step 5b checklist and fill in the `## Scenario coverage` table, marking every category with case numbers, N/A + reason, or "not run (over cap)" + case numbers. Without this line the planner optimises for finishing, not for coverage.
- The required return format (below).

**Planner must return** (under ~300 words plus the list):

- Absolute path of the skeleton report and its `parts/` directory.
- The auth recipe (login curl + how the credential travels afterwards) and the DB query command pattern, so workers don't rediscover them.
- The numbered case list, one line each: `N. [worker A|B|C] [P1|P2] title — expected <status/state>`.
- Categories it could not cover and why ("no second tenant available, so isolation is untested").
- Whether memory was created or updated.

**Worker brief — must include:**

- The base URL, auth recipe, DB command pattern and the identifier prefix for this worker (`A-<timestamp>`), verbatim from the planner's return.
- Its case subset, verbatim, each with the expected outcome. Nothing else from the plan.
- The absolute path of its part file: `<skeleton-dir>/<skeleton-stem>/parts/<worker>.md`.
- The instruction to execute Step 6 of this file only, write **only** `### Test N:` sections in the Step 7 format, and end every section with a `**Result:**` line.
- An explicit reminder: **do not delete or clean up any test data** (see Step 6). Leave all rows in place.
- A reminder that it cannot ask the user anything; a case it cannot run gets `**Result:** SKIP — <reason>`.

**Worker must return:** the part file path, its pass/fail/skip counts, and one line per FAIL or surprise. Under ~120 words.

**When NOT to dispatch:** if discovery reveals the diff is empty, unrelated, or the user wants to debate scope, resolve that conversationally first. Only dispatch once the change under test is pinned down.

## Why this exists

A unit test passing isn't proof a backend change actually works. This skill closes that gap: it exercises the change against a real running service with real auth and a real DB, and leaves a runnable artifact (the curls + responses + DB output) so the user or a teammate can replay and inspect the run.

The persistent memory step is what makes this fast on repeat use. The first run on a project, you spend a few minutes learning auth and startup. Every run after that is plug-and-play.

## On using `curl` vs `httpie`

The user's global rules prefer `httpie` over `curl` for ad-hoc shell use, but the **report itself uses `curl`**. The point of the report is that anyone can copy-paste the requests and replay them — `curl` is the universal lingua franca for API requests. You can use httpie freely for interactive exploration during testing, but the curls that land in the report must be runnable `curl` invocations.

## Process

### Step 1 — Discover what changed

Gather these in parallel:

- `git status` (uncommitted work)
- `git diff` (unstaged + staged changes)
- `git log --oneline -10` and `git diff main...HEAD` (committed work on the branch)
- Look in `docs/` for recently-modified `.md` files matching the change — common patterns: `docs/<feature>-plan.md`, `docs/<feature>-spec.md`, anything from a `superpowers:writing-plans` or `superpowers:brainstorming` run. Match by domain terms appearing in the diff.
- Check the most recent commit messages and current branch name for hints

Then ask the user **one** short confirmation question that also states the run size, e.g.: "I see you added `POST /api/orders/discount` and modified `OrderService.applyDiscount` — that's the focus, right? New endpoint with a state machine and tenant scoping, so expect ~15–20 cases at `depth=standard`, 2 sonnet workers, roughly 10 minutes. Say `smoke` or `thorough` to change." Don't pile on follow-ups; let them correct you if you're off.

If the working tree is clean and the branch has nothing on it, ask the user what to test before continuing.

### Step 2 — Recall (or learn) how the app works

The skill maintains a per-project memory of the backend's setup, so subsequent runs skip rediscovery.

Memory lives at the existing auto-memory path for the current project (the same one referenced in your top-level memory instructions). Save findings there as a `reference` type memory.

**First, check existing memory.** Look for a file like `reference_backend_setup.md` (or whatever name was used previously) under the project's memory directory. If one exists, read it. It should cover:

- How to start the service (docker-compose command, or `./mvnw` / `./gradlew`)
- Base URL and port
- Auth flow: which endpoint issues tokens, request body shape, where the token goes on subsequent requests
- DB type, connection details, and how to run queries against it
- Optional `## Test-run defaults` block with knob defaults for this project

**Verify the memory before trusting it.** Memory can drift. Quickly confirm:

- The docker-compose / build file referenced still exists
- The auth endpoint still resolves in the code (grep for the controller)
- The DB config in `application.yml` matches what memory says

If anything is stale, update the memory rather than acting on outdated info. Trust what you observe in the code now.

**If no memory exists**, discover the setup:

- **Service start**: look for `docker-compose.yml` / `docker-compose.yaml` / `compose.yml`. Prefer this over Maven/Gradle for parity with how the app likely runs in CI/staging. If none, fall back to `./mvnw spring-boot:run` or `./gradlew bootRun`.
- **Base URL/port**: `application.yml` / `application.properties` — `server.port`, `server.servlet.context-path`. Check profile-specific files (`application-local.yml`).
- **Auth**: search for `SecurityConfig`, `JwtFilter`, `WebSecurityConfigurerAdapter`, `SecurityFilterChain`, controllers under `/auth`, `/login`, `/oauth`. Identify the login endpoint, the request body shape, how the token comes back (response field name), and how it's sent on subsequent requests (typically `Authorization: Bearer <token>`).
- **DB**: `spring.datasource.*` in config + the JDBC driver in `pom.xml` / `build.gradle` tells you the type. Note connection details and figure out the query path — usually `docker exec -i <db-container> psql -U <user> -d <db> -c "..."` for Postgres in compose, similar for MySQL/Mongo.
- **Test credentials**: check `data.sql`, Flyway / Liquibase migrations under `src/main/resources/db/migration/`, or a seed file for a known test user before asking the user for one. Note a **second** user or tenant if one exists — the authorization and isolation cases need one.

Save what you found as a `reference` memory and add a one-line pointer in `MEMORY.md`. Keep it structural, not ephemeral — auth endpoint shape, startup command, DB connection details. Avoid memorizing things that change frequently (current branch, today's seed data).

### Step 3 — Start the service

If a `docker-compose.yml` was found:

```bash
docker compose up -d
```

Wait for readiness. If Spring Boot Actuator is enabled, poll `/actuator/health`:

```bash
until curl -sf http://localhost:8080/actuator/health > /dev/null; do sleep 2; done
```

If no actuator, poll the base URL until you get any HTTP response (not connection refused). Cap the wait at ~60 seconds and surface the failure clearly if it times out — don't loop forever.

If no docker-compose is present, start with Maven/Gradle in the background and tail logs to confirm startup ("Started Application in X seconds"). If startup fails (port in use, DB not ready, missing env), surface the error to the user rather than working around it silently.

### Step 4 — Authenticate

Run the auth flow you discovered. Capture the token into a shell variable so you don't paste it into every curl:

```bash
TOKEN=$(curl -s -X POST http://localhost:8080/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email": "test@example.com", "password": "secret"}' | jq -r '.access_token')
```

If the auth flow needs a user that doesn't exist, check seed data and migrations before asking — there's often a default test user.

The planner runs this once to prove the recipe works and writes the recipe into its return. Each worker then logs in itself with the same recipe — sessions are cheap, and a worker that owns its session can renew it when it expires mid-run.

### Step 5 — Design the test cases

This is the step that decides whether the run is worth anything. A report with four cases on a change that has fourteen observable behaviours isn't a light test — it's a false clean bill of health, and it's worse than no report at all, because the user now believes the change is verified.

So build the case list from two directions, reconcile them, then size the result.

#### 5a — Mine the diff for behaviours

The change itself is the primary source of cases. Read the actual diff hunks (not just the file names) and list every observable behaviour the new code can produce. Each of these is a candidate case:

- Every new or changed endpoint, and every HTTP method on it
- Every validation constraint — `@NotNull`, `@Size`, `@Pattern`, `@Min`, a custom validator, or a hand-written `if (...) throw`
- Every `throw` and every `catch` that maps to a status code — each one is a response a client can actually receive
- Every new branch in a service method: a state check, a role check, a feature flag, a null guard, an early return
- Every DB constraint the change adds — `NOT NULL`, `UNIQUE`, a foreign key, a `CHECK`, an index used for dedup — and every migration/changeset, including whether it reaches **existing** rows and **existing** tenant schemas, not just a freshly created one
- Every side effect: a published event, an email, a cache eviction, an audit row, an outbox entry
- Every new config property and its default

When you finish, you should be able to point at each meaningful hunk of the diff and name the case that covers it. A hunk with no case is untested code, and it should either get a case or an explicit line in the coverage table saying why it can't be reached from the API.

#### 5b — Walk the scenario checklist

Mining the diff finds what the change *does*. This checklist finds what the change *forgot* — and that's where the bugs are. Walk every row. For each, either name the case(s) that cover it or write **N/A + a one-line reason**. Deciding "not applicable" in writing takes five seconds; deciding it silently is how a run ends up with four cases.

1. **Happy path** — the primary success, asserting the exact response shape, not just the status
2. **Persistence & side effects** — the row is really there, the event was really published, the mail really landed (query the DB / queue / mailpit, don't infer it from a 201)
3. **Authentication** — no token, malformed token, expired token
4. **Authorization & tenant isolation** — a *valid* token with the wrong role, another user's resource, another tenant's data. The most-skipped category and the one that leaks customer data when it breaks. Cross-tenant reads should come back 404, not 403 — a 403 confirms the id exists.
5. **Input validation** — missing required fields, wrong types, malformed JSON, wrong or absent `Content-Type`
6. **Boundary values** — 0, negative, empty string, whitespace-only, max length and max+1, empty collection, very large collection, unicode / RTL text if the app is localized
7. **Not found & referential integrity** — unknown id, an id that exists but belongs elsewhere, a reference to a deleted or archived row
8. **Conflict & uniqueness** — create the same thing twice, and try the case-insensitive / trimmed-whitespace variant of an existing value
9. **Idempotency & replay** — fire the identical request twice. Does it double-write? Does the second call return the same body, or a confusing 500 from a constraint violation?
10. **State machine & ordering** — an invalid transition, an action out of order, an action on an already-terminal resource
11. **Concurrency** — two identical requests genuinely in flight at once. Exactly one should win and the DB should show one row, not two.
12. **Error contract** — every error response carries the project's standard envelope (code, requestId, field, correlation header). Make this one case whose evidence cites the error responses the other cases already produced — one claim, one verdict, no new requests.
13. **Pagination, filtering & sorting** — only if the change returns a collection: page past the end, `size=0`, size over the configured max, and a sort on a field that isn't whitelisted (which must be rejected, not passed into SQL)
14. **Caching & conditional requests** — only if the change touches a cacheable read: ETag, `If-None-Match` → 304 with an empty body, `Last-Modified`
15. **Neighbour regression** — one call to the nearest endpoint you did *not* change that shares the modified code path, proving the change didn't break it

`references/scenario-catalog.md` has the concrete recipe for the categories that are awkward to test over HTTP — concurrency, idempotency, tenant isolation, sort injection, migration-reaches-existing-rows. Read it when you get to those rows; there's no need to reinvent the shell pattern each run.

#### 5c — One case, one claim

**One case makes one claim.** It's tempting to fold five assertions into a single numbered case to keep the list tidy, but then a failure tells the reader only that "case 3 failed" and they have to read the whole card to find out which of the five claims broke — and the pass/fail count over-reports coverage, because eight bundled cases look identical to eight real ones. Split them. Cheap setup (a login, a seed row) can be shared across cases; the *claim* can't.

**Negative cases must prove the non-change.** A 400 is only half the evidence — the DB-after query showing zero rows written is the other half. Otherwise you've proven the API said no, not that it did nothing.

**Every case states its expected outcome** — status, error code, DB state — before anything runs. The worker compares against that line; a case without an expectation can't fail, which means it can't pass either.

#### 5d — Group the cases into areas

Six cases are a list. Eighty in one flat list are a wall, and a reader who can't find the part they came for reads none of it. Decide the grouping here, while the diff is still in front of you — not afterwards, when the numbers are already fixed and regrouping means renumbering.

An **area** is one coherent slice of the change: the error envelope, signup validation, the editor endpoints, the bundle endpoint plus tenant isolation. Three to six areas is the useful range; ten areas is a second flat list wearing a hat.

Three rules make an area worth having:

- **Case numbers stay contiguous.** An area is one unbroken range — 1–16, 17–36, 37–61 — never "1–4, 9, 22". Number the cases in area order and the ranges fall out on their own. Contiguity is what lets the report state a range instead of listing twenty numbers, and what lets a reader jump to an area and trust that everything under the heading belongs to it.
- **Cases go from common to rare.** Put the happy path first. Then put the usual failures: bad input, no token, the wrong tenant. Put the rare cases last: concurrency, replay, boundary limits, and the cases you wrote because one line of the diff looked odd. A reader who stops halfway has still read the cases that carry the most weight, and a reviewer sees at once whether you tested the basic path at all. Order the areas the same way — the area that holds the main change goes first, the far corners go last. Number the cases after you put them in this order, not before.
- **Titles are short and plain.** Three to five words in ASD-STE100 Simplified Technical English (the user's global rule), naming the slice and not the verdict: "Signup validation", "Bundle endpoint and tenant isolation". A title you can't say in five words is usually two areas.

**Below roughly 10 cases, don't group.** Grouping eight cases costs the reader a `## Navigation` section, a set of sub-headings and a set of `## Details —` splits to organize eight cards they could have read in one pass — ceremony charged against a report nobody needed help with. Ten is guidance, not a gate: eight cases that fall into two obvious halves can be grouped, and fourteen cases that are all one endpoint and one validator should stay flat. The honest test is whether you can name the areas in four words each without straining. If you can't, the run doesn't have areas — leave it flat, and the renderer will leave it flat too. The common-to-rare order still applies to a flat list: happy path first, the rare cases last.

**If the run is split across parallel worker agents, the areas are already decided.** Each worker owns a case-number range, so each range *is* an area: hand the worker its area title along with its range, and have it number only inside that range. The merge step then preserves the grouping for free — no renumbering across workers, which would break the one property the report depends on.

#### 5e — Tier, cap, and assign

The full list is the honest size of the change. In practice a one-line bug fix lands around 5–8 cases and a new endpoint with a state machine and tenant scoping lands around 15–25. If you've written fewer than 8 for anything bigger than a typo fix, go back to 5b — you skipped rows. The `depth` knob then decides how many run *this time*; the rest are recorded, not forgotten.

**Tier every case:**

- **P1** — happy path; persistence & side effects; authorization & tenant isolation; conflict/uniqueness and concurrency on write endpoints; neighbour regression; and any case that covers a `throw` added by the diff. These are the cases that find data leaks and double-writes.
- **P2** — everything else: the individual validation and boundary variants, malformed JSON, wrong content type, expired token, pagination edges, ETag.

**Apply the cap** (`max`, else `smoke` = P1 only ≤ 8, `standard` ≤ 20, `thorough` = unlimited): keep every P1, then P2 in checklist order until the cap. Never cut a P1 to make room for a P2. If P1 alone exceeds a `smoke` cap, keep the eight with the highest blast radius (isolation, persistence, conflict, neighbour first) and say so.

Cut cases go under a `## Not run` heading in the skeleton, numbered continuously after the last running case, each with `— over cap (depth=standard)`. In the coverage table, a row whose only cases were cut reads `not run (over cap): 21, 22`. That's how a later `only=21,22` run knows what to pick up, and how the reader knows the green ring covers 20 of 27, not 20 of 20.

**Assign workers** by what a case touches, not by number:

- **A — read-only & negative:** authentication, authorization/isolation reads, validation, boundaries, not-found, protocol, error contract. Writes nothing, so it can run beside anything.
- **B — writes:** happy path, persistence, conflict, idempotency, state machine, neighbour regression. Sequential inside the worker because each depends on the state the previous one left.
- **C — concurrency:** runs alone, after A and B return.

With `workers=1`, one worker runs A's cases, then B's, then C's. With `workers=3` or `4`, split B (and then A) by resource or flow, never by interleaving numbers. Throttle and lockout cases go last in their worker and use a second user when one exists — otherwise they lock the account every other worker logs in with.

Write the skeleton (Step 7) with the `## Test cases` list, the `## Not run` list, and the filled `## Scenario coverage` table. Don't stop to ask the user for approval — the size was already stated in Step 1, and the coverage table is what surfaces the choices for review afterwards.

### Step 6 — Fire requests and capture everything

For each test case:

1. **Query the DB before** the request (capture the relevant rows)
2. **Fire the curl** and capture full request + response
3. **Query the DB after** the request
4. **Compare** before/after and the response against the case's expected outcome (or expected non-change for negative cases)

Curl pattern — the request you paste into the report stays simple and copy-pasteable (`-i -s`, headers and body together):

```bash
curl -i -s -X POST http://localhost:8080/api/orders \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"productId": 42, "quantity": 2}'
```

...but when you actually run it, split the headers from the body so you can pretty-print the payload. `-i` glues them into one stream, and a JSON body on a single 900-character line is the main reason these reports are painful to read:

```bash
curl -sS -D /tmp/h.txt -o /tmp/b.json -X POST http://localhost:8080/api/orders \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"productId": 42, "quantity": 2}'
cat /tmp/h.txt; echo; jq . /tmp/b.json 2>/dev/null || cat /tmp/b.json
```

**Worker rules** (these matter only because several workers share one service and one DB):

- Use `/tmp/<worker>-h.txt` style scratch paths and the identifier prefix from your brief for every row you create. Two workers writing `/tmp/h.txt` corrupt each other's evidence.
- If a request that should be authenticated returns 401, re-run the login recipe once and retry before recording a verdict; note the re-login in the case. Sessions expire mid-run.
- Record the verdict against the expectation in your brief. A different-but-defensible outcome is still a FAIL with the observed value in the result line — the reader decides, not the worker.
- Never re-plan. A case that can't run as written gets `**Result:** SKIP — <reason>`, not a substitute case.

**Formatting rules for anything that lands in the report:**

- **Pretty-print every JSON body.** Indented, one field per line — request bodies too, if they're more than a couple of fields. A reader scanning for `errorCode` should find it on its own line, not buried mid-line.
- **Never truncate a body with `...`.** The whole point of the artifact is that someone can check a claim against the actual bytes; an elided body can't be checked. The HTML renderer collapses long blocks for you, so length costs the reader nothing. (One narrow exception: a response whose body is a genuinely unbounded dump — an i18n bundle with 400 keys, a full table export. Show the first ~40 lines, then a line saying exactly how many entries were omitted and why, so the reader knows it was a deliberate call and not a lost body.)
- **Tag your fenced blocks** — ` ```bash `, ` ```http `, ` ```json `, ` ```sql `. The renderer keys off these: `bash` gets a copy button, `http` gets a colour-coded status line, `json` and `sql` get syntax colouring. Raw psql output is the one thing to leave untagged — it isn't a language, and a plain block renders it fine.
- **`http` blocks are status line + headers, one blank line, then the body.** That blank line is what tells the renderer where the payload starts. A bodiless response (304, 204) is just the status line and headers with nothing after them — that's a complete block, not a truncated one, and it renders correctly.
- **When the case *is* a repetition** — six login attempts to trip a throttle, a poll until a job finishes — don't paste six near-identical curls. Show the one representative invocation as the request, and put the per-attempt outcomes (`attempt 1 → 401`, … `attempt 6 → 429`) in a plain untagged block beside it. The loop is the setup; the response you're making a claim about is the case.
- **Show the DB command, not just its output.** `**DB before:**` followed by a bare table of rows proves the state but not how to check it again. Put the `docker exec … psql -c "…"` in a `bash` block, then the output in a plain block under it. A reader who can't re-run your query can't extend your test.

The renderer reads the same blocks to build the dashboard: it pulls method and path from the **last** curl in each `bash` block (setup calls like fetching a CSRF token come first, the request under test comes last — write them in that order), the status code from the `http` block, and any `"code"` / `"errorCode"` field from the response body. Nothing is invented; a case with no curl simply shows "no request captured". So the tidier the blocks, the richer the dashboard — but a sloppy report still renders, it just tells the reader less.

DB query pattern (Postgres in docker-compose):

```bash
docker exec -i <db-container> psql -U <user> -d <db> \
  -c "SELECT id, status, total, updated_at FROM orders WHERE id = 42;"
```

The before/after pair is what proves the change actually persisted (or correctly rejected the write). Don't skip it — that verification *is* the value of this skill over hand-running a curl.

**Do not delete or clean up test data after the run.** Leave every row, record, and side-effect created by the test in place. Reasons:

- The user (or a teammate) may want to inspect the persisted state directly after the run to corroborate the report.
- Follow-up debugging often needs the exact rows the test produced; deleting them destroys evidence.
- Idempotency / uniqueness collisions on a re-run are a *signal* worth surfacing in the report, not something to paper over by wiping data first.

This means: no `DELETE` / `TRUNCATE` statements, no "cleanup" curl calls, no `docker compose down -v`, no resetting sequences. If a test case requires a clean slate (e.g., a uniqueness constraint), pick a fresh identifier (new email, new external ref, UUID) instead of deleting prior data. If that's not possible, mark the case SKIP with the reason.

### Step 7 — Write the report

The run produces two files from one source: Markdown is written (skeleton by the planner, cases by the workers, stitched by the assembler), and a bundled script renders the HTML. Markdown is what gets committed, diffed, and grepped; HTML is what a human actually reads when the run has twenty cases and a few hundred lines of JSON. One source means they can't drift.

**Planner** writes the skeleton to `docs/test-runs/<YYYY-MM-DD-HHMM>-<feature-slug>.md` and creates `docs/test-runs/<YYYY-MM-DD-HHMM>-<feature-slug>/parts/`. Create `docs/test-runs/` if it doesn't exist (the user's global rule keeps all docs under `docs/`).

**Workers** write `### Test N:` sections only, into `parts/<worker>.md`.

**Assembler** (`python3 <skill-dir>/scripts/assemble_report.py docs/test-runs/<report>.md`) replaces `## Details` with the parts in case order, fills each line of `## Test cases` with its verdict (a case no part covered becomes `SKIP — not run`), writes `## Summary`, then calls the renderer. Run it again after an `only=` rerun and it merges the new parts over the old cases.

Skeleton structure:

```markdown
# Test Run: <feature> — <YYYY-MM-DD HH:MM>

## What changed
<2-3 sentence summary>

- Spec / plan: <link to docs/<feature>-plan.md if one exists, otherwise "none">
- Files touched: <list from git diff>
- Branch: <name>

## Setup
- Service: <how it was started, e.g., `docker compose up -d`>
- Base URL: `http://localhost:8080`
- Auth: <one line, e.g., `POST /api/auth/login` with `{email, password}`, returns `access_token`>
- DB: <type + how queries were run>
- Run: depth=standard (cap 20), workers=2 (sonnet), planner=inherit

## Navigation
<Below ~10 cases, drop this section, drop the sub-headings under `## Test cases`,
and use a single flat `## Details`. See Step 5d.>

The <N> cases are in <k> areas. Each area is one unbroken range of case numbers.

- **<Area one>** — cases 1–16 (16 cases, 16 PASS / 0 FAIL)
- **<Area two>** — cases 17–36 (20 cases, 19 PASS / 1 FAIL: 35)
- **<Area three>** — cases 37–61 (25 cases, 22 PASS / 3 FAIL: 39, 57, 59)

## Test cases

### <Area one>
1. **Happy path: create order with valid input** — expected 201, row with status CREATED
2. **Auth failure: missing token** — expected 401, nothing written
3. **Validation: negative quantity** — expected 400, nothing written

### <Area two>
17. **...** — expected 200, page 2 holds the remaining 3 rows

## Not run
21. **Boundary: quantity at max+1** — over cap (depth=standard)
22. **Protocol: text/plain content type** — over cap (depth=standard)

## Scenario coverage
| Category | Cases | Notes |
| --- | --- | --- |
| Happy path | 1 | |
| Persistence & side effects | 1, 9 | order row + `orders.created` event |
| Authentication | 2 | |
| Authorization & tenant isolation | 6, 7 | tenant B gets 404, not 403 |
| Input validation | 3, 4 | |
| Boundary values | 4, 5 | quantity 0 / -1; not run (over cap): 21 |
| Not found & referential integrity | 8 | |
| Conflict & uniqueness | 10 | |
| Idempotency & replay | 11 | |
| State machine & ordering | N/A | create-only endpoint, no states |
| Concurrency | 12 | 5 parallel creates, 1 row expected |
| Error contract | 13 | cites the bodies of 2, 3, 6, 8 |
| Pagination, filtering & sorting | N/A | returns a single object |
| Caching & conditional requests | N/A | POST only, nothing cacheable |
| Neighbour regression | 14 | `GET /api/orders` still returns 200 |

## Details — <Area one>

<One line: the case range, the split, and what this area covers.>

## Summary
```

Case section format (what a worker writes into its part file):

```markdown
### Test 1: Happy path — create order with valid input
**Goal:** verify a valid POST creates an order row with status `CREATED`.
**Expected:** 201, one new row, status `CREATED`.

**DB before:**
\`\`\`bash
docker exec -i pg psql -U app -d app -c "SELECT id, status FROM orders WHERE customer_id = 7;"
\`\`\`
\`\`\`
 id | status
----+--------
(0 rows)
\`\`\`

**Request:**
\`\`\`bash
curl -i -s -X POST http://localhost:8080/api/orders \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"customerId": 7, "productId": 42, "quantity": 2}'
\`\`\`

**Response:**
\`\`\`http
HTTP/1.1 201 Created
Content-Type: application/json
Location: /api/orders/123

{
  "id": 123,
  "status": "CREATED",
  "total": 199.98
}
\`\`\`

**DB after:**
\`\`\`bash
docker exec -i pg psql -U app -d app -c "SELECT id, status, total FROM orders WHERE customer_id = 7;"
\`\`\`
\`\`\`
 id  | status  | total
-----+---------+--------
 123 | CREATED | 199.98
\`\`\`

**Result:** PASS — order persisted with the expected status and total.
```

Five things matter most about the report:

- **The curls are runnable.** Anyone reading should be able to copy-paste them (with the right env vars) and reproduce the result. That's the artifact's whole value.
- **The DB output is real.** Don't summarize it ("the row was created") — paste the actual psql output. That's the proof.
- **Every case ends with a `**Result:** PASS` / `FAIL` / `SKIP` line.** This is both the honest verdict and the hook the renderer reads to colour the case and build the pass/fail counts at the top. A case with no `**Result:**` line silently drops out of the tally.
- **The coverage table is filled in, N/A and not-run rows included.** The pass count says how many claims held; the coverage table says how many claims were made and how many were deferred. A reader needs all three to know how much the green ring is worth, and an N/A with a reason is the only way a genuine gap is distinguishable from a forgotten one.
- **The grouping is all three places or none of them.** If the run has areas (Step 5d), it has a `## Navigation` list, sub-headings under `## Test cases`, and one `## Details — <area>` section per area — and the ranges agree across all three. Half a grouping is worse than none: the renderer reads the `## Details — <area>` headings, so a report that groups the checklist but keeps one flat `## Details` gets no areas in the HTML and the reader is told about ranges the page can't show. Below ~10 cases, none of the three: plain `## Details`, no `## Navigation`, no sub-headings.

#### Render the HTML

The assembler renders automatically. To re-render by hand:

```bash
python3 <skill-dir>/scripts/render_report.py docs/test-runs/<report>.md
```

It prints the path it wrote and needs nothing but Python 3 — no pip install, no network, one self-contained file.

The HTML is deliberately not a copy of the Markdown in a nicer font. It's a dashboard built from the same facts, and everything on it is read off the report — nothing is invented, so a case that showed no curl says so instead of guessing an endpoint.

What the reader gets:

- **A one-line headline** that says how the run went — "All 18 cases passed." or "2 of 18 cases failed — start with case 7.", with every failing number linked to its card.
- **A pass-rate ring**, counts of cases / endpoints / DB checks, and a bar chart of every status code the run saw.
- **An Areas panel**, on a grouped report only: one row per area with its case range, its case count, a pass/fail bar and its failing numbers, each linked. It is counted from the cases themselves, so it cannot drift from the Markdown — which is why the page then drops your `## Navigation` list and the lead paragraph under each `## Details — <area>` rather than printing the same facts twice. A report with no areas gets no panel and loses nothing.
- **An endpoint coverage table** — which paths were hit, which statuses each returned — where **every row links to the cases behind it**. The case numbers are real links to the cards (`#case-7`), sorted numerically, so "which case saw that 409?" is a click instead of a text search. A case whose curl didn't parse still gets a row, labelled "no request captured", and is still linked.
- **A hint on every one of those numbers.** Hovering or tabbing to a case number shows the case's number, title, verdict, HTTP status, error code and — on a grouped report — its area, before the click. The same sentence is on the link as an `aria-label`, so a keyboard or screen-reader user gets the identical facts. Fields the case never showed are simply absent from the hint.
- **One card per case.** Inside a card the `**Goal:**` / `**DB before:**` / `**Request:**` / `**Response:**` / `**DB after:**` / `**Result:**` labels become a numbered step rail, and each `http` block shows its status as a chip with the headers folded so the body is read first — so keep using those labels exactly. Failures are pre-opened.
- **Area dividers in the case list**, on a grouped report: each area opens with its title, its range and its split, and carries its own lead paragraph. Turning on "Only failures" hides a divider whose cases have all just been hidden.
- **A sticky bar** with Expand all / Collapse all / Only failures / Jump-to-case that follows the reader down the list. The jump list is grouped by area when there are areas, and a plain list when there aren't.

The `## Test cases` checklist is dropped from the HTML because the cards and the dashboard already say it.

If it errors, fix the Markdown rather than hand-writing HTML — an error almost always means a malformed fence or a missing blank line between headers and body inside an `http` block, and that same malformed block is what makes the Markdown hard to read too.

Don't commit either file unless the user asks.

### Step 8 — Tell the user

This step runs back in the main session, after the assembler finishes. Relay: where the report is, pass/fail/skip counts, how many categories were covered vs N/A vs not run, and any specific issues that warrant a follow-up. Surface failures explicitly — don't bury a failed test in a "mostly worked" summary.

Relay coverage gaps too. "12/12 passed" reads very differently once the user knows tenant isolation went untested because there was only one tenant, or that seven boundary cases sit under `## Not run`. One line each, plus the offer: "`only=21,22` runs the deferred ones" or "`depth=thorough` next time".

Give the user the HTML, not just its path — send the `.html` file so it opens for them (`SendUserFile` with `display: "render"`, or whatever the host offers). Mention the `.md` path alongside it for anyone who'd rather read it in the repo. A rendered report the user has to go find themselves usually doesn't get read.

Do not re-read the report file to "verify" it before relaying — trust the assembler's output and the workers' structured returns. If the user asks for details beyond the summary, then open the report.

## When to stop and ask

- The diff is empty or unrelated to what the user said they're testing.
- Auth is non-trivial (real OAuth provider, MFA) with no obvious test-mode bypass — ask how to get a token.
- A test reveals a clear bug introduced by the change — call it out instead of just marking FAIL and moving on.
- Starting the service would do something destructive (drop volumes, wipe seed data) — confirm first.

Otherwise, run end-to-end without checking in mid-way. The user invoked the skill because they want a report at the end, not a conversation in the middle.

## Notes on the memory step

The memory you save in Step 2 is a gift to your future self running this skill on the same project tomorrow. Save:

1. A `reference` type memory file describing the backend's setup as it stands today
2. A one-line pointer in `MEMORY.md`

What to include:
- Startup command (`docker compose up -d` or build-tool command)
- Base URL + port
- Auth: endpoint, body shape, response field for the token, header name on subsequent requests
- DB: type, container name, user, database name, query command pattern
- Test credentials if discovered from seed data (don't memorize secrets the user typed in chat), including a second user or tenant for isolation cases
- A `## Test-run defaults` block if the user has expressed a preference (`depth`, `workers`, `model`)

What not to include:
- Current branch, current PR, today's test data, the specific endpoint you tested this run — those belong in the report, not in long-lived memory.

If on a later run any cached fact doesn't match the code (file moved, endpoint renamed, port changed in config), update the memory rather than acting on the stale fact. Trust the code over the memory when they conflict.
