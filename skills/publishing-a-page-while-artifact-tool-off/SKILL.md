---
name: publishing-a-page-while-artifact-tool-off
description: "Publish or update a claude.ai Artifact page when the Artifact tool is off to save ~12-16k start tokens: a throwaway side session publishes the file and prints the URL. Use for: No such tool available: Artifact, publish this page, update the artifact."
---

```bash
${CLAUDE_PLUGIN_ROOT}/tools/publish-artifact.sh page.html                 # new artifact, prints URL (~17s)
${CLAUDE_PLUGIN_ROOT}/tools/publish-artifact.sh page.html <artifact-url>  # update in place
${CLAUDE_PLUGIN_ROOT}/tools/artifacts.sh status                           # how Artifact is switched off
```

- Keep it off with `export CLAUDE_CODE_DISABLE_ARTIFACT=1` in the shell rc. Never `enableArtifact: false` in settings.json: nothing overrides it, and live sessions hot-reload it.
- Env: `PUBLISH_CWD` (a trusted dir), `LAUNCH_SHELL="zsh -ic"` if `claude` is a shell function.
- No comment auto-replies: the side session exits after publishing.
