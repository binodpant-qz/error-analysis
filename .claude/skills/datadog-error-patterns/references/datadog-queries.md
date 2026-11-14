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

**3. Issue-backed signatures (5xx)**
```
query:    service:<svc> operation_name:http.server.request @http.status_code:[500 TO 599] @issue.id:*
group_by: {fields: ["@issue.id", "resource_name", "@http.status_code", "@error.type", "@error.message"], limit: 1000}
computes: [{aggregation: COUNT, field: "*", output: errors}]
```

**4. Signatures without an issue (4xx, and 5xx missing an issue)**
```
query:    service:<svc> operation_name:http.server.request @http.status_code:[400 TO 599] -@http.status_code:499 -@issue.id:*
group_by: {fields: ["resource_name", "@http.status_code", "@error.message"], limit: 1000}
computes: [{aggregation: COUNT, field: "*", output: errors}]
```
**5. Errors with no message (always run; grouping in 3–4 drops these)**
```
query:    service:<svc> operation_name:http.server.request @http.status_code:[400 TO 599] -@http.status_code:499 -@error.message:*
group_by: {fields: ["resource_name", "@http.status_code"], limit: 1000}
computes: [{aggregation: COUNT, field: "*", output: errors}]
```
Use `"(no message)"` as the message for these rows.

**6. Duration check for no-message 5xx (timeout inference)**
```
query:    service:<svc> operation_name:http.server.request @http.status_code:[500 TO 599] -@error.message:*
group_by: {fields: ["resource_name"], limit: 50}
computes: [{aggregation: COUNT, field: "*", output: n},
           {aggregation: P50, field: "@duration", output: p50_ns},
           {aggregation: MIN, field: "@duration", output: min_ns}]
```
Don't add `@error.type` to this group_by; it's missing on these spans too.

Drop the 4xx part of query 4 when status classes are 5xx only.
