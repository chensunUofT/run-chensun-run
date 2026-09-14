# Agent management

This project uses [donvito/codex-astra-luna-orchestrator](https://github.com/donvito/codex-astra-luna-orchestrator), pinned to commit `642b16074ba8973d4f920ad8cbfb542bde3b4682`.

The upstream Pro profile was copied into `.codex`, `.agents`, and `AGENTS.md` using the manual project-local installation described by upstream. No global Codex settings were changed. This selects the Astra orchestration topology; it does not change an account subscription.

- Root: architecture, contracts, Windows tooling, integration and verification.
- Luna backend worker: `backend/`.
- Luna frontend worker: `frontend/`.
- Astra reviewer: independent review after integration.

Current-session workers are explicitly spawned with the upstream model settings. Project config applies when Codex opens this directory as a trusted project; copying files does not change an already-running root model.

Upstream license is retained in `ORCHESTRATOR-LICENSE`.
