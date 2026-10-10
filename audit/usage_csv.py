"""One reader and one header for the usage CSV that skill-usage.py writes.

Readers should import `load_usage` instead of hard-coding column names: it
accepts the current header, the legacy header (`auto_tool_use`, `typed_slash`,
`sessions`, `override`) and returns rows keyed by the current names, with the
counts already parsed. A CSV with neither header set raises `UsageSchemaError`
naming the columns it found, which beats a bare `KeyError` on the next run.

Writers call `header()` (current names plus the legacy aliases by default) and
`alias_row()` to append the alias values to each row.
"""
import csv

SCHEMA_VERSION = 2

HEADER = ["skill", "model_invocations", "typed_invocations", "invocations",
          "last_used", "description_length", "suggested_tier"]

# legacy name -> current name (schema 1 wrote skill,override,auto_tool_use,typed_slash,sessions,last_used)
LEGACY = {"auto_tool_use": "model_invocations", "typed_slash": "typed_invocations"}
COUNTS = ("model_invocations", "typed_invocations", "invocations")


class UsageSchemaError(ValueError):
    pass


def header(legacy_aliases=True):
    return HEADER + (list(LEGACY) if legacy_aliases else [])


def alias_row(row):
    """Append the legacy alias values to a row written in HEADER order."""
    idx = {n: i for i, n in enumerate(HEADER)}
    return list(row) + [row[idx[cur]] for cur in LEGACY.values()]


def _int(v):
    v = (v or "").strip()
    return int(v) if v else 0


def normalize_row(r):
    """Return a dict keyed by HEADER names with integer counts, from either schema."""
    out = {k: r.get(k, "") for k in HEADER}
    for old, cur in LEGACY.items():
        if not (r.get(cur) or "").strip() and (r.get(old) or "").strip():
            out[cur] = r[old]
    out["model_invocations"] = _int(out["model_invocations"])
    out["typed_invocations"] = _int(out["typed_invocations"])
    out["invocations"] = _int(out["invocations"]) if (out["invocations"] or "").strip() \
        else out["model_invocations"] + out["typed_invocations"]
    return out


def load_usage(path_or_file):
    """{skill: normalized row} from a usage CSV path or open text file."""
    f = open(path_or_file, newline="") if isinstance(path_or_file, str) else path_or_file
    try:
        rd = csv.DictReader(f)
        cols = set(rd.fieldnames or [])
        known = set(HEADER) | set(LEGACY)
        if "skill" not in cols or not (cols & (set(COUNTS) | set(LEGACY))):
            raise UsageSchemaError(
                "not a usage CSV (schema %d expects %s; legacy aliases %s); found columns: %s"
                % (SCHEMA_VERSION, ",".join(HEADER), ",".join(LEGACY), ",".join(sorted(cols - known) or cols)))
        return {r["skill"]: normalize_row(r) for r in rd if r.get("skill")}
    finally:
        if isinstance(path_or_file, str):
            f.close()
