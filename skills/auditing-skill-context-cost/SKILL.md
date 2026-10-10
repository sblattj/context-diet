---
name: auditing-skill-context-cost
description: "Find which skills to fully list, list by name only, or hide (skillOverrides), from real usage in session logs, plus descriptions over the 600-char cap. Use for: skill diet, which skills can I hide, trim skill descriptions."
---

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/audit/skill-usage.py --all-projects -o /tmp/usage.csv     # interactive sessions only (--include-headless to count -p runs); default scope is the current project
python3 ${CLAUDE_PLUGIN_ROOT}/audit/audit.py /tmp/usage.csv              # suggested skillOverrides changes, read-only
python3 ${CLAUDE_PLUGIN_ROOT}/audit/skill-lint.py ~/.claude/skills --settings ~/.claude/settings.json   # fully listed skills over 600 chars
```

- Built-in skills (/loop, /init, dataviz...) obey skillOverrides too but aren't on disk; find them in the `skill_listing` block.
- A skill a CLAUDE.md line tells Claude to load must stay at least `name-only`.
- Never name a hidden skill in a listed description; say "the user command /x".
- Prove a change with the A/B before keeping it (ab-testing-whether-skills-still-fire).
