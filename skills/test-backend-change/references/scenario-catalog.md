# Scenario catalog — recipes for the awkward categories

Step 5b of `SKILL.md` lists the categories a run should walk. Most of them are an ordinary curl. This file covers the ones where people either skip the category or "test" it in a way that can't actually fail, which is worse — a green case that proves nothing tells the reader the behaviour is safe when nobody checked.

Every recipe here is shell you can adapt directly. They assume `$TOKEN` / `$COOKIE` from Step 4 and a Postgres container from Step 2; swap the DB command for your stack.

## Contents

- [Concurrency](#concurrency)
- [Idempotency and replay](#idempotency-and-replay)
- [Authorization and tenant isolation](#authorization-and-tenant-isolation)
- [Pagination, sorting and sort injection](#pagination-sorting-and-sort-injection)
- [Migrations reaching existing data](#migrations-reaching-existing-data)
- [Side effects: events, mail, cache](#side-effects-events-mail-cache)
- [Conditional requests and ETags](#conditional-requests-and-etags)
- [Boundary values worth trying](#boundary-values-worth-trying)
- [Error contract](#error-contract)

---

## Concurrency

The claim is "two identical requests at once produce one row, not two". Firing them sequentially can't test that — the first one finishes and the second sees its result. They have to overlap.

```bash
REF="ord-$(date +%s)"
for i in 1 2 3 4 5; do
  curl -sS -o /tmp/conc-$i.json -w "%{http_code}\n" \
    -X POST http://localhost:8080/api/orders \
    -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    -d "{\"externalRef\": \"$REF\", \"quantity\": 1}" &
done
wait
```

Then the evidence that matters is the DB, not the five responses:

```bash
docker exec -i pg psql -U app -d app \
  -c "SELECT count(*) FROM orders WHERE external_ref = '$REF';"
```

Report the count of each status across the five attempts (`1 × 201, 4 × 409`) in a plain block, plus the row count. One 201 and one row is a pass. Five rows is a real bug and the most valuable thing the run will find.

If the endpoint is a state transition rather than a create, the same shape applies: fire N captures at one authorized payment, then check the ledger has one capture row and the balance moved once.

## Idempotency and replay

Two different claims, both worth a case:

**Replay with the same idempotency key** should return the same result and write once.

```bash
KEY=$(uuidgen)
for n in first second; do
  curl -sS -D /tmp/h-$n.txt -o /tmp/b-$n.json -X POST http://localhost:8080/api/payments \
    -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    -H "Idempotency-Key: $KEY" -d '{"amount": 100, "currency": "SAR"}'
done
diff /tmp/b-first.json /tmp/b-second.json && echo "bodies identical"
```

**Plain replay with no key** — send the same create twice and see what the second one does. A 409 with the project's error code is fine. A 500 from a raw constraint violation is a finding: the constraint is doing the job the service should be doing, and the client gets an unhelpful error.

Both cases end at the DB with a `count(*)`.

## Authorization and tenant isolation

Authentication (`no token → 401`) is not authorization. The cases that matter use a **valid** token:

| Case | Expectation |
| --- | --- |
| Token for user B reads user A's resource | 404 — not 403, and not 200 |
| Token for a role without the permission | 403 with the project's error code |
| Token for tenant T2 reads an id created in tenant T1 | 404, and the DB shows T1's row untouched |
| Tenant T2 host header with a T1-issued token | rejected, not silently cross-served |

Why 404 and not 403 for another tenant's id: a 403 confirms the id exists. Iterating ids against a 403/404 split enumerates the other tenant's data. If the app answers 403, that's worth reporting even though "access was denied" looks correct at a glance.

To get a second identity, prefer whatever the app already offers — a second seed user in `data.sql`, a second tenant from the signup flow. Creating one through the API is fine and is itself a happy-path case you needed anyway.

```bash
TOKEN_B=$(curl -s -X POST http://localhost:8080/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"b@example.com","password":"secret"}' | jq -r '.data.accessToken')

curl -sS -D /tmp/h.txt -o /tmp/b.json \
  http://localhost:8080/api/orders/$ORDER_ID_OWNED_BY_A \
  -H "Authorization: Bearer $TOKEN_B"
head -1 /tmp/h.txt; jq . /tmp/b.json
```

## Pagination, sorting and sort injection

Only applies when the change returns a collection. Four quick cases, one curl each:

```bash
# past the end — empty content, not an error, and totalElements still correct
curl -sS "http://localhost:8080/api/orders?page=9999&size=20" -H "Authorization: Bearer $TOKEN" | jq '{n: (.data.content|length), total: .data.totalElements}'

# size=0 — either rejected or coerced to the default; a 500 is a finding
curl -sS -o /dev/null -w "%{http_code}\n" "http://localhost:8080/api/orders?size=0" -H "Authorization: Bearer $TOKEN"

# size over the configured max — must be capped, or one client can pull the table
curl -sS "http://localhost:8080/api/orders?size=100000" -H "Authorization: Bearer $TOKEN" | jq '.data.content|length'

# sort on a field that isn't whitelisted — must be rejected, never reach SQL
curl -sS -o /dev/null -w "%{http_code}\n" "http://localhost:8080/api/orders?sort=password,desc" -H "Authorization: Bearer $TOKEN"
```

Spring's `Pageable` binds `sort` straight to a property path by default. If `sort=someOtherEntityField` returns 200, the sort surface is wider than the response DTO and that belongs in the report.

Also check the collection is tenant-scoped: list as tenant T2 and confirm T1's rows aren't in it. A list endpoint is the easiest place to leak a whole table.

## Migrations reaching existing data

A migration that works on a database created from scratch says nothing about the databases that already exist — and in a schema-per-tenant app, "already exists" is every customer. This is the failure mode that a fresh-container test run cannot see, because the container was fresh.

Two things to check when the diff touches `db/migration`, `db/changelog`, or an entity:

1. **Existing rows.** Query rows created before the change and confirm the new column is populated / the new constraint holds. A `NOT NULL` added with no back-fill fails here.
2. **Existing tenant schemas.** If the app provisions a schema per tenant, confirm the changeset ran against a tenant that existed before this deploy, not only against one you created during the run.

```bash
docker exec -i pg psql -U app -d app -c "\dn"        # list tenant schemas
docker exec -i pg psql -U app -d app \
  -c "SELECT table_schema, column_name FROM information_schema.columns
      WHERE column_name = 'new_column' AND table_name = 'your_table';"
```

If a schema is missing the column, that's a FAIL with a clear cause even though every API call passed.

## Side effects: events, mail, cache

A 202 is a promise, not evidence. Verify the effect where it lands:

```bash
# mail — mailpit / mailhog expose a JSON API
curl -sS http://localhost:8025/api/v1/messages | jq '.messages[0] | {to: .To, subject: .Subject}'

# queue — rabbit management API
curl -sS -u guest:guest http://localhost:15672/api/queues/%2f/orders.created | jq '.messages'

# outbox / audit table
docker exec -i pg psql -U app -d app -c "SELECT type, payload FROM outbox ORDER BY id DESC LIMIT 3;"

# cache — did the write evict the read?
docker exec -i redis redis-cli KEYS 'orders::*'
```

Negative cases matter here too: a rejected request must not have sent the mail.

## Conditional requests and ETags

```bash
ETAG=$(curl -sS -D - -o /dev/null http://localhost:8080/api/i18n -H "Authorization: Bearer $TOKEN" \
  | awk 'tolower($1)=="etag:"{print $2}' | tr -d '\r')

curl -sS -D /tmp/h.txt -o /tmp/b.txt http://localhost:8080/api/i18n \
  -H "Authorization: Bearer $TOKEN" -H "If-None-Match: $ETAG"
head -1 /tmp/h.txt; wc -c < /tmp/b.txt   # expect 304 and 0 bytes
```

Then the case people forget: change the underlying data and confirm the ETag *changes*. A stable-forever ETag is a caching bug that looks like a passing test.

## Boundary values worth trying

Pick the ones the change actually exposes rather than firing all of them:

| Input kind | Try |
| --- | --- |
| Integer / amount | `0`, `-1`, the documented max, max + 1, a non-integer |
| String | `""`, `"   "`, max length, max length + 1, leading/trailing spaces |
| Collection | `[]`, one element, a few hundred elements |
| Date / time | past, far future, a DST boundary, a different timezone offset |
| Enum | a value not in the enum, a lowercase variant of a valid one |
| Localized text | RTL Arabic/Hebrew, an emoji, a 4-byte character (catches `utf8` vs `utf8mb4`) |
| Money | more decimal places than the currency allows |
| Id | an id from another tenant, a deleted id, a malformed UUID |

## Error contract

Most projects standardize the error body (RFC 7807 problem details, an `errorCode`, a `requestId` that matches a response header). Rather than writing a dedicated case, assert the contract on the error responses the other cases already produce, and give it one row in the coverage table naming those cases. Check specifically:

- the code is the project's documented one, not a generic 500
- `requestId` in the body equals the correlation header on the same response
- the offending field name is present and correct for validation errors
- multiple simultaneous violations come back as multiple entries in one response, not just the first
- no stack trace, SQL fragment, or internal class name leaks into the body
