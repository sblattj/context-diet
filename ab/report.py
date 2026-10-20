#!/usr/bin/env python3
"""Report per-arm trigger metrics with Wilson 95% CIs and paired exact McNemar tests.

Metrics (prompt level; excluded/errored runs dropped, reps collapsed by --aggregate first):
  recall            positives where an expected skill fired
  recall (implicit) same, restricted to positives that do not name the skill
  wrong skill       positives where skills fired but none was expected
  fires on negatives (rows with label negative or expect_none true)
  read-only fires   informational: positives whose expected skill was only read (read_skills), never in recall
  median start tokens (first assistant turn input+cache usage)
Savings: per case, the median start tokens over its usable reps, per arm; each non-baseline
arm is paired with the baseline on cases where both have a value. Ratio = arm/baseline per
case; reported as the geometric-mean ratio (percent change) with a 95% percentile bootstrap
CI (resampling cases, 2000 resamples, --seed) and an exact two-sided sign test on
cheaper vs costlier cases (ties excluded from the test, counted in the case total).
Majority needs strictly more than half the reps. An exact tie resolves conservatively: a tied
positive is a miss (recall), a tied negative counts as fired and a tied wrong pick counts as wrong.
Read-only fires (expected skill only read, record key read_skills) are an informational column and
never count toward recall. Rows with expect_none true are negatives.
Unit of analysis is the prompt (case). Reps of a case are aggregated by a rule declared up front
(--aggregate majority|all|any, default majority) before any test or rate; every rate below is
over prompts, never runs. Paired test: each non-baseline arm vs the baseline (first arm in run
order, or --baseline), over positive prompts present in both arms. McNemar is the exact two-sided
binomial test on discordant prompts. Non-inferiority: one-sided 95% bounds (normal approximation,
paired difference) against --margin-pp (default 5): non-inferior if the lower bound is above
-margin, inferior if the upper bound is below -margin, else inconclusive. Provider error rate per
arm (error / is_error rows over non-excluded rows) above --max-error-rate (default 0.05) makes the
verdict "invalid". Also lists per-skill regressions and a per-entry (expected skill) table.
"""
import argparse
import json
import math
import random
import statistics
import sys
from collections import defaultdict

from run import skill_matches

Z95 = 1.959963984540054
Z95_ONE_SIDED = 1.6448536269514722
AGGREGATES = ("majority", "all", "any")
DEFAULT_MARGIN_PP = 5.0
DEFAULT_MAX_ERROR_RATE = 0.05


def wilson(k, n, z=Z95):
    """Wilson score interval for k successes of n. Returns (lo, hi); (nan, nan) if n == 0."""
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def mcnemar_exact(b, c):
    """Exact two-sided McNemar p (binomial on discordant pairs b, c)."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def sign_test_exact(k, n):
    """Exact two-sided binomial (p = 0.5) sign-test p for k of n; n == 0 gives 1.0."""
    return mcnemar_exact(k, n - k)


BOOT_RESAMPLES = 2000


def geo_mean(ratios):
    return math.exp(math.fsum(math.log(x) for x in ratios) / len(ratios))


def case_medians(recs):
    """case id -> median start tokens over that case's usable reps with a token value."""
    by = defaultdict(list)
    for r in recs:
        if r.get("start_tokens"):
            by[r["case"]].append(r["start_tokens"])
    return {c: statistics.median(v) for c, v in by.items()}


def savings(base_med, arm_med, seed=0, resamples=BOOT_RESAMPLES):
    """Paired token savings of an arm vs the baseline from per-case medians.

    Returns dict(cases, geo_ratio, pct, ci (pct lo, hi), cheaper, costlier, p), or None
    if fewer than 2 paired cases.
    """
    common = sorted(set(base_med) & set(arm_med))
    if len(common) < 2:
        return None
    ratios = [arm_med[c] / base_med[c] for c in common]
    gm = geo_mean(ratios)
    rng = random.Random(seed)
    n = len(ratios)
    boots = sorted(geo_mean([ratios[rng.randrange(n)] for _ in range(n)]) for _ in range(resamples))
    lo = min(boots[int(0.025 * resamples)], gm)  # clamp only matters for float noise on constant ratios
    hi = max(boots[int(0.975 * resamples) - 1], gm)
    cheaper = sum(1 for x in ratios if x < 1)
    costlier = sum(1 for x in ratios if x > 1)
    return {"cases": n, "geo_ratio": gm, "pct": 100 * (gm - 1),
            "ci": [100 * (lo - 1), 100 * (hi - 1)], "cheaper": cheaper, "costlier": costlier,
            "p": sign_test_exact(cheaper, cheaper + costlier)}


def is_hit(rec):
    return any(skill_matches(f, e) for f in rec["skills"] for e in rec["expected"])


def load(path):
    recs = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                recs.append(json.loads(line))
    return recs


def fmt(k, n):
    if n == 0:
        return "n/a (0)"
    lo, hi = wilson(k, n)
    return "%3.0f%% [%2.0f-%2.0f] (%d/%d)" % (100 * k / n, 100 * lo, 100 * hi, k, n)


def is_err(rec):
    return bool(rec.get("error") or rec.get("is_error"))


def eff_label(rec):
    """expect_none rows are negatives whatever their label says."""
    return "negative" if rec.get("expect_none") else rec["label"]


def is_read_hit(rec):
    return any(skill_matches(f, e) for f in rec.get("read_skills") or [] for e in rec["expected"])


def agg(vals, rule, tie=False):
    """Collapse one prompt's per-rep booleans by the declared rule.

    Majority needs strictly more than half. An exact tie (e.g. 1 of 2 reps) resolves to `tie`:
    False for hit (a tied positive is a miss), True for the safety-direction metrics wrong and
    fired (a tied negative counts as fired, a tied wrong pick counts as wrong)."""
    n, k = len(vals), sum(1 for v in vals if v)
    if rule == "all":
        return n > 0 and k == n
    if rule == "any":
        return k > 0
    return k * 2 > n or (tie and k * 2 == n and n > 0)


def prompts(recs, rule="majority"):
    """case id -> prompt-level outcome, aggregating that case's reps by rule."""
    by = defaultdict(list)
    for r in recs:
        by[r["case"]].append(r)
    out = {}
    for c, rs in by.items():
        first = rs[0]
        out[c] = {"label": eff_label(first), "expected": first.get("expected") or [],
                  "explicit": bool(first.get("explicit")), "reps": len(rs),
                  "hit": agg([is_hit(r) for r in rs], rule),
                  "read_hit": agg([is_read_hit(r) for r in rs], rule),
                  "wrong": agg([bool(r["skills"]) and not is_hit(r) for r in rs], rule, tie=True),
                  "fired": agg([bool(r["skills"]) for r in rs], rule, tie=True)}
    return out


def run_level(recs):
    """Legacy run-level counts (every rep counted once); kept for callers of the old keys."""
    pos = [r for r in recs if eff_label(r) == "positive"]
    neg = [r for r in recs if eff_label(r) == "negative"]
    imp = [r for r in pos if not r.get("explicit")]
    return {
        "recall": (sum(is_hit(r) for r in pos), len(pos)),
        "recall_implicit": (sum(is_hit(r) for r in imp), len(imp)),
        "wrong": (sum(1 for r in pos if r["skills"] and not is_hit(r)), len(pos)),
        "neg_fire": (sum(1 for r in neg if r["skills"]), len(neg)),
    }


def summarize(recs, rule="majority"):
    """Prompt-level summary: reps of a case are collapsed by rule first."""
    pr = prompts(recs, rule)
    pos = [p for p in pr.values() if p["label"] == "positive"]
    neg = [p for p in pr.values() if p["label"] == "negative"]
    imp = [p for p in pos if not p["explicit"]]
    toks = [r["start_tokens"] for r in recs if r.get("start_tokens")]
    return {
        "recall": (sum(p["hit"] for p in pos), len(pos)),
        "recall_implicit": (sum(p["hit"] for p in imp), len(imp)),
        "wrong": (sum(p["wrong"] for p in pos), len(pos)),
        "read_fires": (sum(p["read_hit"] for p in pos), len(pos)),
        "neg_fire": (sum(p["fired"] for p in neg), len(neg)),
        "median_tokens": statistics.median(toks) if toks else None,
    }


def majority(recs):
    """case id -> bool majority hit over positive records."""
    return {c: p["hit"] for c, p in prompts([r for r in recs if eff_label(r) == "positive"]).items()}


def skill_rates(recs, rule="majority"):
    by = defaultdict(lambda: [0, 0])
    for p in prompts(recs, rule).values():
        if p["label"] == "positive":
            for e in p["expected"][:1]:
                by[e][1] += 1
                by[e][0] += p["hit"]
    return by


def paired_noninferiority(b, c, n, margin_pp=DEFAULT_MARGIN_PP, z=Z95_ONE_SIDED):
    """Paired difference (candidate - baseline) over n prompts with b = baseline-only hits and
    c = candidate-only hits. One-sided 95% bounds (or the z given) from the paired variance (normal approximation,
    with one pseudo-discordant pair so zero observed discordance is not read as zero variance).
    Returns dict(diff_pp, lower_pp, upper_pp, margin_pp, verdict)."""
    if n == 0:
        return {"diff_pp": float("nan"), "lower_pp": float("nan"), "upper_pp": float("nan"),
                "margin_pp": margin_pp, "verdict": "inconclusive"}
    d = (c - b) / n
    se = math.sqrt(max((b + c + 1) / n - d * d, 0.0) / n)  # +1 discordant keeps se > 0 when b = c = 0
    lo, hi = 100 * (d - z * se), 100 * (d + z * se)
    verdict = "non-inferior" if lo > -margin_pp else "inferior" if hi < -margin_pp else "inconclusive"
    return {"diff_pp": 100 * d, "lower_pp": lo, "upper_pp": hi, "margin_pp": margin_pp, "verdict": verdict}


def entry_table(per, arms, base, rule):
    """expected skill -> per-arm prompt recall [k, n], delta vs baseline (pp), n (max prompt count across arms)."""
    rates = {a: skill_rates(per[a], rule) for a in arms}
    out = {}
    for s in sorted({s for a in arms for s in rates[a]}):
        e = {"arms": {a: list(rates[a][s]) for a in arms if s in rates[a]}, "delta_pp": {}}
        e["n"] = max(v[1] for v in e["arms"].values())
        bk, bn = e["arms"].get(base, (0, 0))
        for a, (k, n) in e["arms"].items():
            if a != base and n and bn:
                e["delta_pp"][a] = 100 * (k / n - bk / bn)
        out[s] = e
    return out


def control_arms(recs, extra=()):
    """Arm names marked as negative controls: any record with control=true, plus --control names."""
    return sorted({r["arm"] for r in recs if r.get("control")} | set(extra))


def add_control_info(out, controls):
    """Label control arms and attach the calibration (can the harness detect a recall loss?)."""
    out["controls"] = [c for c in controls if c in out["arms"]]
    out["calibration"] = {}
    for c in out["controls"]:
        out["arms"][c]["control"] = True
        pr = out["paired"].get(c)
        if pr is None:
            continue
        pr["control"] = True
        bk, bn = out["arms"][out["baseline"]]["prompt"]["recall"]
        ck, cn = out["arms"][c]["prompt"]["recall"]
        lower = bn > 0 and cn > 0 and ck / cn < bk / bn
        out["calibration"][c] = {"recall": [ck, cn], "baseline_recall": [bk, bn],
                                 "p": pr["p"], "detects_loss": bool(lower and pr["p"] < 0.05)}


def render_calibration(rep):
    L = []
    for c, cal in (rep.get("calibration") or {}).items():
        (ck, cn), (bk, bn) = cal["recall"], cal["baseline_recall"]
        pct = lambda k, n: "n/a" if n == 0 else "%.0f%%" % (100 * k / n)  # noqa: E731
        L.append("control %s: recall %s vs baseline %s (harness can detect loss: %s; McNemar p = %.4g)" % (
            c, pct(ck, cn), pct(bk, bn), "yes" if cal["detects_loss"] else "no", cal["p"]))
    return L


def build_report(recs, baseline=None, synthetic="exclude", seed=0, controls=(), aggregate="majority",
                 margin_pp=DEFAULT_MARGIN_PP, max_error_rate=DEFAULT_MAX_ERROR_RATE):
    """synthetic: exclude (default; real recall is not inflated) | include | only."""
    synth_all = [r for r in recs if r.get("synthetic") and not r.get("excluded") and not is_err(r)]
    all_recs = recs  # arm list/order always comes from every record
    if synthetic == "exclude":
        recs = [r for r in recs if not r.get("synthetic")]
    elif synthetic == "only":
        recs = [r for r in recs if r.get("synthetic")]
    usable = [r for r in recs if not r.get("excluded") and not is_err(r)]
    # order arms by the run's arm order (arm_idx), not by file order: workers append concurrently
    order = {}
    for r in all_recs:
        order.setdefault(r["arm"], r.get("arm_idx", len(order)))
        order[r["arm"]] = min(order[r["arm"]], r.get("arm_idx", order[r["arm"]]))
    arms = sorted(order, key=lambda x: order[x])
    base = baseline or (arms[0] if arms else None)
    per = {a: [r for r in usable if r["arm"] == a] for a in arms}
    out = {"synthetic_mode": synthetic, "baseline": base, "arms": {}, "paired": {}, "savings": {},
           "aggregate": aggregate, "margin_pp": margin_pp, "max_error_rate": max_error_rate,
           "errors": sum(1 for r in recs if is_err(r)),
           "excluded": sum(1 for r in recs if r.get("excluded"))}
    for a in arms:
        summ = summarize(per[a], aggregate)
        out["arms"][a] = {**run_level(per[a]), "median_tokens": summ["median_tokens"], "prompt": summ}
        ne = sum(1 for r in recs if r["arm"] == a and is_err(r) and not r.get("excluded"))
        out["arms"][a]["error_rate"] = (ne, ne + len(per[a]))
        if synthetic == "exclude":  # shown separately, never mixed into the real numbers
            sp = [p for p in prompts([r for r in synth_all if r["arm"] == a], aggregate).values()
                  if p["label"] == "positive"]
            out["arms"][a]["synthetic_recall_prompt"] = (sum(p["hit"] for p in sp), len(sp))
            sr = [r for r in synth_all if r["arm"] == a and eff_label(r) == "positive"]
            out["arms"][a]["synthetic_recall"] = (sum(is_hit(r) for r in sr), len(sr))
    bm = {c: p["hit"] for c, p in prompts(per[base], aggregate).items() if p["label"] == "positive"} if base else {}
    for a in arms:
        if a == base:
            continue
        am = {c: p["hit"] for c, p in prompts(per[a], aggregate).items() if p["label"] == "positive"}
        common = sorted(set(bm) & set(am))
        only_base = [c for c in common if bm[c] and not am[c]]
        only_arm = [c for c in common if am[c] and not bm[c]]
        sb, sa = skill_rates(per[base], aggregate), skill_rates(per[a], aggregate)
        regress = sorted(((s, sb[s], sa[s]) for s in sb if s in sa and
                          sa[s][0] / max(sa[s][1], 1) < sb[s][0] / max(sb[s][1], 1)),
                         key=lambda x: x[0])
        out["paired"][a] = {"cases": len(common), "only_baseline": len(only_base),
                            "only_arm": len(only_arm),
                            "p": mcnemar_exact(len(only_base), len(only_arm)),
                            "regressions": regress,
                            **paired_noninferiority(len(only_base), len(only_arm), len(common), margin_pp)}
        bad = [x for x in (base, a) if out["arms"][x]["error_rate"][1]
               and out["arms"][x]["error_rate"][0] / out["arms"][x]["error_rate"][1] > max_error_rate]
        out["paired"][a]["invalid_arms"] = bad
        if bad:
            out["paired"][a]["verdict"] = "invalid"
        bmed, amed = case_medians(per[base]), case_medians(per[a])
        out["savings"][a] = {"paired_cases": len(set(bmed) & set(amed)), **(savings(bmed, amed, seed) or {})}
    out["entries"] = entry_table(per, arms, base, aggregate) if base else {}
    add_control_info(out, control_arms(all_recs, controls))
    return out


def render(rep):
    L = []
    L.append("Baseline arm: %s   (errors dropped: %d, excluded by design: %d)" % (
        rep["baseline"], rep["errors"], rep["excluded"]))
    L.append("Synthetic cases: %s" % rep.get("synthetic_mode"))
    L.append("Unit: prompt (reps aggregated by %s); non-inferiority margin %g pp; max provider error rate %.1f%%" % (
        rep.get("aggregate", "majority"), rep.get("margin_pp", DEFAULT_MARGIN_PP),
        100 * rep.get("max_error_rate", DEFAULT_MAX_ERROR_RATE)))
    L.append("")
    L.append("%-14s %-24s %-24s %-22s %-22s %-22s %s" % (
        "arm", "recall", "recall (implicit)", "wrong skill", "fires on negatives", "read-only fires", "median start tok"))
    L.append("(per-arm columns are prompt-level; run-level counts stay in the JSON under recall, wrong, neg_fire)")
    for a, s in rep["arms"].items():
        if s.get("control"):
            a = a + " [control]"
        mt = "%.0f" % s["median_tokens"] if s["median_tokens"] is not None else "n/a"
        pm = s.get("prompt", s)
        L.append("%-14s %-24s %-24s %-22s %-22s %-22s %s" % (
            a, fmt(*pm["recall"]), fmt(*pm["recall_implicit"]), fmt(*pm["wrong"]), fmt(*pm["neg_fire"]),
            fmt(*pm["read_fires"]) if "read_fires" in pm else "-", mt))
        sr = s.get("synthetic_recall_prompt") or s.get("synthetic_recall")
        if sr and sr[1]:
            L.append("%-14s synthetic-only recall (not in the numbers above): %s" % ("", fmt(*sr)))
    L.append("")
    cap = rep.get("max_error_rate", DEFAULT_MAX_ERROR_RATE)
    for a, s in rep["arms"].items():
        ek, en = s.get("error_rate", (0, 0))
        L.append("%-14s provider errors: %s%s" % (a, fmt(ek, en), "  OVER CAP" if en and ek / en > cap else ""))
    for a, p in rep["paired"].items():
        L.append("")
        if p.get("control"):
            a = a + " [control]"
        L.append("Paired %s vs %s over %d positive prompts (reps aggregated by %s): only %s hit: %d, only %s hit: %d, "
                 "exact McNemar p = %.4g" % (a, rep["baseline"], p["cases"], rep.get("aggregate", "majority"), rep["baseline"],
                                             p["only_baseline"], a, p["only_arm"], p["p"]))
        if "verdict" in p:
            L.append("  paired difference %+.1f pp, one-sided 95%% bounds [%.1f, %.1f] vs margin -%g pp: %s%s" % (
                p["diff_pp"], p["lower_pp"], p["upper_pp"], p["margin_pp"], p["verdict"],
                " (error rate over cap in: %s)" % ", ".join(p["invalid_arms"]) if p.get("invalid_arms") else ""))
        for s, b, x in ([] if p.get("control") else p["regressions"]):
            L.append("  regression: %s  %d/%d -> %d/%d" % (s, b[0], b[1], x[0], x[1]))
    for a, v in rep.get("savings", {}).items():
        if "pct" not in v:
            L.append("%s start tokens vs %s: n/a (%d paired cases)" % (a, rep["baseline"], v["paired_cases"]))
        else:
            L.append("%s start tokens vs %s: %+.1f%% [%.1f, %.1f] (geo-mean, 95%% bootstrap, %d cases); "
                     "cheaper on %d/%d, sign p = %.2g" % (
                         a, rep["baseline"], v["pct"], v["ci"][0], v["ci"][1], v["cases"],
                         v["cheaper"], v["cases"], v["p"]))
    ent = rep.get("entries") or {}
    if ent:
        names = list(rep["arms"])
        L.append("")
        L.append("Per-entry recall (expected skill; prompts):")
        L.append("%-28s %s %s" % ("entry", " ".join("%-22s" % n for n in names), "delta(pp) vs baseline / n"))
        for s, e in ent.items():
            cells = " ".join("%-22s" % (fmt(*e["arms"][n]) if n in e["arms"] else "-") for n in names)
            dl = ", ".join("%s %+.0f" % (n, d) for n, d in e["delta_pp"].items()) or "-"
            L.append("%-28s %s %s / n=%d" % (s, cells, dl, e["n"]))
    cal = render_calibration(rep)
    if cal:
        L.append("")
        L.extend(cal)
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("results", help="results.jsonl from run.py")
    ap.add_argument("--baseline", default=None, help="baseline arm name (default: first arm in run order)")
    ap.add_argument("--synthetic", choices=["exclude", "include", "only"], default="exclude",
                    help="treatment of synthetic cases (default exclude; their recall is shown on its own line)")
    ap.add_argument("--seed", type=int, default=0, help="bootstrap seed for the token-savings CI (default 0)")
    ap.add_argument("--control", action="append", default=[], metavar="ARM",
                    help="also treat ARM as a negative control (normally read from the records; repeatable)")
    ap.add_argument("--aggregate", choices=AGGREGATES, default="majority",
                    help="how a prompt's reps collapse to one outcome, fixed before any test (default majority; "
                         "majority is strictly more than half, ties count as miss for recall and as "
                         "fired/wrong for negatives and wrong picks)")
    ap.add_argument("--margin-pp", type=float, default=DEFAULT_MARGIN_PP,
                    help="non-inferiority margin in percentage points of recall (default 5)")
    ap.add_argument("--max-error-rate", type=float, default=DEFAULT_MAX_ERROR_RATE,
                    help="provider error rate per arm above which the verdict is invalid (default 0.05)")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of text")
    a = ap.parse_args(argv)
    rep = build_report(load(a.results), a.baseline, a.synthetic, seed=a.seed, controls=a.control,
                       aggregate=a.aggregate, margin_pp=a.margin_pp, max_error_rate=a.max_error_rate)
    print(json.dumps(rep, indent=2) if a.json else render(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
