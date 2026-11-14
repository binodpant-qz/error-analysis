---
name: datadog-error-patterns
description: Group a service's HTTP 4xx/5xx errors from Datadog APM into a few major categories using K-means clustering, and summarize each category (share of errors, endpoints, status codes, example messages, Error Tracking links). Use this whenever someone asks what kinds of errors a service is throwing, wants error patterns, themes or categories, asks to "group", "cluster" or "summarize" errors, 500s, 4xx/5xx or failures for a service or endpoint over a time window, even if they don't mention clustering. Works for any service in Datadog APM; subscriptions is the reference service.
---

# Datadog error patterns

Turns a pile of 4xx/5xx errors into 3–8 named categories a person can reason about.
Milestone 1 scope: categorize and summarize only. No error rates, trends or release
correlation yet (span counts are sampled, see references/datadog-queries.md).

Read `references/datadog-queries.md` before writing any query. It holds the
field names and pitfalls that make or break this analysis.

## 1. Scope

The caller may already supply scope (for example another skill like QZQA passing it in).
Use whatever is provided and ask only for what is missing, in one prompt:

| Field | Default |
|---|---|
| service | required, no default |
| endpoints | all (`resource_name` values) |
| window | last 14 days |
| status classes | 5xx; 4xx is opt-in |
| env | prod |

499 (client closed request) is excluded from clustering by default and reported
as a single line, because it is usually client-side noise but can point at slow endpoints.

If the user wants specific endpoints but doesn't know their exact names, run the
"top endpoints by errors" query and let them pick.

## 2. Pull error signatures

A **signature** is the unit we cluster:
- 5xx with an Error Tracking issue: `issue:<@issue.id>`
- anything without an issue ID (all 4xx today): `sig:<endpoint>|<status>|<message template>`

Run queries 3, 4 and 5 from the reference file: issue-backed errors, errors with a
message but no issue, and errors with no message at all. Then do the **coverage
check**: the three must add up to the ungrouped total from query 1. Grouping
silently drops spans missing a group-by field, so skipping query 5 can lose half
the data (on subscriptions, 48% of 5xx and all 4xx have no message).

**Infer timeouts for no-message 5xx.** Run query 6 (duration per endpoint). If an
endpoint's min duration is within ~2% of its p50 and sits on a round value
(2.9s, 5s, 9s, 12s), the request-timeout middleware cut it off: set
`"cause": "timeout"` on those rows. Otherwise leave cause unset.

Write the rows to `signatures.json` as a list of objects:

```json
{"signature": "issue:185bdcfa-...", "issue_id": "185bdcfa-...", "endpoint": "POST /v0/...",
 "status_code": 503, "error_type": "*errs.Error", "message": "webhook processing failed; will retry",
 "count": 412, "cause": null}
```
`cause` is optional; set it only when you inferred it (see above).

One row per signature × endpoint × status is fine; the script merges them.

**Privacy:** keep only the fields above. Never copy client IPs, geolocation, user
agents, request headers, full URLs with query strings, or stack traces into the
file or the report. The script also scrubs IDs, emails, IPs and numbers from messages.

## 3. Cluster

```bash
pip install -r scripts/requirements.txt --break-system-packages   # if needed
python scripts/cluster.py signatures.json --status-class 5xx --out clusters.json
```

Cluster 5xx and 4xx in separate runs; mixed runs split on "has message vs not"
instead of failure mode.

What it does: normalizes messages into templates (bracketed params, tokens,
query strings, IDs and numbers scrubbed), classifies each error chain's leaf into
a failure mode (timeout, auth, conflict, not_found, unsupported, bad_data, other),
builds TF-IDF vectors from failure mode (weighted 3x) + error type + message +
endpoint + status class, runs K-means for k = 2..8 (fixed seed, weighted by log
of error count), picks k by silhouette, and writes clusters with members, cause,
top terms, endpoints and shares.

Silhouette on this kind of data is low (0.15–0.2 is normal); judge clusters by
whether their members share a failure mode and a story. If the best k is the
max k, say so: the data may want finer splits. Merge adjacent clusters in the
report when they're the same failure on the same endpoint.

**4xx:** these usually have no message, so clustering only regroups endpoints.
Prefer a short endpoint × status table for 4xx unless the user asks to cluster.

Flags: `--k N` forces k; `--weight raw|log|none`; `--max-k N`.
With fewer than 4 signatures it skips clustering and returns one group per
signature; say so instead of pretending to have categories.

## 4. Label and report

Name each cluster from its members and top terms: short, concrete, about the
failure ("Chargeback webhook processing failures"), not the algorithm
("Cluster 2"). If a cluster's members don't share a clear theme, call it mixed
and say why rather than inventing one.

Report in chat, in this order:
1. One-line scope: service, endpoints, window, status classes, total errors analysed, coverage.
2. Categories, largest first. For each: name, share of errors, top endpoints,
   status codes, 1–2 example message templates, links to Error Tracking issues
   (`<base_url>/error-tracking/issue/<issue_id>`).
3. The 499 line and anything excluded.
4. Caveats: sampled spans (shares are approximate), chosen k and silhouette score,
   anything odd in coverage.

Also save `summary.json` (the clusters plus your labels) so another skill can
consume it later:

```json
{"scope": {...}, "total_errors": 2985, "k": 4, "silhouette": 0.41,
 "categories": [{"label": "...", "share": 0.38, "error_count": 1130,
   "endpoints": [...], "status_codes": [...], "issue_ids": [...], "examples": [...]}],
 "excluded": {"499": 1236}}
```

## 5. Sanity check

With few signatures (tens), K-means isn't the only option: Claude could group them
directly. When testing the skill, compare the two and note where they disagree;
that's the signal for whether clustering earns its place in Milestone 2.
