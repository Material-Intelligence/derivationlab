# Derivation Web

Tree-first React/TypeScript client for autonomous scientific derivation runs.

## Run locally

```bash
cd src/derivation_web
npm ci
npm run dev
```

The product build uses the same-origin HTTP API by default. Open `/?empty=1` to start persistent AI Problem Intake. The backend owns one recoverable IntakeSession and returns every currently independent blocking question as one frontier. The page renders the questions as option/custom-answer cards and submits the whole frontier together. `?intake=<session_id>` restores the same session after refresh or backend restart; unfinished sessions are discoverable in a collapsed list. The complete specification and advanced runtime settings stay collapsed until requested.

Fixture mode is explicit and never calls a backend. It exposes the tree and run catalog fixture but intentionally does not pretend to provide AI intake:

```bash
VITE_API_MODE=fixture npm run dev
```

To target the FastAPI service:

```bash
VITE_API_BASE_URL=http://127.0.0.1:8000 npm run dev
```

The typed adapter uses:

- `GET /api/runs` (catalog)
- `GET/POST /api/intake/sessions`
- `GET /api/intake/sessions/:sessionId`
- `POST /api/intake/sessions/:sessionId/rounds`
- `POST /api/intake/sessions/:sessionId/confirm`
- `POST /api/intake/sessions/:sessionId/cancel`
- `POST /api/runs`
- `GET /api/runs/:runId`
- `GET /api/runs/:runId/events` (SSE)
- `POST /api/runs/:runId/pause`
- `POST /api/runs/:runId/resume`
- `POST /api/runs/:runId/interrupt`
- `POST /api/runs/:runId/branches`

## Verify

```bash
npm test
npm run api:check
npm run typecheck
npm run lint
npm run build
```

`npm run api:check` regenerates the typed client from `src/derivation_api/openapi.json` in a temporary directory and fails if it differs from `src/api/generated/`.

Two browser smokes are manual and need an installed Chrome/Edge browser (or `CHROME_PATH`): `npm run smoke:fixture` drives the fixture UI and writes screenshots to `runs/smoke/fixture-smoke/`; `node scripts/server-auth-smoke.mjs` needs a prior `npm run build` and writes to `runs/smoke/server-auth-browser/`. Set `SMOKE_EVIDENCE_DIR` to a repo-relative path to redirect them. `runs/` is ignored by git.

## Interaction contract

- The run catalog remains visible on desktop and becomes a drawer on narrow screens; changing runs replaces `?run=` and closes the old SSE subscription.
- Read-only validation runs expose the same tree and route reader without pause, resume, interrupt, or branch commands.
- Problem Intake is a persistent backend-owned decision frontier rather than a fixed questionnaire. The complete versioned specification is inspectable but collapsed by default.
- The complete tree remains the primary canvas; the reader renders the whole current route as one continuous document, headed by the run question with the route label, status and size as its subtitle.
- Clicking a node or edge resolves a route in this order: keep the route already being read when it still contains the target, then the direction remembered for that fork, then the canonical order by `status_history[0].seq`, then `branch_id`.
- Selecting a route pill remains an explicit override.
- Forks are chosen in the prose. The picker under a step groups `continuation`/`model_fork`/`human_direction` as directions and `human_revision` as revisions of that same step; `proposed` edges never enter the reading model. After a switch only the tail of the document is rebuilt and the viewport stays on the fork.
- Reading focus collapses the tree rail to a 24px strip and gives the desk the main area; the compact (<=760px) workbench stays tab-driven instead.
- Titles are balanced before rendering: a server-truncated title is cut at its first unclosed `\[`, `\(`, `$$` or `$` and ends in an ellipsis, and every surface that cannot host KaTeX (tree nodes, breadcrumbs, fork options, copy-title) renders closed formulas as readable plain text.
- `?theme=paper|light` pins a palette for one session; the Settings menu (Appearance) persists the choice, defaulting to warm paper.
- `?run=stress-run` loads the wide fixture run (deep tree, five-way fork, long formulas) in `VITE_API_MODE=fixture`; `?run=demo-run` is the three-route fixture.
- Dense five-field/audit evidence opens only on request.
- Human branches are available only from sealed revisions in a review-ready phase.
- `human_direction` sends free text; `human_revision` edits and serializes exactly `claim`, `why`, `source`, `derivation`, and `scope` as canonical JSON.
- SSE overlays describe in-flight calls without replacing the last complete canonical snapshot.
