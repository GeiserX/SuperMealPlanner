# Data format

SuperMealPlanner keeps code and household data apart. The app reads one data directory (`SMP_DATA`, default `/data`):

```
household.yaml        people, targets, pills, food rules
settings.yaml         how the app behaves: language, stores, bot, AI provider, reminders (optional)
ingredients.yaml      ingredient catalogue
recipes/*.yaml        one recipe per file
stores/*.yaml         store catalogues: the real product to buy per ingredient (optional)
state/                written by the app: plans/<monday>.yaml, paused.yaml and supermealplanner.db
```

Keep the data directory in its own private repository: it holds health details. Ids are lowercase kebab-case ASCII. Food names, steps and units are in whatever language you cook in.

## Portion model

Every recipe quantity is for **one reference serving**: what a person eating 2,000 kcal a day puts on the plate. Each person's factor is their calorie target divided by 2,000, so one pot feeds everyone with different plates. The shopping list multiplies by the sum of factors, plus guests.

## household.yaml

```yaml
household: The Example Household
timezone: Europe/Madrid
locale: en                    # fallback language; settings.yaml can override
reference_kcal: 2000
lunch_time: "14:00"
shopping_day: monday
guests: {sunday_lunch: 0}     # extra reference servings at Sunday lunch
calendar:
  ics_env: CALENDAR_ICS_URL   # env var holding a secret iCal address; optional
  busy_afternoon: ["16:00", "20:00"]   # an event overlapping this window makes the afternoon busy
batch:
  preferred_days: [monday, friday, saturday]
  sessions_per_week: 2
people:
  - id: alex
    name: Alex
    sex: male
    birth_year: 1990
    height_cm: 178
    activity_factor: 1.4
    goal: {mode: lose, target_kg: 78, band_kg: [77, 79], max_loss_kg_week: 0.5, waist_cm_max: 89}
    kcal: {start: 2200, floor: 1800, step: 150, every_weeks: 4}
    protein_g: {target: 110, floor: 95}
    fluid_l: [2.5, 3.0]
    weigh_every: week
    glp1: {drug: semaglutide, day: monday, early_week_factor: 0.95, late_week_factor: 1.05}
    packs: [glp1, binge-eating-safe]
    avoid_ingredients: [grapefruit]
    avoid_tags_at_dinner: [cold-soup, fried]
    pills:                    # shown next to the meal they go with; the app never changes a dose
      waking: []
      breakfast: [{name: Vitamin D3, dose: 2000 IU}]
      lunch: [{name: Omega-3, dose: 1 softgel}]
      dinner: []
      bedtime: []
    notes: ["Drinks more water on hot days"]   # yours, by hand; the chat never changes them
    chat_notes: []              # what the household told the assistant; it adds and removes these
  - id: sam
    name: Sam
    sex: female
    birth_year: 1992
    height_cm: 165
    goal: {mode: maintain, band_kg: [58, 61]}
    kcal: {start: 1800, floor: 1500, step: 100}
    protein_g: {target: 90, floor: 70}
rules:                        # per 7 days; the plan validator enforces them
  legumes_min: 4
  fish_min: 3
  oily_fish_min: 2
  salad_daily: true
  max_repeats_per_4_weeks: 2
```

`goal.mode` is `lose`, `maintain` or `gain`. `glp1` is optional: with it, plates are a little smaller for the three days after the weekly injection and a little larger from day 4, and lighter dishes are preferred early in the week.

## settings.yaml

Everything here is optional.

```yaml
locale: es                    # en, es; add a file to supermealplanner/locales to add a language
base_url: https://meals.example.org   # used for links in bot messages
stores:
  supermarket: {name: Supermarket, catalog: stores/supermarket.yaml}
  online: {name: Online shop, catalog: stores/online.yaml}
shopping:
  primary: supermarket        # the list is written for this store
  alternatives: [online]      # shown as "or at ..." and as its own list view
sections: {butcher: Butcher, greengrocer: Greengrocer}   # labels for ingredient sections
toppers:                      # suggested when a day falls short on protein
  - {label: "1 Greek yogurt", protein_g: 7, kcal: 120}
bot:
  enabled: true
  token_env: TELEGRAM_BOT_TOKEN
  chat_id: -1001234567890     # the household group; send /chatid in the group to get it
  people: {alex: 111111111, sam: 222222222}   # person id -> Telegram user id
  handles: {sam: "@sam_cooks"}  # optional: how to @mention someone; looked up from Telegram when left out
  admins: [alex]              # optional: whose yes approves what only an admin may (a plugin's pull request)
  voice: true                 # transcribe voice notes; in the group only one that replies to the bot
  private_chats: true         # people in bot.people may also write to the bot privately
  group_open: false           # true: anyone in the group may use the bot, not only bot.people
  sync_profile: true          # at start, set the bot's command menu and descriptions in Telegram
  share_logs_in_group: false  # true: the AI may discuss weigh-ins, binges, cravings and notes in the group
  history_scope: asked        # asked | all: which group messages are kept as AI context
  history: 20
llm:
  provider: openai            # none | openai | anthropic | plugin
  base_url: http://ollama:11434/v1    # any OpenAI-compatible endpoint
  model: llama3.1
  api_key_env: LLM_API_KEY
  instructions: "Answer like a friendly home cook."
  timeout_s: 600
transcription:                # voice notes; optional
  provider: openai            # none | openai (any /audio/transcriptions endpoint) | plugin
  base_url: http://whisper:8000/v1
  model: whisper-1
  language: en
  timeout_s: 600
reminders:
  - {id: water, kind: message, text: "💧 Water, {names}!", at: ["11:00", "17:00"], people: [alex]}
  - {id: weigh-in, kind: message, text: "⚖️ Weekly weigh-in", at: ["08:30"], days: [monday]}
  - {id: shopping, kind: shopping, at: ["09:00"], days: [monday]}
  - {id: batch, kind: batch, at: ["12:00"]}
  - {id: defrost, kind: defrost, at: ["21:00"]}
  - {id: lunch-pills, kind: pills_lunch, at: ["16:00"]}
```

Reminder times are `HH:MM` in the household timezone; a bad time or an unknown `{placeholder}` is rejected when the data loads. Reminder kinds: `message` sends its text (`{names}` and `{today}` are filled in); `meal` sends today's `meal` (breakfast, lunch or dinner) with where it comes from and the recipe link; `shopping` links the week's list; `batch` lists today's batch dishes, only on batch days; `defrost` fires only when tomorrow's lunch is in the freezer; `pills_lunch` asks each person with lunch pills who has not logged them yet.

The `plugin` provider loads a Python file from the data directory that defines `complete(system, messages, settings) -> str` (sync or async). Use it for any model or gateway the built-in providers do not cover.

A plugin can also keep the conversation on its side, so each message sends only what is new instead of the whole history. It then defines `converse(turn, settings)` and, optionally, `forget(conversation, settings)`, which `/new` calls:

| `turn` key | what it holds |
|---|---|
| `conversation` | the id the plugin returned last time in this chat, or `None` |
| `rules` | the instructions; they stay the same from one message to the next |
| `context` | the household's data as of now: people, plates, the week, recent logs, the settings a change may touch |
| `new` | the messages the plugin has not seen yet (every recent message when `conversation` is `None`) |
| `history` | the earlier messages, a recap for when the plugin has to start over |
| `files` | `ingredients.yaml` and every `recipes/*.yaml`, as text, for a model that can read files |

It returns the answer, or `{"text": ..., "conversation": id, "kept": true}`. The app keeps the id per chat and sends it back with the next message. `conversation: None` makes the next message carry the whole history again; `kept: false` (the plugin's side did not store this turn) sends these messages again with the next one. The app stops waiting after `llm.timeout_s` (in `settings`), but it cannot stop a plugin's thread: a plugin that waits on a remote turn should give up, and cancel that turn, a little before then, so a turn the app has stopped waiting for is never stored on the plugin's side.

A plugin can also offer to do something outside the chat, such as opening a pull request with a change to the app. The app tells it that it can with `turn["proposals"] = True`; the plugin's answer then may hold `"proposals": [{"id": ..., "summary": ..., "confirm_by": ...}]`, with any other keys it needs. The bot posts each `summary` as plain text and waits for a yes, as for a settings change, from the person who asked (`confirm_by: asker`) or from one of `bot.admins` (`admins`). An `admins` proposal raised in a private chat by someone who is not an admin is asked in the household group (`bot.chat_id`), where the admins can answer; without a group it is refused. A yes that replies to one of the bot's messages settles only what that message offered. The app waits on `act()` for up to an hour; a plugin should give up, and cancel its work, a little before then. On the yes it calls the plugin's `act(proposal, settings)` with the same dict and posts what it returns: a text, or `{"text": ..., "proposals": [...]}` with follow-ups, offered the same way. `confirm_by: none` is for a step that changes nothing, such as drafting a change for a person to review: it runs at once, in the background, and only for a household member. If the bot restarts while `act()` runs, it tells that chat on the next start that the work was cut off and has to be asked for again. Nothing in the app carries out a proposal by itself; the plugin decides what each one does.

### Changes from the chat

The AI ends its answer with a `change` block, and the app makes the change for the person who was talking. The block may change a short list of settings: `household.lunch_time`, `shopping_day`, `guests`, `batch.preferred_days`, `batch.sessions_per_week`, the weekly `rules`, additions to the asker's own `avoid_ingredients`, `shopping.primary` and `alternatives`, `sections`, `reminders` other than the `pills_lunch` one, `llm.instructions` (how the assistant answers: the whole new text, at most 4000 characters; the confirmation shows only the lines that change), a new recipe, and some fields of a recipe that exists (`name`, `meals`, `tags`, `season`, `serves_note`, `video`, `steps`, `ingredients`, `nutrition`; never `id`, `kind` or how long it keeps). It may also add to and remove from a person's `chat_notes`, the facts the household told the assistant (at most 20 per person, 300 characters each). A person's `notes` are written by hand and the chat never changes them. Pills, the pills reminder, calorie and protein targets and goals are never on it. Avoided foods are often medical, so nobody can change another person's list from the chat, and a food comes off a list only by hand. When the block says `asked: true` (the person asked for exactly this change), the bot makes it at once and answers with what changed, the commit id and how to undo it. Without it (the change is the AI's own idea) the bot shows the change in plain words and waits for that person to answer yes or no; in the group the answer has to reply to the bot or mention it, so an "ok" said to someone else is not a yes. Either way the limits above hold. To save settings it pulls the data repo, edits the file (comments and layout stay), runs the same validation as `smp check` on a copy, and pushes one git commit, so every change can be reverted. That needs the data folder to be a git checkout and `SMP_GIT_WRITE_TOKEN` (a token that may push to the data repo). Without them the bot shows the change for a person to make by hand.

Recipe edits and notes go in the same block and are saved in the same commit:

```yaml
asked: true
recipes:
  porridge: {meals: []}            # only the fields that change; an empty meals list retires it from the plan
notes:
  - add: {person: ana, text: Works 9 to 14 on Tuesdays}
  - remove: {person: ana, text: Works 9 to 14 on Tuesdays}   # the exact text of a chat note
```

When a recipe loses a meal, the days already planned from tomorrow on get another dish for that meal, and the bot says how many.

The same block changes the menu with a `plan` list. Menu changes are written under `state/`, not to git, so they need no token ([how it works](how-it-works.md#changing-the-menu)):

```yaml
asked: true
plan:
  - set: {date: 2026-01-14, meal: dinner, recipe: tuna-salad}   # breakfast, lunch, side, snack, dinner
  - swap: {a: 2026-01-14, b: 2026-01-16, meal: dinner}          # without meal: the whole day
  - pause: {from: 2026-01-12, to: 2026-01-18}                   # both days included
  - resume: {from: 2026-01-15, to: 2026-01-18}
  - regenerate: {from: 2026-01-19}                              # a Monday
  - undo: true                                                  # the last menu change, alone in the list
```

### Environment

| variable | what it does |
|---|---|
| `SMP_DATA` | the data directory, `/data` by default |
| `SMP_GIT_PULL=1` | pull the data directory every hour when it is a git checkout |
| `SMP_GIT_TOKEN` | optional read token for that pull over https |
| `SMP_GIT_WRITE_TOKEN` | optional token that may push: lets the bot save changes made from the chat |

## ingredients.yaml

```yaml
chicken-thigh:
  name: Chicken thigh, boneless
  section: butcher            # any id; label it in settings.sections
  store: supermarket          # default store for this ingredient
  unit: g                     # g | ml | ud (units)
  kitchen: {name: thigh, g: 110}   # how a cook counts it; optional `plural` when the language rule fails
  flags: []                   # free tags: oxalate-high, gluten, lactose, fish, sweet, contact-irritant...
```

## recipes/<id>.yaml

```yaml
id: lentil-stew
name: Lentil stew with vegetables
meals: [lunch]               # breakfast, lunch, dinner, snack
kind: batch                  # batch (cooked in a session, reheated) | quick (<=25 min) | assemble (no cooking) | fixed (breakfast/snack staples)
minutes: 60
fridge_days: 2               # days it keeps cooked in the fridge after the cook day
freezes: true
protein: legume              # legume | fish | oily-fish | chicken | turkey | beef | pork | egg | dairy | mixed
tags: [legume, light]        # the planner reads: legume, fish, oily-fish, egg, salad, side, cold-soup, fried, light, hearty, guests, treat
season: all                  # all, or a list of months 1-12
video: https://example.org/how-to-make-it   # optional
ingredients:
  - {id: brown-lentils, qty: 70}   # per reference serving, in the ingredient's unit
  - {id: carrot, qty: 60}
  - {id: olive-oil, qty: 8}
steps:
  - Sweat the onion, carrot and pepper in the oil.
nutrition:                   # per reference serving
  kcal: 520
  protein_g: 28
  fiber_g: 12
  sat_fat_g: 2.5
  salt_g: 1.0
  source: USDA
```

Tag and protein names can be in your language too: map them in `settings.yaml` under `tags` so the planner knows which of yours mean legume, fish, salad and so on.

```yaml
tags:                        # settings.yaml, optional: your tag -> the planner's meaning
  legumbre: legume
  pescado: fish
  pescado-azul: oily-fish
  ensalada: salad
  guarnicion: side
  sopa-fria: cold-soup
  frito: fried
  ligero: light
  contundente: hearty
  invitados: guests
  capricho: treat
```

## stores/<store>.yaml

```yaml
store: supermarket
name: Supermarket
region: "28001"
checked: 2026-10-10
products:
  chicken-thigh:              # ingredient id, products in order of preference
    - id: "3829"
      name: Boneless chicken thighs
      pack: {qty: 600, unit: g}    # one pack, in the ingredient's unit; loose produce: 1000 g and a note "sold loose"
      price_eur: 4.50
      url: https://shop.example/product/3829
      label: "Chicken thigh"       # ingredient text from the label
      clean: true                  # false = additives you avoid; used only when nothing clean exists
      note: ""
```

The list shows the first clean product, how many packs cover the week, and the estimated total per store.
