# Contributing to AutoDevLoop

Thanks for your interest! AutoDevLoop aims to stay small, dependency-free, and
easy to read.

## Development setup

```bash
git clone <your-fork-url>
cd autodevloop
pip install -e ".[dev]"
pytest
```

## Guidelines

- **No required runtime dependencies.** Keep the core importable with the
  standard library only. Optional extras (e.g. PyYAML) must degrade gracefully.
- **Cross-platform.** Code must work on Windows, macOS, and Linux. Avoid
  POSIX-only assumptions; mind file encodings (UTF-8) and read-only files.
- **Prompts are data, not code.** New agent behaviour should live in editable
  templates (`autodevloop/prompts.py` defaults), not hard-coded f-strings.
- **Keep the pipeline standardised.** Add a step only if it earns its tokens;
  make it toggleable via `pipeline.steps`.
- **Run the smoke test** before sending a PR (see `_smoke/` examples / the test
  suite). Validate both `simple` and `advanced` modes when touching the engine.

## Project layout

| Module | Responsibility |
|---|---|
| `cli.py` | Argument parsing and subcommands |
| `engine.py` | The orchestration loop (phases, value gate, snapshots) |
| `llm.py` | Provider invocation, retry/backoff, cost parsing |
| `prompts.py` | Default prompt templates + rendering |
| `config.py` | Config schema, defaults, mode/step resolution |
| `testing.py` | Built-in test detection and execution |
| `reporting.py` | CHANGELOG / FEATURES / final report |
| `vcs.py` | Optional git commit/tag per version |
| `webapp.py` | The local dashboard (stdlib HTTP server + embedded SPA) |

## Reporting issues

Please include your OS, Python version, provider CLI, and the relevant
`.autodev/logs/` output (redact anything sensitive).
