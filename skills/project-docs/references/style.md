# Style

The voice of tinycast's docs: dense technical English that names real things and explains why. A
reader should finish each paragraph knowing something the code did not tell them in one read.

## Rules

1. **Lead with the claim.** The first sentence of a paragraph or bullet is the fact or rule; the
   reasoning follows. In lists of rules, bold the rule.
2. **Name real things in backticks** — classes, methods, files, tables, config keys, commands,
   status values. A name the reader can search for beats a description of it.
3. **Explain why, not what.** The code shows what it does. The doc says why it is shaped that way,
   what breaks if you change it, and what is non-obvious.
4. **Use tables for mappings** — file → role, doc → trigger, command → purpose, key → meaning.
5. **Scope line, then content.** Each doc opens with one sentence of what it covers and links to
   where neighbouring concerns live. No "This document describes…", no "Introduction" section.
6. **Present tense, the code as it is.** Not "should", not "will be". Where the code breaks its own
   convention, say so as fact. A suspected defect is not a fact about the design: it goes in
   `known-issues.md`, not in a "Known defects" section of a feature doc.
7. **No speculation, no marketing.** No "robust", "seamless", "powerful", "leverages". No "probably",
   "it seems", "might be used for". Unknown means omitted and reported.
8. **Relative links**, to headings where possible: `[standards.md](standards.md#naming)`.
9. **Commands in `sh` code blocks**, with a comment when the purpose is not obvious.
10. **Never copy a secret** — passwords, tokens, keys, credentialed URLs, certificate contents. Name
    the key or the file only.
11. **Length follows content.** A small feature is 40 lines; a central one can be 600. Do not pad a
    thin feature and do not trim a rich one to look even.
12. **Diagrams as ASCII in code fences** for layers and topology — they render in every viewer and in
    a terminal. A Mermaid sequence diagram is fine for a long flow if the repo host renders it.

## Good and bad

**Scope line**

Bad:
> This document provides an overview of the testing strategy used in the project.

Good:
> How to check that a change holds up. The automated half is the JUnit suite; the manual half is the
> curl sweep at the bottom of this file.

**An invariant**

Bad:
> - Payments should be handled carefully to avoid duplicates.

Good:
> - **A payment is debited once per `requestId`.** `PaymentService.pay` looks the ID up in
>   `payment_request` before debiting, and the column has a unique index
>   (`DDL/005-payment-request.xml`), so a retried call returns the first result instead of charging
>   twice.

**Explaining why**

Bad:
> The `FeeCalculator` class calculates fees using `BigDecimal`.

Good:
> Fees are computed in `BigDecimal` with `HALF_UP` at scale 2, because the ledger rejects a balance
> with more than two decimals — a `double` fee once left a wallet one halala short
> (commit `a1b2c3d`).

**Honest about the code**

Bad:
> All services use constructor injection.

Good (when two services do not):
> Services use constructor injection via Lombok's `@RequiredArgsConstructor`. `LegacyTopupService` and
> `SmsSender` still use field `@Autowired`; new code follows the constructor style.

**A layout row**

Bad:
> | `PaymentController.java` | The payment controller |

Good:
> | `payment/PaymentController.java` | `POST /api/v1/payments`, `GET /api/v1/payments/{id}`; validation only |

**AGENTS.md non-negotiable**

Bad:
> - Follow best practices for security.

Good:
> - **Every wallet query filters by the caller's `walletId` from the token, never from the request
>   body.** `WalletAccessFilter` puts it in `RequestContext`; a query that takes it from the body lets
>   one customer read another's balance. See [security.md](docs/security.md#ownership-checks).

## Excerpts from the reference docs

From tinycast `docs/README.md`:

> Each document below has one job and one trigger: the change that obliges you to edit it. A document
> that contradicts the code is a defect, so fix it in the commit that made it wrong.

From tinycast `docs/features/window-rooms.md`, `## Invariants`:

> - **A window's way back is on disk before it moves.** `RoomParkingLedger.record` writes
>   synchronously and returns false when the write fails, and then the window is not parked.

> - **`RoomPlan` decides everything before the first write, and is pure.** Matching, frames, what
>   parks and which apps stay visible come out of one call the harness pins; `RoomRunner` only
>   carries the plan out.

From tinycast `docs/standards.md` opening:

> How code in Tinycast is written. This is **guidance** — it describes what the codebase already looks
> like so that new code reads like it was there all along, and a good reason to depart from it is a
> good reason. […] When this document and the code disagree, the code is probably right and this file
> is stale. Fix it.
