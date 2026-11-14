# Datadog queries and pitfalls

Learned from the subscriptions service (Nov 2026). Recheck when pointing the skill
at a new service; instrumentation differs between services.

## Pitfalls

1. **Use the app span, not the proxy span.** Each request produces an Istio proxy
   span (`operation_name:istio.proxy.server`) and an app span
   (`operation_name:http.server.request`). Count only the app span, or errors double.
   It is also the span carrying `@error.type`, `@error.message` and `@issue.id`.
   For a new service, check operation names first (query 0).
2. **Version is `@service.version`, not `version`.** The reserved `version` tag is
   empty for OTel-instrumented Go services; grouping by it returns nothing.
   Format: `YYMMDD-HHMMSS-<gitsha>`. Not used in Milestone 1, noted for later.
3. **Only 5xx errors carry `@issue.id`, and only some of them.** On subscriptions,
   347 of 1,175 5xx spans had an issue ID; the same failures often appear both
   with and without one. 4xx spans have no issue ID and no `@error.message`.
8. **No-message 503s are mostly request timeouts.** Their durations are pinned
   at per-endpoint limits (rtdn 2.9s, manage-subscription 5s, webhooks 12s).
9. **Error messages contain user IDs, Zuora account/subscription IDs and
   Google Play purchase tokens.** Never copy raw messages into files or reports;
   template them first (the script does this, but pre-template anything you
   write by hand).
4. **Grouping drops spans missing a group-by field.** Always compare bucket sums
   to an ungrouped total (coverage check).
5. **Status filter uses the attribute:** `@http.status_code:[500 TO 599]`,
   not `status:5xx`. `status:error` is the APM error flag, a different thing.
6. **Spans are sampled** (retention filters, `retained_by: diversity_sampling`).
   Shares between categories are approximate; don't present span counts as
   request totals or error rates.
7. **Spans contain client IPs and geolocation** (`@http.client_ip`,
   `@http.client_ip_details`). Never group by or output them.

## Query templates (aggregate_spans)

Replace `<svc>`, `<window>` (e.g. `now-14d`) and add
`resource_name:"<endpoint>"` clauses when the scope names endpoints
(OR them together inside parentheses).

**0. Operation names (new service only)**
```
query:    service:<svc> @span.kind:server
group_by: {fields: ["operation_name"], limit: 10}
computes: [{aggregation: COUNT, field: "*", output: spans}]
```

**1. Status code overview and ungrouped total**
```
query:    service:<svc> operation_name:http.server.request @http.status_code:[400 TO 599]
group_by: {fields: ["@http.status_code"], limit: 30}
computes: [{aggregation: COUNT, field: "*", output: errors}]
```

**2. Top endpoints by errors (for the scope prompt)**
```
query:    service:<svc> operation_name:http.server.request @http.status_code:[500 TO 599]
group_by: {fields: ["resource_name"], limit: 10}
computes: [{aggregation: COUNT, field: "*", output: errors}]
```

**Group limits.** Datadog rejects an aggregation whose per-field limits multiply
past 10,000 groups ("Cannot generate more than 10000 groups across all dimensions"),
and a group that hits its limit silently drops the rest. Raw `@error.message` values
contain IDs, so one issue can have 20+ distinct messages. Keep each query to two or
three fields, set a limit per field, and check every grouped sum against the same
filter ungrouped (query 1 split by status). Per-field limits need the SDK
(`v2.SpansApi.aggregateSpans` through `execute_code`, `groupBy: [{facet, limit, sort}]`);
the `aggregate_spans` tool takes one `limit` for all fields.

Queries 3, 4 and 5 partition the errors (message + issue, message without issue,
no message), so their ungrouped totals must add up to query 1's total. On
subscriptions they did (347 + 267 + 561 = 1,175).

**3. Issue-backed signatures (5xx), as two queries joined on `@issue.id`**
```
query:    service:<svc> operation_name:http.server.request @http.status_code:[500 TO 599] @issue.id:* @error.message:*
3a group_by: @issue.id (50) x @error.message (150)                 -> one row per issue x message
3b group_by: @issue.id (50) x resource_name (5) x @http.status_code (3) -> endpoint and status per issue
computes: count
```
Give each 3a row the issue's top endpoint and status from 3b. Don't put all four
fields in one query: 50 x 5 x 3 x 150 is far over the cap, and smaller limits
truncate messages (on subscriptions, 324 of 347 spans came back).

**4. Signatures with a message but no issue (4xx, and 5xx missing an issue)**
```
query:    service:<svc> operation_name:http.server.request @http.status_code:[400 TO 599] -@http.status_code:499 -@issue.id:* @error.message:*
group_by: resource_name (20) x @http.status_code (3) x @error.message (100)
computes: count
```

**Error type per message (for queries 3 and 4)**
```
query:    service:<svc> operation_name:http.server.request @http.status_code:[500 TO 599] @error.message:*
group_by: @error.message (200) x @error.type (3)
computes: count
```
Look up `error_type` by raw message. Don't add `@error.type` to queries 3 or 4:
spans missing it drop out of the group.

**5. Errors with no message (always run; queries 3 and 4 can't see these)**
```
query:    service:<svc> operation_name:http.server.request @http.status_code:[400 TO 599] -@http.status_code:499 -@error.message:*
group_by: resource_name (50) x @http.status_code (5)
computes: count
```
Use `"(no message)"` as the message for these rows.

**6. Duration check for no-message 5xx (timeout inference)**
```
query:    service:<svc> operation_name:http.server.request @http.status_code:[500 TO 599] -@error.message:*
group_by: resource_name (50)
computes: count; median of @duration (p50_ns); min of @duration (min_ns)
```
The SDK has no `pc50`: use `median` (`{aggregation: "median", metric: "@duration"}`).
The `aggregate_spans` tool accepts `P50`. Don't add `@error.type` to this group_by;
it's missing on these spans too.

Drop the 4xx part of query 4 when status classes are 5xx only.

**Template messages before they leave Datadog.** Messages carry user IDs, Zuora
IDs and purchase tokens. When running these through `execute_code`, apply the
same scrub patterns as `cluster.py` (`SCRUB`) in the script and return only templates.
