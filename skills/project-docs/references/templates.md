# Templates

Skeleton and guidance for every file the skill writes. Headings in `<angle brackets>` are placeholders;
sections marked *(if …)* appear only when the project has that content. Never keep a section just to
match the template — an empty or generic section is worse than none.

## Contents

- [AGENTS.md](#agentsmd)
- [CLAUDE.md](#claudemd)
- [CONTEXT.md](#contextmd)
- [docs/README.md](#docsreadmemd)
- [docs/architecture.md](#docsarchitecturemd)
- [docs/standards.md](#docsstandardsmd)
- [docs/testing.md](#docstestingmd)
- [docs/development.md](#docsdevelopmentmd)
- [Topic docs](#topic-docs)
- [docs/known-issues.md](#docsknown-issuesmd)
- [docs/features/\<feature\>.md](#docsfeaturesfeaturemd)
- [Finding invariants](#finding-invariants)

---

## AGENTS.md

The short version. A newcomer reads this first and should be able to make a safe first change with it
plus one linked doc. Target 80–160 lines.

```markdown
# <Project name>

<2–4 sentences: what the system does, for whom, how it runs (service, batch, CLI, app), the stack
with major versions, and the systems it talks to.>

Domain terms are defined in [CONTEXT.md](CONTEXT.md); use them in code, docs and commits.

## Where things are

| Folder | Holds |
| --- | --- |
| `<real path>/` | <what lives there, naming key classes> |

| Read it before you | Doc |
| --- | --- |
| change how anything is wired or owned | [architecture.md](docs/architecture.md) |
| write code — naming, style, errors, persistence | [standards.md](docs/standards.md) |
| claim a change is done | [testing.md](docs/testing.md) |
| build, run or configure locally | [development.md](docs/development.md) |
| touch one feature's internals | [features/](docs/features/) — each opens with its invariants |
| <one row per topic doc> | <link> |

## Non-negotiables

<Project-wide rules whose breach causes a bug, data loss, a security hole or an outage. 4–10 items.
Each: a bold one-line rule, one or two sentences of why or what enforces it, a link for detail.
Feature-specific rules belong in that feature's ## Invariants, not here.>

- **<Rule.>** <Why / what enforces it.> See [<doc>](docs/<doc>.md#<heading>).

## Conventions worth knowing up front

<3–8 conventions a newcomer would otherwise get wrong on their first change: where new code goes,
naming suffixes, how config is added, how migrations are added, commit style.>

## Before you finish

Each item is explained in [testing.md](docs/testing.md#definition-of-done).

- `<real test command>` passes.
- <other real checks: build, lint, format>
- Any doc your change made wrong is fixed in the same commit.
```

**Where the rows come from:** "Where things are" lists the top-level source folders (or packages)
someone navigates, not every directory. "Non-negotiables" come from the strongest cross-feature
invariants you found — see [Finding invariants](#finding-invariants).

---

## CLAUDE.md

Exactly:

```markdown
@AGENTS.md
```

If a `CLAUDE.md` with content existed and the user approved the plan, its content moved into
`AGENTS.md` first.

---

## CONTEXT.md

Follows the domain-modeling `CONTEXT.md` format so other skills can read it.

```markdown
# <Project or bounded-context name>

<One or two sentences: what this context is and why it exists.>

## Language

### <Cluster, e.g. Invoicing>

**<Term>**:
<One or two sentences: what it IS, not what it does.> Code: `<ClassOrEnum>`.
_Avoid_: <synonyms that name the same thing>

**<Status or enum value set>**:
<What the states mean.> Values: `DRAFT`, `SUBMITTED`, `CLEARED`.
```

Rules:

- **Only project-specific terms.** Entities, value objects, statuses, roles, partner systems, domain
  acronyms (`VAT`, `ZATCA`, `IBAN`, …). General programming concepts never belong.
- **Tie each term to code** with the class, enum or table that holds it, so a reader can jump there.
- **`_Avoid_` only lists real confusables** — names the code actually uses for the same concept in
  different places, or an obvious wrong word. Omit the line when there are none.
- **Group under subheadings** when natural clusters appear; a flat list is fine for a small domain.
- If the repo clearly contains several bounded contexts (separate modules with separate models),
  write a root `CONTEXT-MAP.md` plus one `CONTEXT.md` per context instead.

---

## docs/README.md

```markdown
# <Project> documentation

Start with [`AGENTS.md`](../AGENTS.md) at the repo root — it is the short version, and it links here
for anything that needs more than a line. Domain terms are in [`CONTEXT.md`](../CONTEXT.md).

Each document below has one job and one trigger: the change that obliges you to edit it. A document
that contradicts the code is a defect, so fix it in the commit that made it wrong.

| Document | Covers | Edit it when |
| --- | --- | --- |
| [architecture.md](architecture.md) | <layers, owners, flows, topology, tree> | a layer boundary, an owner, or the tree changes |
| [standards.md](standards.md) | <naming, style, errors, …> | a convention changes, or a check is added |
| [testing.md](testing.md) | <definition of done, suites, manual checks> | a suite moves, or the bar changes |
| [development.md](development.md) | <setup, build, run, config> | the local toolchain changes |
| <topic docs> | | |
| [known-issues.md](known-issues.md) | Suspected defects found in the code, with how to confirm each | an issue is confirmed, fixed or ruled out |

## Features

One document per feature, covering its invariants and internals. Every one opens with an
`## Invariants` section; read it before changing anything in that area.

[<feature>](features/<feature>.md) · [<feature>](features/<feature>.md) · …

## Other documents *(if the repo had plans, specs, ADRs the user kept)*
```

The "Edit it when" column is the most important one: make each trigger a concrete change in the code,
not "when needed".

---

## docs/architecture.md

How the system is wired. Opens with one line of scope and links to `standards.md` and the features.

Sections:

1. **The layering** — an ASCII box diagram (it renders in any viewer and in a terminal) of the layers
   the code actually has, listing real class names in each layer, then one paragraph per layer: what it
   may and may not depend on, and what decides versus what does. If the code does not respect a
   layering, say where it leaks.
2. **Ownership and wiring** — the composition root (Spring `@Configuration`, `main`, DI module, app
   bootstrap), who owns long-lived state (caches, clients, schedulers, pools), and the rule for where a
   new one goes.
3. **Request lifecycle** — one real, typical request or message traced end to end through real
   classes: entry point → validation → service → persistence/integration → response/event. Numbered
   steps or a sequence diagram. Add a second trace when there is a second kind of entry point (a
   consumer, a scheduler).
4. **Runtime topology** — the processes, databases, queues, caches and external systems, and which
   direction each call goes. An ASCII diagram or table.
5. **Cross-cutting concerns** *(if the code has them)* — transactions, error translation (exception
   handlers, error codes), security filters, logging/correlation IDs, caching, retries.
6. **The folder tree** — an annotated tree of the source folders, two or three levels deep, one
   comment per line.

---

## docs/standards.md

How code here is written. Opens with: this is guidance that describes what the codebase already does;
what is actually checked is in `testing.md#definition-of-done`; when this doc and the code disagree,
the code is probably right and this doc is stale.

Sections (only those with real content):

- **Posture** — language and framework versions, and the style choices that follow (records vs Lombok,
  `Optional`, reactive vs blocking, constructor injection …), with the reason when you can find it.
- **Organization** — the rules a new feature must satisfy: where its files go, how packages split.
- **Naming** — a table of the type suffixes the code actually uses (`Controller`, `Service`,
  `Repository`, `Client`, `Mapper`, `Request`, `Response`, `Dto`, `Entity`, `Job`, `Listener`…) and
  the responsibility each names, with a real example. Note inconsistencies honestly.
- **Errors** — how failures are raised, translated and returned (exception types, handlers, error
  bodies, codes).
- **Persistence** — entity conventions, IDs, auditing columns, migration naming, native vs derived
  queries, transaction placement.
- **API** — path and versioning scheme, DTO shape, validation annotations, response envelope.
- **Logging** — logger, levels, what is logged and what must never be (PII, secrets).
- **Concurrency** *(if relevant)* — async executors, locks, scheduled jobs overlapping.
- **Comments** — the style the code uses.
- **What is actually checked** — one line linking `testing.md#definition-of-done`.

---

## docs/testing.md

How to check that a change holds up.

1. **Definition of done** — a `Check | Command` table of the mechanical bar: tests, build, lint/format,
   any purity or architecture check, and "docs still true". Only commands that exist in the repo. If
   there is no CI, say that every item is run locally. If there are no tests at all, say it plainly and
   make the bar what exists (build, manual checks).
2. **The suites** — framework, where tests live, what each kind covers (unit, slice, integration,
   contract), what they need (Testcontainers, embedded DB, mocks), how to run all and how to run one.
3. **What to run when** *(if suites are separable)* — a `Change | Run` table.
4. **Test data and fixtures** — builders, SQL fixtures, JSON samples, test profiles.
5. **Manual verification** — how to exercise the running service (curl with a real endpoint, Postman
   collection, UI path) for the main flows.
6. **Coverage gaps** — areas with no tests, stated as fact without judgement, so a reader knows where
   the safety net is missing.

---

## docs/development.md

The local loop. Opens with one line of scope and links to `release.md` and `testing.md` if they exist.

1. **Requirements** — toolchain with versions (JDK, Node, Maven/Gradle wrapper, Docker, DB).
2. **First-time setup** — the steps that are needed once: certificates, private registries, local DB.
3. **Build & run** — the exact commands, with the profile or env needed, and the port it listens on.
4. **Configuration** — profiles and the env vars/keys a developer must set. Name keys only; **never
   copy a secret value** (password, token, key, connection string with credentials) into a doc.
5. **Local dependencies** — database, queues, stub servers, docker-compose.
6. **Tooling** *(if present)* — formatter, linter, IDE settings, generated code and how to regenerate.

---

## Topic docs

Same shape as the core docs: one-line scope and links at the top, sections of real content, a
trigger in the index. Suggested contents:

- **release.md / deployment.md** — how a build reaches an environment: Docker images (which
  Dockerfile for which environment), CI/CD pipelines, versioning, environments, rollbacks.
- **configuration.md** — a table of keys: `Key | Default | Used by | Meaning`, grouped by feature;
  profiles and precedence.
- **security.md** — authentication, authorization (roles and where checked), secrets handling,
  certificates and keystores (paths and purpose, never contents).
- **data.md** — the schema by table cluster, migration tool and conventions, shared tables and who
  writes them, retention.
- **integrations.md** — a `System | Direction | Protocol | Client | Config keys | On failure` table,
  then a section per system with the non-obvious parts (auth, signing, retries, timeouts, sandbox).
- **messaging.md** — topics/queues, producers, consumers, payload types, ordering and retry rules.
- **api.md** — versioning, envelope, error codes, pagination, idempotency keys.
- **ui.md** — tokens, shared components, layout rules.

---

## docs/known-issues.md

Written only when you found suspected defects. It is a triage list, not a verdict: every entry says
what the code does, why it may be wrong, and how to confirm it.

```markdown
# Known issues

Suspected defects found while these docs were written. None is confirmed until its status says so.
When an issue is confirmed, fixed or ruled out, update or delete its entry in the same commit, and
remove its ID from the feature doc that lists it.

| ID | Area | Issue | Severity | Status |
| --- | --- | --- | --- | --- |
| KI-1 | [<feature>](features/<feature>.md) | <one line> | High | Suspected |

## KI-1: <short title>

- **Code:** `<Class.method>`, `<file or query>`
- **What happens:** <what the code does today, as fact>
- **Why it may be wrong:** <the consequence — double charge, stuck record, leaked secret>
- **How to confirm:** <a SQL query, a test, a log search, or the question for the business>
```

Rules:

- **Severity:** High = money, data loss or corruption, security, or an outage; Medium = a wrong
  result a user can see; Low = everything else. Order the table by severity.
- **Status:** `Suspected` for everything you write; the team moves it to `Confirmed`.
- **Secrets committed to the repo** are an entry: name the file and the key, never the value.
- **Not an issue:** a business question with no sign of a defect (goes in your report), a style
  inconsistency (goes in `standards.md` as fact), missing tests (goes in `testing.md` as a gap).
- IDs never change once written, so links from feature docs and tickets stay valid.

---

## docs/features/\<feature\>.md

File name: kebab-case domain noun (`payments.md`, `invoice-submission.md`).

```markdown
# <Feature name>

<2–4 sentences in CONTEXT.md terms: what this feature does, for whom, triggered how. Link related
features.>

## Invariants

- **<The rule, in bold, one line.>** <Why it matters or what breaks. Which code enforces it —
  `Class.method`, a DB constraint, a test — or "nothing enforces this; it is a convention".>

## Layout

| File | Role |
| --- | --- |
| `<path relative to the source root>` | <one line> |

## <Content sections — choose what the feature has>
```

Content sections to choose from (use the feature's own words for headings, like tinycast's
"Poll-based capture" or "Text delivery and pasteboard safety", rather than generic ones):

- **Entry points** — `Method Path | Handler | Purpose` for endpoints; consumers, schedulers (with cron).
- **The flow** — how the main operation proceeds, numbered, naming real methods.
- **States** — the status values and allowed transitions (table or ASCII), and who moves each.
- **Persistence** — tables, key columns, constraints, migrations that created them.
- **External calls** — what is called, when, with what timeout, and what happens on failure.
- **Failure and retries** — error paths, compensation, idempotency.
- **Configuration** — the keys that change this feature's behaviour.
- **Tests** — which tests cover it and how to run just them.

When `known-issues.md` has entries for this feature, end the doc with one line, not a section:

```markdown
Suspected issues in this area: KI-1, KI-4 — see [known-issues.md](../known-issues.md).
```

**Layout** lists every file that belongs to the feature across layers, with paths short enough to scan
(relative to the source root, e.g. `payment/fees/FeeCalculator.java`). State once at the top of the
table which root the paths are relative to if it is not obvious.

---

## Finding invariants

An invariant is a rule that must stay true for the feature to be correct. Breaking it causes a bug,
lost money, duplicate processing, a security hole or corrupted data. Mine them from:

- **Guards and validation** — `if (…) throw`, `@Valid` constraints, `Preconditions`, assertions.
- **Transactions** — `@Transactional` boundaries, propagation, what must commit together.
- **Uniqueness and constraints** — unique indexes and foreign keys in migrations; `unique = true`.
- **Idempotency** — dedup keys, "already processed" checks, upserts.
- **State machines** — which transitions are allowed and where they are checked.
- **Ordering and concurrency** — locks, `SELECT … FOR UPDATE`, optimistic `@Version`, single-thread
  schedulers, `ShedLock`.
- **Money and numbers** — `BigDecimal` scale and rounding mode, currency handling, fee formulas.
- **Security** — authorization checks, tenant/owner filters, signature verification, masking.
- **Integration contracts** — required headers, signing, certificate use, timeouts, retry limits.
- **Comments and commit messages** — "must", "never", "do not", "important", "hack", "because".

Write each one as the rule, not the code: "**A payment is debited once per request ID.**" then how
that is enforced. Skip generic truths ("inputs are validated") — an invariant earns its place by being
specific to this feature and surprising to someone who has not read the code.
