# Alpha Harness, with Vision

A local-first research workstation for [WorldQuant BRAIN](https://platform.worldquantbrain.com),
plus **Vision**, an agent built into the app that can do anything the UI can do, sees the page
you are on, and knows how a consultant is actually scored.

Upstream is [residual-lab/alpha-harness](https://github.com/residual-lab/alpha-harness) by
MiracleInvoker, MIT licensed. This fork adds the agent; everything else is theirs.

Everything runs on your own machine. Your BRAIN session, your LLM key and your research all
stay in `~/.alpha-harness/`, which is outside this repository. **No credentials are in this
code**, and nothing here is specific to one account: every live figure is read from an
endpoint when it is needed.

## What Vision adds

- **Does the work, not just the talking.** One action per API operation the UI uses, so it can
  sync, search, preview, queue tasks and read any screen. It never submits an Alpha to BRAIN;
  that stays yours, on the platform.
- **Asks before it spends.** Reads run immediately. Anything that writes, simulates or spends
  LLM budget waits for your approval. `/permissions` switches between *Ask first* and *Auto*,
  and only you can switch it: the agent has no action for its own permissions.
- **Works to a goal.** `/goal` sets an objective and a simulation ceiling. The ceiling is
  enforced where a capability actually runs, not merely described in a prompt, so an action
  beyond it is refused rather than trimmed.
- **Knows the scoring.** Pay is pyramid multiplier x Osmosis x Value Factor, not Sharpe. It
  reads your live consultant scoreboard, holds both objectives (points this quarter versus
  standing) and tells you which one a plan serves.
- **Measures instead of guessing.** Read-only SQL over the local catalogue and simulation
  history, so "which near-miss fails one check, and by how little" is arithmetic rather than a
  model reading paged JSON.
- **Remembers.** A research journal of findings, dead ends and decisions, searched before it
  proposes a sweep, because the expensive mistake is re-running ground already covered.

## What you need

- A WorldQuant BRAIN account. Consultant level for the scoreboard; below it that one panel is
  empty and the rest still works.
- An LLM key for Vision and the LLM labs. Add it in **LLM Integration -> Keys**. Several free
  providers are listed there and the app picks a model your key can actually answer for, so you
  do not have to match anyone else's setup.
- Python 3.14 with [uv](https://docs.astral.sh/uv/), and Node with
  [pnpm](https://pnpm.io/) for the frontend.

## Running it

```bash
cd backend && uv run uvicorn alpha_harness.main:app --port 8000
cd frontend && pnpm install && pnpm dev
```

Then open the frontend and sign in to BRAIN. If port 8000 is taken, run the backend elsewhere
and point the frontend at it:

```bash
cd backend && uv run uvicorn alpha_harness.main:app --port 8020
cd frontend && VITE_BACKEND_URL=http://127.0.0.1:8020 pnpm dev
```

Open Vision with the button at the bottom right, or **Cmd/Ctrl + J**. Type `/` for its
commands.

## Licence

MIT, as upstream. See [LICENSE](LICENSE); the copyright notice stays with MiracleInvoker.
