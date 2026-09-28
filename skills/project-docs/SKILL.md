---
name: project-docs
description: Build the full documentation set for an existing codebase, modelled on tinycast's docs/ — AGENTS.md (with CLAUDE.md forwarding to it), a CONTEXT.md glossary, docs/ with an index, architecture, standards, testing, development, topic docs, known issues, and one feature doc per feature that opens with its invariants.
argument-hint: "[repo-path]"
disable-model-invocation: true
---

# Project docs

Target repo: `$ARGUMENTS` — when empty, use the current working directory.

Build the documentation an engineer — or a coding agent — needs to change an existing codebase safely
on day one. The reference is tinycast's `docs/` (github.com/abue-ammar/tinycast/tree/main/docs): a
short root `AGENTS.md` that states the rules and points at the docs, an index where every document
has one job and one trigger, topic documents for the cross-cutting concerns, and one document per
feature that opens with the invariants a change must not break.

The docs exist to carry what the code cannot tell a reader in one read: who owns what, how a request
really flows, which rules span files, why a strange choice was made, and what breaks if you ignore it.
Restating what one file already says is noise.

## The output

```
AGENTS.md                 the short version; entry point for humans and agents
CLAUDE.md                 one line: @AGENTS.md
CONTEXT.md                domain glossary — the project's ubiquitous language
docs/
  README.md               index: Document | Covers | Edit it when
  architecture.md         layers, ownership, request lifecycle, runtime topology, folder tree
  standards.md            how code here is written: naming, style, errors, persistence, comments
  testing.md              definition of done, how to run and add tests, manual checks
  development.md          the local loop: requirements, setup, build, run, config, tooling
  <topic>.md              only when the project has the substance — see "Deciding the doc set"
  known-issues.md         suspected defects found while reading the code (only if there are any)
  features/<feature>.md   one per feature; each opens with ## Invariants
```

The root `README.md` stays as it is: it is the user-facing page and an input to read, not a target.

## Principles

These are the reasons behind every step below; when a case is not covered, decide by them.

1. **Grounded, never invented.** Every class, file, command, table, config key, endpoint and status
   value you name must exist in the repo. Read the code before you write about it. When you cannot
   confirm something, leave it out and list it as an open question in your final report. A reader
   trusts a doc more than a guess, so a wrong doc costs more than a missing one — tinycast states it
   as "a document that contradicts the code is a defect".
2. **One job, one trigger.** Each document covers one concern and names the change that obliges
   someone to edit it. That is what keeps the set maintainable after you leave.
3. **Describe the code as it is; keep suspected bugs in one place.** Standards describe what the
   codebase already does, so new code reads like it was always there. Where the code breaks its own
   convention, say so plainly. Reading a whole codebase always turns up suspected defects — retries
   that never fire, secrets in the repo, double sends. They go in `docs/known-issues.md`, each with
   the code involved and how to confirm it, never into a feature doc as fact. A feature doc only
   points at the IDs that touch it. Kept apart, the feature docs stay true when a bug is fixed, and
   the list is one place a team can triage.
4. **Invariants first.** A reader about to change a feature needs the rules that must hold before
   anything else. Each invariant names the code that enforces it, or says that nothing does.
5. **Say it once, link the rest.** `AGENTS.md` states a rule in one line and links to the doc that
   explains it. A fact lives in one document; others link to that document's heading.
6. **Dense technical English.** Short, exact sentences that name real things. Read
   `references/style.md` before writing — it has the rules and good/bad pairs from real docs.

## Workflow

### Step 1 — Check for existing docs, and stop if there are any

Look for `docs/`, `doc/`, `AGENTS.md`, `CLAUDE.md`, `CONTEXT.md`, `CONTEXT-MAP.md`, and any other
Markdown docs beside the root `README.md` (e.g. `ARCHITECTURE.md`, `CONTRIBUTING.md`, ADR folders).
The root `README.md`, `CHANGELOG`, and `LICENSE` do not count — they are inputs.

**If any exist, write nothing yet.** Read them, then show the user a plan and wait for approval:

- a table of each existing file → *keep as is* / *move to X* / *merge into X* / *correct* (quote the
  specific contradiction with the code) / *leave outside the structure*;
- the list of new files you would create, including the feature list;
- any question the plan depends on.

The user may keep docs from other tools (plans, specs, ADRs, a hand-written CLAUDE.md) exactly as they
are, so the decision is theirs. Never delete or overwrite an existing file without that approval.
Files that are not part of this structure (plans, specs, ADRs) stay where they are and get a link from
`docs/README.md` when they help a reader. If an existing `CLAUDE.md` has content, the plan moves it
into `AGENTS.md` and turns `CLAUDE.md` into the forwarder.

If nothing exists, go straight on — no need to ask.

### Step 2 — Survey the project

Start with the inventory script; it prints the stack, the tree with file counts, entry points,
persistence, messaging, clients, config, CI, containers and recent git history in one pass:

```sh
python3 <skill-dir>/scripts/survey.py <repo-root>
```

Run `git status` too. If the working tree has uncommitted changes or deletions, decide which state
the docs describe (usually `HEAD`; read deleted files with `git show HEAD:<path>`), and say so in the
report — a doc for code that is half-deleted on disk misleads either way.

Then read to fill in the facts. What you need, and where it usually lives:

| Fact | Where to look |
| --- | --- |
| Stack and versions | build file (`pom.xml`, `build.gradle`, `package.json`, `go.mod`, `pyproject.toml`) |
| Build, run, test commands | wrapper scripts, `Makefile`, `package.json` scripts, `Dockerfile`, root `README.md`, CI files |
| Configuration | `application*.yml/properties`, profiles, `.env.example`, `@ConfigurationProperties`, `@Value` |
| Layering and ownership | package tree, DI configuration, base classes, the composition root |
| Entry points | controllers, message listeners, schedulers, CLI commands, UI routes |
| Persistence | entities, repositories, migrations (Liquibase/Flyway/SQL), native query files |
| Integrations | HTTP clients (Feign, RestTemplate, WebClient, axios), queues, SDKs, certificates |
| Conventions | type suffixes, DTO mapping, error handling, logging, formatter/lint configs |
| Domain terms | entity and enum names, status values, API paths, comments, commit messages |
| Active areas and history | `git log --oneline -50`, `git log --format='%s' -- <path>` for one feature |

For a repo with more than about 150 source files, fan out: spawn Explore subagents in parallel — for
example one for build/run/test/CI/deploy, one for architecture and conventions, and one per group of
packages to identify features and their invariants. Ask each to return facts with file paths, not
prose, so you stay the one who decides what goes in which doc.

### Step 3 — Decide the doc set and the feature list

**Core docs are always written:** `docs/README.md`, `architecture.md`, `standards.md`, `testing.md`,
`development.md`, plus `AGENTS.md`, `CLAUDE.md`, `CONTEXT.md`.

**Topic docs are written only when the project has the substance** — roughly 30+ lines of real content
that belong in no core doc. Otherwise fold the content into the closest core doc.

| Evidence in the repo | Topic doc |
| --- | --- |
| Dockerfiles, CI/CD pipelines, versioning, environments | `release.md` (or `deployment.md` if nothing is "released") |
| Many config keys, profiles, env vars, per-environment files | `configuration.md` |
| Auth filters, roles, tokens, certificates, keystores, signing | `security.md` |
| Migrations plus tables shared across features | `data.md` |
| Several external systems (clients, queues, partner APIs) | `integrations.md` |
| A frontend with a design system or shared components | `ui.md` |
| Queues/topics with producers and consumers | `messaging.md` |
| Public API conventions (versioning, envelope, error codes) | `api.md` |

**Features** are business capabilities a user or another system would name — Payments, Refunds,
Invoice submission, Beneficiaries — not layers. In a codebase packaged by layer (`controller/`,
`service/`, `repository/`), group the files for one domain noun across layers into one feature. In a
codebase of adapters (`source/x`, `target/y`), one adapter per feature is usually right when each has
its own rules; otherwise one feature for the family with a section per adapter. Merge features too
small to have an invariant; split one that needs more than about 800 lines. Most services land between
4 and 25 features.

Write the list down (feature → files) before writing any doc: architecture, `AGENTS.md` and the index
all depend on it.

### Step 4 — Write

Read `references/templates.md` (skeleton and guidance for every file) and `references/style.md` before
the first doc. Then write in this order, because each step feeds the next:

1. `CONTEXT.md` — fixing the terms first keeps every later doc consistent.
2. `docs/features/*.md` — the bulk of the facts.
3. `docs/architecture.md`, `standards.md`, `testing.md`, `development.md`, topic docs.
4. `docs/known-issues.md` — from the suspected defects you and the subagents collected; then add the
   "Suspected issues in this area" line to each feature doc it touches.
5. `docs/README.md` — the index, now that every doc exists.
6. `AGENTS.md` — the short version of everything, written last so it only summarizes and links.
7. `CLAUDE.md` — the single line `@AGENTS.md`.

With more than about 6 features, write the feature docs in parallel subagents (2–4 features each).
Give each subagent: the paths to `references/templates.md` and `references/style.md`, the feature's
file list, the repo root, and the finished `CONTEXT.md`. Ask it to write the files and to return any
suspected bugs (in the known-issues entry shape from the templates) and unconfirmed facts separately,
so they reach `known-issues.md` and your report instead of the feature docs.

If the toolchain is present, try the definition-of-done commands once (with a timeout) so
`testing.md` lists commands that work. If they cannot run here (missing JDK, private registry,
database), keep the commands the repo documents and say in your report that you did not run them.

### Step 5 — Verify

```sh
python3 <skill-dir>/scripts/check_docs.py <repo-root>
```

It checks that the required files exist, `CLAUDE.md` forwards, every relative link and heading anchor
resolves, every feature doc opens with `## Invariants`, the index lists every doc, and every
backticked path or class name exists somewhere in the repo. Fix every error. Treat each warning as a
possible invention: confirm it in the code or remove it. Expected warnings are library and JDK names
(`NullPointerException`), runtime file names the app creates, and files outside the repo.

Then reread `AGENTS.md` as a newcomer: could someone make a safe first change with only it and the one
doc it points to? If not, the missing fact belongs in `AGENTS.md` or the link is wrong.

### Step 6 — Report

Keep it short:

- the files created (full paths) and the feature list;
- open questions — facts you could not confirm and left out;
- the known issues, highest severity first, one line each, with a link to `docs/known-issues.md`;
- which commands you ran, and which you only copied from the repo.

Do not commit unless the user asks.
