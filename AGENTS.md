# AGENTS.md

SuperMealPlanner is a self-hosted household meal planner: a deterministic 4-week planner, per-person portions, store-aware shopping lists, a server-rendered PWA and a Telegram bot. Python 3.12+, FastAPI, Jinja2, SQLite, managed with uv.

## Commands

- Test: `uv run pytest -q`
- Lint and format: `uv run ruff check . && uv run ruff format --check .`
- Run against a data folder: `uv run smp --data <dir> check | plan --start <monday> --write | serve`

## Rules

- Code never knows a person, a shop, a language or an AI provider. Those live in the data folder (`docs/data-format.md`). Keep every example fictional.
- Every user-facing string goes through `supermealplanner/locales/*.yaml` (English is the source; each other locale has every key, a test checks it).
- The planner stays deterministic: same data and start date, same plan. AI is optional and only answers in the bot.
- Never add logic that cuts food after a binge or changes a medication dose.
- Tests use the synthetic household in `tests/conftest.py`. A new rule needs a test that fails without it.
- Conventional commits. PR titles say why, not what.
