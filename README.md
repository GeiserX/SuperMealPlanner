<p align="center">
  <img src="docs/images/banner.svg" alt="SuperMealPlanner" width="100%">
</p>

<h1 align="center">SuperMealPlanner</h1>

<p align="center">
  <a href="https://github.com/GeiserX/SuperMealPlanner/releases"><img src="https://img.shields.io/github/v/release/GeiserX/SuperMealPlanner?style=flat-square" alt="Release"></a>
  <a href="https://github.com/GeiserX/SuperMealPlanner/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/GeiserX/SuperMealPlanner/ci.yml?style=flat-square&label=CI" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/GeiserX/SuperMealPlanner?style=flat-square" alt="License"></a>
</p>

SuperMealPlanner is a self-hosted meal planner for a household that eats the same food but needs different amounts. It plans four weeks of real food around the cook's calendar, sizes every plate to each person's own targets, writes the shopping list for your supermarket, and runs a Telegram bot for the family group.

## Features

- One recipe, one pot, a plate per person: quantities scale to each person's calorie target.
- A 4-week plan, batch-cooked on free afternoons read from a private iCal address.
- Weekly rules: legumes, fish, oily fish, a salad every day, no dish repeated too often.
- Shopping list per store, with the real product, packs to buy and an estimated total.
- Targets that follow each person's weight trend, with floors and doctor flags.
- GLP-1 aware: lighter dishes and smaller plates in the days after a weekly injection.
- Telegram bot: today's plates, the week, the list, logging, reminders and voice notes. Always answers in private, and in the group when you mention it or reply to it.
- An assistant that remembers the conversation: one ongoing conversation per chat, `/new` to start over.
- Change settings from the chat: reminders, batch days, guests, foods to avoid and more, applied only after a yes and saved as a git commit you can revert.
- Bring your own AI: any OpenAI-compatible endpoint, Anthropic, or a plugin file that can keep the conversation.
- Every string is translatable; English and Spanish ship today.

## Quick start

```bash
git clone https://github.com/GeiserX/SuperMealPlanner && cd SuperMealPlanner
uv run smp --data ./my-household plan --start 2026-10-12 --no-calendar --write
docker run -p 8080:8080 -v "$PWD/my-household:/data" drumsergio/supermealplanner:0.4.0
```

Open http://localhost:8080 and add it to your phone's home screen. The data folder holds `household.yaml`, `ingredients.yaml` and `recipes/`; the full format, with a complete example, is in [docs/data-format.md](docs/data-format.md). Keep that folder in a private repository: it holds health details.

## Documentation

- [Data format](docs/data-format.md): every file in the data folder, settings, store catalogues, reminders, AI and bot
- [How it works](docs/how-it-works.md): the planner, portions, targets, the bot

## Not medical advice

SuperMealPlanner helps you plan and cook. It does not treat any condition, eating disorders included. It never changes a medication and never cuts food after a binge. Targets and rules come from the household file you write: check them with your clinician.

## License

[AGPL-3.0-or-later](LICENSE)
