#!/usr/bin/env python3
"""Cluster error signatures from Datadog APM into categories with weighted K-means.

Input: JSON list of rows with keys signature, endpoint, status_code, error_type,
message, count (issue_id optional). Rows sharing a signature are merged.
Output: JSON with chosen k, silhouette scores and clusters.
"""
import argparse
import json
import math
import re
import sys
from collections import Counter, defaultdict

# Checked before the third-party imports so an old interpreter gets this message
# instead of a ModuleNotFoundError from a failed install.
if sys.version_info < (3, 12):
    sys.exit(f"cluster.py needs Python >= 3.12 (pinned numpy 2.5); this is {sys.version.split()[0]}.")

import numpy as np
from sklearn.cluster import KMeans
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics import silhouette_score

SEED = 42

SCRUB = [
    # Go error strings put parameters in brackets: id[...], token[...], userID[...]
    (re.compile(r"\[[^\]]{1,300}\]"), "[<v>]"),
    # URL query strings carry account IDs and field lists
    (re.compile(r"\?[^\s\"']+"), "?<query>"),
    # long opaque tokens (purchase tokens, JWT-like strings)
    (re.compile(r"\b[A-Za-z0-9_-]{24,}(?:\.[A-Za-z0-9_-]{8,})+\b"), "<token>"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "<email>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"), "<ip>"),
    (re.compile(r"\b0x[0-9a-f]+\b", re.I), "<hex>"),
    (re.compile(r"\b[0-9a-f]{12,}\b", re.I), "<hex>"),
    (re.compile(r"\b(?:sub|cus|in|pi|ch|evt|req)_[A-Za-z0-9]{6,}\b"), "<id>"),
    (re.compile(r"\d+(?:\.\d+)?"), "<n>"),
]


def template(message):
    """Replace identifiers and numbers so similar messages collapse to one template."""
    msg = (message or "(no message)").strip()
    for pattern, repl in SCRUB:
        msg = pattern.sub(repl, msg)
    # keep the whole chain: the root cause is at the END of Go error chains
    return re.sub(r"\s+", " ", msg)[:2000]


def merge_rows(rows):
    sigs = {}
    for r in rows:
        key = r["signature"]
        s = sigs.setdefault(key, {
            "signature": key,
            "issue_id": r.get("issue_id"),
            "error_type": r.get("error_type") or "",
            "message": template(r.get("message")),
            "cause_hint": r.get("cause"),
            "count": 0,
            "endpoints": Counter(),
            "status_codes": Counter(),
        })
        c = int(r.get("count", 0))
        s["count"] += c
        s["endpoints"][r.get("endpoint", "")] += c
        s["status_codes"][str(r.get("status_code", ""))] += c
    return list(sigs.values())


CAUSES = [
    ("timeout", r"deadline exceeded|timeout|timed out"),
    ("auth", r"unauthori[sz]ed|unauthenticated|not authenticated|forbidden|\b401\b|\b403\b"),
    ("conflict", r"≠|already|conflict|rejecting|duplicate"),
    ("not_found", r"no rows|not found|no \w+ package for|missing"),
    ("unsupported", r"unimplemented|no \w+ inference mapping|unsupported"),
    ("ineligible", r"not [\w-]+ eligible|ineligible|switches are off"),
    ("bad_data", r"unmarshal|cannot be null|empty \w+|invalid|no line items|unprocessable|bad request"),
]
FOCUS_WEIGHT = 3  # how many times the preset's focus token is repeated in the document

# Markers for the downstream system a segment of an error chain talks to. Checked
# per segment from the leaf outwards, so the deepest call wins: "build zuora
# subscription info: query by subscription number: deadline exceeded" is a DB failure.
DEPENDENCIES = [
    ("web_php", r"web-php|web_php|/rpc/"),
    # Not "zms": it names checkout flows ("checkout zms: ..."), not a call to Zuora.
    ("zuora", r"zuora|submit order|by order"),
    ("apple", r"appstoreapi|storekit|apple request|call apple"),
    ("google_play", r"androidpublisher|googleapis|play developer"),
    ("db", r"\bsql\b|psdb\.cloud|planetscale|begin tx|rollback|^query\b|insert \w+"),
]
# Provider API calls whose segment doesn't name the provider ("get subscription v2").
# The provider comes from the rest of the chain.
PROVIDER_CALL = r"get subscription v<n>"
PROVIDER_CONTEXT = [
    ("google_play", r"play store|rtdn|receipt|google"),
    ("apple", r"apple|ios"),
    ("zuora", r"zuora|\bzms\b"),
]
PRESETS = ("root_cause", "endpoint", "dependency")


def dependency_of(message):
    """Name the downstream system the deepest failing call in the chain talks to."""
    if message == "(no message)":
        return "unknown"
    text = message.lower()
    for segment in reversed(text.split(": ")):
        for name, pat in DEPENDENCIES:
            if re.search(pat, segment):
                return name
        if re.search(PROVIDER_CALL, segment):
            for name, pat in PROVIDER_CONTEXT:
                if re.search(pat, text):
                    return name
    return "internal"


def cause_of(message):
    """Classify the leaf of a Go-style 'a: b: leaf' error chain into a failure mode."""
    if message == "(no message)":
        return None
    leaf = message.lower().rsplit(": ", 2)[-2:]
    leaf = " ".join(leaf)
    for name, pat in CAUSES:
        if re.search(pat, leaf):
            return name
    return "other"


def endpoint_token(endpoint):
    return "ep_" + re.sub(r"[^a-z0-9]+", "_", endpoint.lower()).strip("_")


def doc_for(sig, by="root_cause"):
    """Text the vectorizer sees: the preset's focus token (repeated), failure mode,
    error type, message template, endpoint words and status class."""
    route_words = " ".join(re.split(r"[^A-Za-z]+", " ".join(sig["endpoints"])))
    statuses = " ".join(f"status_{c} class_{c[:1]}xx" for c in sig["status_codes"])
    etype = re.sub(r"[^A-Za-z]+", " ", sig["error_type"])
    msg = "" if sig["message"] == "(no message)" else sig["message"]
    msg = re.sub(r"<\w+>", " ", msg)  # placeholders carry no meaning
    cause = sig.get("cause_hint") or cause_of(sig["message"])
    cause_token = f"cause_{cause}" if cause else "cause_unknown"

    if by == "endpoint":
        top_endpoint = sig["endpoints"].most_common(1)[0][0]
        focus = [endpoint_token(top_endpoint)] * FOCUS_WEIGHT + [cause_token]
    elif by == "dependency":
        focus = [f"dep_{dependency_of(sig['message'])}"] * FOCUS_WEIGHT + [cause_token]
    else:
        focus = [cause_token] * (FOCUS_WEIGHT if cause else 1)

    return f"{' '.join(focus)} {etype} {msg} {route_words} {statuses}".lower()


def weights(sigs, mode):
    if mode == "raw":
        return np.array([s["count"] for s in sigs], dtype=float)
    if mode == "log":
        return np.array([math.log1p(s["count"]) for s in sigs], dtype=float)
    return np.ones(len(sigs))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("--out", default="clusters.json")
    ap.add_argument("--k", type=int, help="force number of clusters")
    ap.add_argument("--max-k", type=int, default=8)
    ap.add_argument("--status-class", choices=["4xx", "5xx", "all"], default="all",
                    help="cluster only this status class")
    ap.add_argument("--weight", choices=["raw", "log", "none"], default="log")
    ap.add_argument("--by", choices=PRESETS, default="root_cause",
                    help="what the categories are about: failure mode, endpoint or downstream system")
    args = ap.parse_args()

    with open(args.input) as f:
        sigs = merge_rows(json.load(f))
    sigs = [s for s in sigs if s["count"] > 0]
    if args.status_class != "all":
        want = args.status_class[0]
        sigs = [s for s in sigs if any(c.startswith(want) for c in s["status_codes"])]
    total = sum(s["count"] for s in sigs)
    n = len(sigs)
    if n == 0:
        sys.exit("No signatures with errors in input.")

    result = {"signatures": n, "total_errors": total, "by": args.by, "weight": args.weight}

    if n < 4:
        labels = list(range(n))
        result.update(k=n, silhouette=None, silhouette_by_k={},
                      note="Fewer than 4 signatures; not clustered.")
        top_terms = [[] for _ in range(n)]
    else:
        vec = TfidfVectorizer(ngram_range=(1, 2), min_df=1, sublinear_tf=True,
                              stop_words="english", token_pattern=r"(?u)\b[a-z_][a-z0-9_]+\b")
        X = vec.fit_transform([doc_for(s, args.by) for s in sigs])
        w = weights(sigs, args.weight)
        terms = np.array(vec.get_feature_names_out())

        distinct = len({tuple(r) for r in X.toarray().round(6)})  # identical docs can't be split
        top = max(2, min(args.max_k, n - 1, distinct))
        candidates = [args.k] if args.k else range(2, top + 1)
        scores, models = {}, {}
        for k in candidates:
            km = KMeans(n_clusters=k, n_init=10, random_state=SEED)
            km.fit(X, sample_weight=w)
            models[k] = km
            if len(set(km.labels_)) > 1:
                scores[k] = round(float(silhouette_score(X, km.labels_, metric="cosine")), 3)
        best_k = args.k or max(scores, key=scores.get)
        km = models[best_k]
        labels = km.labels_.tolist()
        top_terms = [terms[np.argsort(c)[::-1][:8]].tolist() for c in km.cluster_centers_]
        result.update(k=best_k, silhouette=scores.get(best_k), silhouette_by_k=scores)

    groups = defaultdict(list)
    for sig, lab in zip(sigs, labels):
        groups[lab].append(sig)

    clusters = []
    for lab, members in groups.items():
        count = sum(m["count"] for m in members)
        eps, codes = Counter(), Counter()
        for m in members:
            eps.update(m["endpoints"])
            codes.update(m["status_codes"])
        members.sort(key=lambda m: -m["count"])
        clusters.append({
            "cluster_id": int(lab),
            "error_count": count,
            "share": round(count / total, 3),
            "top_terms": top_terms[lab] if lab < len(top_terms) else [],
            "endpoints": eps.most_common(5),
            "status_codes": codes.most_common(),
            "members": [{
                "signature": m["signature"], "issue_id": m["issue_id"],
                "error_type": m["error_type"], "message": m["message"], "count": m["count"],
                "cause": m.get("cause_hint") or cause_of(m["message"]),
                "dependency": dependency_of(m["message"]),
                "endpoints": [e for e, _ in m["endpoints"].most_common(3)],
            } for m in members],
        })
    clusters.sort(key=lambda c: -c["error_count"])
    result["clusters"] = clusters

    with open(args.out, "w") as f:
        json.dump(result, f, indent=2)
    print(f"{n} signatures, {total} errors -> k={result['k']} "
          f"(silhouette {result['silhouette']}); wrote {args.out}")
    for c in clusters:
        print(f"  cluster {c['cluster_id']}: {c['share']:.0%} ({c['error_count']}) "
              f"terms={', '.join(c['top_terms'][:5])}")


if __name__ == "__main__":
    main()
