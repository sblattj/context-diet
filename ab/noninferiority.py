#!/usr/bin/env python3
"""Is this cut safe? A pre-registrable non-inferiority verdict for a baseline vs a candidate arm.

Rows are paired by (case, rep): two results files (BASE then ARM), or one file holding both arms
interleaved and split by --arm-field. Errored and excluded rows are dropped; a later usable row for
the same key wins. Cases in --exclude-cases are dropped (a frozen floor list, say).

Expected skills split into visible and hidden. Hidden = names given with --hidden / --hidden-file
plus every command file under --commands-dir whose text holds --hidden-marker (default
"disable-model-invocation: true"); the name is the path below the dir minus ".md", "/" -> ":".

Metrics (delta = arm minus baseline over paired rows, in percentage points):
  recall_visible       positives with at least one visible expected skill (primary)
  recall_pure_visible  the same, positives with no hidden expected skill (informational by default)
  recall_mixed         the same, positives with a hidden expected skill too (informational by default)
  wrong_pick           positives where skills fired but none was expected (lower is better)
  false_fire           negatives (label negative or expect_none) where any skill fired (lower is better)
Recall scoring is conservative: a baseline row hits when any expected skill fired, an arm row only
when a visible expected skill fired.

Rule: KEEP iff every decision metric passes. Recall-type metrics pass when the CI lower bound is
>= -margin; wrong_pick and false_fire pass when the CI lower bound of the increase is <= their margin
(default 0, i.e. "not worse beyond the CI"). Defaults: decide on recall_visible (margin 5 pp),
wrong_pick and false_fire (margin 0), two-sided alpha 0.05 (the lower bound of a 95% CI).

CI: normal approximation on the paired difference with a case-clustered (sandwich) standard error,
so reps of one case are not counted as independent evidence; --cluster none treats every pair as
independent. A zero variance falls back to one pseudo-discordant pair. Fewer than 10 discordant
pairs on a decision metric adds a warning.

Pre-registration: --rule FILE reads the rule (alpha, sides, cluster, metrics, optional
exclude_cases and hidden) from JSON and records its sha256; flags that the file fixes are refused.
--rule-sha256 HEX makes a changed file a hard failure (keep false, exit 2).
Exit: 0 keep, 1 park, 2 usage error or rule mismatch.
"""
import argparse
import glob
import hashlib
import json
import math
import os
import statistics
import sys
from collections import defaultdict

from report import case_medians, eff_label, is_err, load, mcnemar_exact, paired_noninferiority, savings
from run import skill_matches

METRICS = {  # name -> (direction, default margin pp)
    "recall_visible": ("higher", 5.0),
    "recall_pure_visible": ("higher", 5.0),
    "recall_mixed": ("higher", 5.0),
    "wrong_pick": ("lower", 0.0),
    "false_fire": ("lower", 0.0),
}
DEFAULT_DECIDE = ("recall_visible", "wrong_pick", "false_fire")
DEFAULT_MARKER = "disable-model-invocation: true"
MIN_DISCORDANT = 10
RULE_KEYS = {"alpha", "sides", "cluster", "metrics", "exclude_cases", "hidden"}


class RuleError(Exception):
    pass


def hidden_from_commands(root, marker=DEFAULT_MARKER):
    """Command names under root whose file text contains marker."""
    out = set()
    for f in glob.glob(os.path.join(root, "**", "*.md"), recursive=True):
        with open(f, encoding="utf-8", errors="replace") as fh:
            if marker in fh.read():
                out.add(os.path.relpath(f, root)[:-3].replace(os.sep, ":"))
    return out


def read_list(path):
    """First tab/whitespace column of each non-blank, non-# line."""
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.strip() and not line.lstrip().startswith("#"):
                out.append(line.split()[0])
    return out


def keyed(recs):
    """(case, rep) -> last usable record."""
    out = {}
    for r in recs:
        if is_err(r) or r.get("excluded"):
            continue
        out[(r["case"], r.get("rep", 0))] = r
    return out


def split_arms(recs, field="arm", base=None, arm=None):
    """Split one interleaved file into (base_name, base_recs, arm_name, arm_recs)."""
    order = {}
    for i, r in enumerate(recs):
        v = r.get(field)
        if v is None:
            continue
        rank = (r.get("arm_idx", i), i)
        order[v] = min(order.get(v, rank), rank)
    names = sorted(order, key=lambda x: order[x])
    if base is None and arm is None:
        if len(names) != 2:
            raise RuleError("one results file needs exactly two values of %r (found %s); name them with "
                            "--base-arm and --arm" % (field, names or "none"))
        base, arm = names
    elif base is None or arm is None:
        rest = [n for n in names if n not in (base, arm)]
        if len(rest) != 1:
            raise RuleError("give both --base-arm and --arm (values of %r: %s)" % (field, names))
        base, arm = (rest[0], arm) if base is None else (base, rest[0])
    for n in (base, arm):
        if n not in order:
            raise RuleError("no rows with %s=%r (values: %s)" % (field, n, names))
    return base, [r for r in recs if r.get(field) == base], arm, [r for r in recs if r.get(field) == arm]


def one_value(recs, field, default):
    """The file's single arm value, else default."""
    vals = {r.get(field) for r in recs} - {None}
    return vals.pop() if len(vals) == 1 else default


def hit(skills, names):
    return any(skill_matches(f, e) for f in skills for e in names)


def outcome_pairs(B, A, hidden, exclude):
    """metric -> list of (case, base_bool, arm_bool) over paired keys."""
    hid = {h.lower() for h in hidden}
    out = defaultdict(list)
    for k in sorted(set(B) & set(A)):
        b, a = B[k], A[k]
        case = k[0]
        if case in exclude:
            continue
        bs, as_ = b.get("skills") or [], a.get("skills") or []
        if eff_label(b) == "negative":
            out["false_fire"].append((case, bool(bs), bool(as_)))
            continue
        if eff_label(b) != "positive":
            continue
        exp = list(b.get("expected") or [])
        out["wrong_pick"].append((case, bool(bs) and not hit(bs, exp), bool(as_) and not hit(as_, exp)))
        vis = [e for e in exp if e.lower() not in hid]
        if not vis:
            continue
        row = (case, hit(bs, exp), hit(as_, vis))
        out["recall_visible"].append(row)
        out["recall_mixed" if len(vis) < len(exp) else "recall_pure_visible"].append(row)
    return out


def cluster_se(rows):
    """Case-clustered SE of the mean paired difference; 0.0 when undefined."""
    n = len(rows)
    m = sum(a - b for _, b, a in rows) / n
    tot = defaultdict(float)
    for c, b, a in rows:
        tot[c] += (a - b) - m
    g = len(tot)
    if g < 2:
        return 0.0
    return math.sqrt(g / (g - 1) * sum(v * v for v in tot.values())) / n


def metric_stats(rows, direction, margin_pp, z, cluster):
    n = len(rows)
    bk, ak = sum(b for _, b, _ in rows), sum(a for _, _, a in rows)
    only_b = sum(1 for _, b, a in rows if b and not a)
    only_a = sum(1 for _, b, a in rows if a and not b)
    st = {"n": n, "base": bk, "arm": ak, "only_base": only_b, "only_arm": only_a,
          "discordant": only_b + only_a, "direction": direction, "margin_pp": margin_pp,
          "mcnemar_p": mcnemar_exact(only_b, only_a)}
    if n == 0:
        st.update(delta_pp=None, ci_pp=[None, None], pass_=False)
        return st
    d = (only_a - only_b) / n
    se = cluster_se(rows) if cluster == "case" else 0.0
    if se > 0:
        lo, hi = 100 * (d - z * se), 100 * (d + z * se)
    else:  # report.py's paired Wald (one pseudo-discordant pair keeps se > 0)
        w = paired_noninferiority(only_b, only_a, n, margin_pp, z=z)
        lo, hi = w["lower_pp"], w["upper_pp"]
    ok = lo >= -margin_pp if direction == "higher" else lo <= margin_pp
    st.update(delta_pp=100 * d, ci_pp=[lo, hi], pass_=bool(ok))
    return st


def file_sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def load_rule(path, expect_sha=None):
    sha = file_sha(path)
    if expect_sha and sha != expect_sha.lower():
        raise RuleError("rule file %s has sha256 %s, pre-registered %s: the rule changed" % (path, sha, expect_sha))
    with open(path, encoding="utf-8") as fh:
        rule = json.load(fh)
    bad = set(rule) - RULE_KEYS
    if bad:
        raise RuleError("unknown rule keys: %s" % ", ".join(sorted(bad)))
    for m in rule.get("metrics") or {}:
        if m not in METRICS:
            raise RuleError("unknown metric in rule: %s" % m)
    return rule, sha


def normalize_rule(alpha=0.05, sides=2, cluster="case", metrics=None):
    if not 0 < alpha < 1 or sides not in (1, 2) or cluster not in ("case", "none"):
        raise RuleError("bad rule: alpha in (0,1), sides 1 or 2, cluster case|none")
    if metrics is None:
        metrics = {m: {} for m in DEFAULT_DECIDE}
    out = {}
    for m, spec in metrics.items():
        if m not in METRICS:
            raise RuleError("unknown metric: %s" % m)
        out[m] = {"margin_pp": float((spec or {}).get("margin_pp", METRICS[m][1]))}
    return {"alpha": alpha, "sides": sides, "cluster": cluster, "metrics": out}


def verdict(base_recs, arm_recs, rule, hidden=(), exclude=(), names=("base", "arm"), seed=0):
    z = statistics.NormalDist().inv_cdf(1 - rule["alpha"] / rule["sides"])
    B, A = keyed(base_recs), keyed(arm_recs)
    exclude = set(exclude)
    pairs = outcome_pairs(B, A, hidden, exclude)
    metrics, reasons, warnings = {}, [], []
    for m, (direction, default_margin) in METRICS.items():
        decide = m in rule["metrics"]
        margin = rule["metrics"][m]["margin_pp"] if decide else default_margin
        st = metric_stats(pairs.get(m, []), direction, margin, z, rule["cluster"])
        st["decides"] = decide
        st["pass"] = st.pop("pass_")
        metrics[m] = st
        if not decide:
            continue
        if st["n"] == 0:
            reasons.append("%s: no paired rows" % m)
        elif not st["pass"]:
            if direction == "higher":
                reasons.append("%s: lower bound %.1f pp < -%g pp margin" % (m, st["ci_pp"][0], margin))
            else:
                reasons.append("%s: increase lower bound %.1f pp > %g pp margin" % (m, st["ci_pp"][0], margin))
        if st["n"] and st["discordant"] < MIN_DISCORDANT:
            warnings.append("%s: only %d discordant pairs; the normal approximation is rough here"
                            % (m, st["discordant"]))
    toks = {}
    for nm, D in zip(names, (B, A)):
        s = [r["start_tokens"] for r in D.values() if r.get("start_tokens")]
        toks[nm] = statistics.median(s) if s else None
    sav = savings(case_medians(B.values()), case_medians(A.values()), seed)
    return {"keep": not reasons, "reasons": reasons, "warnings": warnings,
            "base_arm": names[0], "arm": names[1],
            "n": metrics["recall_visible"]["n"], "metrics": metrics, "rule": rule,
            "hidden": sorted(hidden), "excluded_cases": sorted(exclude),
            "excluded_rows": sum(1 for k in set(B) & set(A) if k[0] in exclude),
            "unpaired": {"base_only": len(set(B) - set(A)), "arm_only": len(set(A) - set(B))},
            "median_start_tokens": toks, "savings": sav}


def render(v):
    r = v["rule"]
    L = ["%s: %s vs %s (base) | alpha %g, %d-sided, cluster %s" % (
        "KEEP" if v["keep"] else "PARK", v["arm"], v["base_arm"], r["alpha"], r["sides"], r["cluster"])]
    if v.get("rule_sha256"):
        L.append("rule %s sha256 %s" % (v.get("rule_file", ""), v["rule_sha256"]))
    L.append("hidden skills: %d; excluded cases: %d (%d paired rows); unpaired base %d, arm %d" % (
        len(v["hidden"]), len(v["excluded_cases"]), v["excluded_rows"],
        v["unpaired"]["base_only"], v["unpaired"]["arm_only"]))
    L.append("")
    L.append("%-20s %5s %6s %6s %9s %18s %7s  %s" % ("metric", "n", "base", "arm", "delta pp", "CI pp", "margin", "result"))
    for m, s in v["metrics"].items():
        if s["n"] == 0:
            L.append("%-20s %5d %6s %6s %9s %18s %7s  %s" % (m, 0, "-", "-", "-", "-", "-",
                                                         "no rows" + (" (decides)" if s["decides"] else "")))
            continue
        res = ("pass" if s["pass"] else "FAIL") if s["decides"] else "info"
        L.append("%-20s %5d %6d %6d %+9.1f %18s %7s  %s" % (
            m, s["n"], s["base"], s["arm"], s["delta_pp"], "[%+.1f, %+.1f]" % tuple(s["ci_pp"]),
            ("-%g" if s["direction"] == "higher" else "+%g") % s["margin_pp"], res))
    L.append("")
    t = v["median_start_tokens"]
    L.append("median start tokens: %s" % ", ".join("%s %s" % (k, "n/a" if x is None else "%.0f" % x) for k, x in t.items()))
    sv = v.get("savings")
    if sv:
        L.append("start tokens %s vs %s: %+.1f%% [%.1f, %.1f] (geo-mean, 95%% bootstrap, %d cases)" % (
            v["arm"], v["base_arm"], sv["pct"], sv["ci"][0], sv["ci"][1], sv["cases"]))
    for w in v["warnings"]:
        L.append("warning: " + w)
    for x in v["reasons"]:
        L.append("park: " + x)
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("results", nargs="+", help="BASE.jsonl ARM.jsonl, or one file holding both arms")
    ap.add_argument("--arm-field", default="arm", help="record field naming the arm (default arm)")
    ap.add_argument("--base-arm", help="baseline arm value (one-file mode, or to filter two files)")
    ap.add_argument("--arm", help="candidate arm value")
    ap.add_argument("--rule", help="pre-registered rule JSON; its sha256 is recorded")
    ap.add_argument("--rule-sha256", help="expected sha256 of --rule; a mismatch fails the verdict")
    ap.add_argument("--alpha", type=float, help="CI alpha (default 0.05)")
    ap.add_argument("--sides", type=int, choices=(1, 2), help="1 or 2 sided CI (default 2)")
    ap.add_argument("--cluster", choices=("case", "none"), help="SE clustering (default case)")
    ap.add_argument("--decide", help="comma-separated decision metrics (default %s; choices %s)" % (
        ",".join(DEFAULT_DECIDE), ",".join(METRICS)))
    ap.add_argument("--margin-pp", type=float, help="margin for recall metrics (default 5)")
    ap.add_argument("--harm-margin-pp", type=float, help="margin for wrong_pick and false_fire (default 0)")
    ap.add_argument("--exclude-cases", action="append", default=[], metavar="FILE",
                    help="case ids to drop, first column per line, # comments (repeatable)")
    ap.add_argument("--hidden", action="append", default=[], metavar="NAME", help="hidden skill name (repeatable)")
    ap.add_argument("--hidden-file", action="append", default=[], metavar="FILE", help="hidden names, one per line")
    ap.add_argument("--commands-dir", action="append", default=[], metavar="DIR",
                    help="scan DIR/**/*.md for --hidden-marker (repeatable)")
    ap.add_argument("--hidden-marker", default=DEFAULT_MARKER, help="text marking a hidden command file")
    ap.add_argument("--seed", type=int, default=0, help="bootstrap seed for the token-savings CI")
    ap.add_argument("--json", action="store_true", help="emit the JSON verdict")
    a = ap.parse_args(argv)
    try:
        if len(a.results) > 2:
            raise RuleError("give one or two results files")
        sha = None
        exclude = set()
        for f in a.exclude_cases:
            exclude |= set(read_list(f))
        hidden = set(a.hidden)
        for f in a.hidden_file:
            hidden |= set(read_list(f))
        for d in a.commands_dir:
            hidden |= hidden_from_commands(d, a.hidden_marker)
        if a.rule:
            raw, sha = load_rule(a.rule, a.rule_sha256)
            fixed = [f for f in ("alpha", "sides", "cluster", "decide", "margin_pp", "harm_margin_pp")
                     if getattr(a, f) is not None]
            if "exclude_cases" in raw and a.exclude_cases:
                fixed.append("exclude_cases")
            if "hidden" in raw and (a.hidden or a.hidden_file or a.commands_dir):
                fixed.append("hidden")
            if fixed:
                raise RuleError("the rule file fixes these; drop the flags: %s" % ", ".join(fixed))
            rule = normalize_rule(raw.get("alpha", 0.05), raw.get("sides", 2), raw.get("cluster", "case"),
                                  raw.get("metrics"))
            exclude |= set(raw.get("exclude_cases") or [])
            hidden |= set(raw.get("hidden") or [])
        elif a.rule_sha256:
            raise RuleError("--rule-sha256 needs --rule")
        else:
            decide = [m.strip() for m in a.decide.split(",") if m.strip()] if a.decide else list(DEFAULT_DECIDE)
            ms = {}
            for m in decide:
                if m not in METRICS:
                    raise RuleError("unknown metric: %s" % m)
                over = a.margin_pp if METRICS[m][0] == "higher" else a.harm_margin_pp
                ms[m] = {} if over is None else {"margin_pp": over}
            rule = normalize_rule(0.05 if a.alpha is None else a.alpha, a.sides or 2, a.cluster or "case", ms)
        if len(a.results) == 2:
            br, ar = load(a.results[0]), load(a.results[1])
            if a.base_arm:
                br = [r for r in br if r.get(a.arm_field) == a.base_arm]
            if a.arm:
                ar = [r for r in ar if r.get(a.arm_field) == a.arm]
            names = (a.base_arm or one_value(br, a.arm_field, "base"), a.arm or one_value(ar, a.arm_field, "arm"))
        else:
            bn, br, an, ar = split_arms(load(a.results[0]), a.arm_field, a.base_arm, a.arm)
            names = (bn, an)
    except (RuleError, OSError, ValueError) as e:
        out = {"keep": False, "reasons": ["error: %s" % e]}
        print(json.dumps(out, indent=2) if a.json else "ERROR: %s" % e)
        return 2
    v = verdict(br, ar, rule, hidden, exclude, names, a.seed)
    if a.rule:
        v["rule_file"], v["rule_sha256"] = os.path.basename(a.rule), sha
    print(json.dumps(v, indent=2) if a.json else render(v))
    return 0 if v["keep"] else 1


if __name__ == "__main__":
    sys.exit(main())
