---
name: ab-testing-whether-skills-still-fire
description: "A/B whether skills still get picked after changing skillOverrides, descriptions or skill names, on real prompts mined from your session logs: recall, wrong pick, false fires, start tokens, McNemar. Use for: do my skills still trigger, test name-only, rename a skill."
---

Pipeline in `${CLAUDE_PLUGIN_ROOT}/ab/` (see its README): mine, filter, make_arms, synth, run, report.
Rename arms: `make_rename_arms.py` (names are the whole signal for name-only skills).

- Write test prompts with a writer that never saw the change, or the new arm wins by construction.
- Label noise is the main risk: read negatives that fire before trusting the false-fire rate.
- Rename only opaque names. Clear names lose ground when renamed, and old names stop working as /commands.
- Every run is a real model call; unset ANTHROPIC_API_KEY to use your subscription (run.py strips it by default).
