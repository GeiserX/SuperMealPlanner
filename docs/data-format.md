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

Keep the data directory in its own private repository: it holds health details. Ids are lowercase kebab-case ASCII, and `guests` is not a person id: it names the guest servings. Food names, steps and units are in whatever language you cook in.

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
  preferred_days: [monday, friday, saturday]   # tried first
  sessions_per_week: 2
  avoid_days: [tuesday]       # never a batch session on these, even when every other afternoon is busy
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
    swaps: {milk: oat-drink}    # ingredient id -> the one this person eats instead
    training_days: [monday, thursday]   # strength training: the day's energy target rises
    training_extra_kcal: 150    # by this much; 150 when left out
    avoid_tags_at_dinner: [cold-soup, fried]
    pills:                    # shown next to the meal they go with; the app never changes a dose
      waking: []
      breakfast: [{name: Vitamin D3, dose: 2000 IU}]
      lunch: [{name: Omega-3, dose: 1 softgel}]
      dinner: []
      bedtime: [{name: Magnesium, dose: 300 mg, days: [monday, thursday]}]   # days: daily when left out
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

`swaps` changes an ingredient for one person only: their plate and the shopping list use the other ingredient in the same quantity, the recipe text stays as written, and nobody else's plate changes. Both ids must be in `ingredients.yaml`, in the same unit, and the one eaten instead must not be among the person's avoided foods. On a day in `training_days` the person's energy target is their calorie target plus `training_extra_kcal` (0 to 600; 150, roughly what 45 to 60 minutes of strength training uses above resting, when left out), so every plate of that day grows a little. `batch.avoid_days` may not hold all seven days. A week whose Monday to Wednesday, or Thursday to Sunday, are all avoided gets one session; the morning cook on the first Monday of a new plan, for that day's lunch, is the one exception to the rule.

A pill with `days` shows, and counts for the lunch pills reminder, only on those weekdays. With `settings.health_export` (below), a person the export lists takes their pills from it instead of from here.

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
  voice: true                 # transcribe voice notes; every one is answered, in the group too
  private_chats: true         # people in bot.people may also write to the bot privately
  group_open: false           # true: anyone in the group may use the bot, not only bot.people
  sync_profile: true          # at start, set the bot's command menu and descriptions in Telegram
  share_logs_in_group: false  # true: the AI may discuss weigh-ins, binges, cravings and notes in the group
  history_scope: asked        # asked | all: which group messages are kept as AI context
  reminder_calendar: false    # true: a reminder the AI offers can also go in the calendar (a plugin with calendar tools)
  history: 20                 # recent messages the AI gets as context
  history_days: 0             # and every message of these last days (0: only `history`)
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
health_export:                # optional: each person's pills from a file another service keeps
  url: https://health.example.org/export.yaml
  token_env: HEALTH_EXPORT_TOKEN   # optional: sent as "Authorization: token <value>"
  cache: state/health/export.yaml  # the last good copy, inside the data folder; this is the default
  timeout_s: 5
```

**Health export.** Another service (a health record, a doctor's app) can own the household's medicines and supplements. `health_export.url` points at a YAML file of this shape; every other key in it is ignored:

```yaml
people:
  alex:                       # a person id from household.yaml
    meds:
      - {name: Vitamin D3, dose: 2000 IU, slot: breakfast, days: daily, food: "With breakfast"}
      - {name: Weekly shot, dose: 1 mg, slot: waking, days: [monday]}
      - {name: Sleep aid, dose: 5 mg, slot: as_needed}
```

`slot` is one of the pill slots (`waking`, `breakfast`, `lunch`, `dinner`, `bedtime`) or `as_needed`, which is taken when needed and so never shows as a daily pill. `days` is `daily` or a list of weekdays. `food` becomes the pill's note. A slot or a day the app cannot place stops the data from loading, with a clear error from `smp check`; the file is then not kept. A person listed with `meds` takes their pills from the export (an empty list means none), and household.yaml's pills stay the fallback for a person the export leaves out.

The app fetches the file when the data loads: at start and every hour. It sends the ETag of the last good copy, so an unchanged file costs a 304, and keeps that copy at `cache` with a `.meta` file beside it. When a fetch fails (no answer within `timeout_s`, an error status, or something that is not an export), the last good copy is used; with no copy, everyone's pills come from household.yaml. `smp check` ends with one line per person saying where their pills came from. A check of a change from the chat never fetches: pills never change from the chat. Nothing logs the URL, the token or the file.

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
| `attachments` | the photos and files sent with this turn's messages, each `{"name": "inbox/<message id>-<file name>", "mime", "data": bytes, "from": person id}` (below) |
| `incoming` | a function the plugin may call from its thread while it works: it returns the chat's messages that arrived since the last call, each `{"id", "role", "content", "ts"}` |
| `ask` | given only when the plugin defines `answer()`: a function that asks a person for a yes (below) |

Each message in `new` and `history` is `{"role", "content", "ids", "ts"}`: the app's ids of the chat messages merged into it and when the last of them was written (ISO 8601, UTC).

It returns the answer, or `{"text": ..., "conversation": id, "kept": true}`. The app keeps the id per chat and sends it back with the next message. `conversation: None` makes the next message carry the whole history again; `kept: false` (the plugin's side did not store this turn) sends these messages again with the next one. The app stops waiting after `llm.timeout_s` (in `settings`), but it cannot stop a plugin's thread: a plugin that waits on a remote turn should give up, and cancel that turn, a little before then, so a turn the app has stopped waiting for is never stored on the plugin's side.

**One answer per burst.** A chat's messages wait 2.5 seconds after the newest one, and a voice note of that chat still being transcribed holds them up to 30 seconds, so messages sent together get one answer. Messages that arrive while the answer is being written are handed to the plugin through `incoming()`; it names the ones its answer took into account with `"covers": [ids]` in its dict, and every handed-over message it does not name makes the next turn. An answer that covers one message replies to it; one that covers several is sent plainly. A plugin that never calls `incoming()` gets those messages in the next turn.

**Photos and files.** A photo, or a file of any kind, always reaches the AI, in the group too, like a voice note; its caption is the message, and its text names the file as `[photo: inbox/5-photo.jpg]` or `[file: inbox/6-recipe.pdf]`; a file keeps its own name and extension, made plain. Only a plugin with `converse()` gets them, in `attachments`; with any other provider the bot says it cannot look at them. The bot fetches each one from Telegram when its turn starts: 20 MB per file (Telegram's limit for bots) and 25 MB for one turn. A file too big, or one whose download fails, gets a short note under it, and the message still goes without the file. A photo sent while an answer is being written is not handed over through `incoming()`: it waits for the next turn, which carries it.

**Edits.** An edited message still waiting goes with its new text. An edit of a message of the running answer reaches the plugin through `incoming()` as a correction with the old and the new wording; an edit of a message already answered starts a new answer with that correction and the answer it got; a group message that did not call the bot and now mentions it is answered. An edit that changes nothing, or starts with `/`, does nothing, and an edit is never a yes or a no. The chat log keeps the final wording. In a group, Telegram sends a bot edits of messages that do not mention it only when the bot is an admin.

**Choices.** The answer may offer the person a choice: `"options": ["Lentils", "Chickpeas"]` in the dict, or an ```` ```options ```` block at the end of the text with one `- label` per line (the built-in rules tell the model how). At most five, 40 characters each. The bot shows them as buttons under the answer with a line saying they may also answer in their own words; a tap by a person the answer was for is that person's next message, with the label as its text, and the buttons give way to the one chosen. Nothing waits for a tap.

**Reminders.** When the answer tells someone to do something at a time (cook on Sunday afternoon, take the fish out of the freezer on Saturday night), the built-in rules have the AI list those steps: `"reminders": [{"when": "2026-01-18 18:00", "text": "Cook Monday's lunch", "person": "sam"}]` in the dict, or a ```` ```reminders ```` block at the end of the text with one `- 2026-01-18 18:00 | Cook Monday's lunch` line each (` | <person id>` at the end when it is for someone other than the person answered). Times are in the household's timezone; the bot keeps only times still to come, within 60 days, at most five. It posts them in one message under the answer, naming the people they are for, with buttons per reminder: a reminder here, in the calendar (only with `bot.reminder_calendar`) or nothing, and the line saying that without an answer nothing is done. Only the people the answer was for and the people a reminder is for can tap. "Here" stores it in the database; the bot posts it in that chat at its time, naming the person, and says so when it is late (after a restart, for example). "In the calendar" starts a turn in which the AI is asked to add the event; that tap approves the turn's first ask when the ask adds an event at that very time (the plugin says so with `"action": "add"` and `"when"`, epoch seconds, in the request), and anything else is asked as usual. An offer nobody taps expires after 30 minutes and nothing is done. `/reminders` lists the reminders to come in that chat with a button to cancel each. A step that repeats every week is a reminder in `settings.yaml` instead.

**Who the AI answers.** In the group the AI sees every message the app keeps (`history_scope: all`), but a message that did not mention the bot or reply to it reaches it marked `[not to you]`, and its rules say to act only on what is addressed to it.

**Asks.** A plugin whose actions need a person's yes while it works defines `answer(request, decision, settings)`; the app then gives it `turn["ask"](request)`, with `request = {"id", "summary", "confirm_by": "asker" | "admins", "expires_at": epoch seconds}`. The bot posts the summary with Approve and Reject, replying to the message being answered. `asker` is the person the turn answers (the admins when it covers several people's messages); anyone else's tap is refused. On a tap the app calls `answer(request, {"approved": bool, "by": user id, "note": None}, settings)`, which returns `{"ok": bool | None, "error": str | None}`, and says done or failed under the ask. An ask nobody can answer, or one the bot cannot post, is answered at once with `approved: false, note: "no one could be asked"`; an edit of a message of the turn answers its open asks with `note: "editado: <the new text>"`. Past `expires_at`, or once the turn ends, the buttons go.

A plugin can also offer to do something outside the chat, such as opening a pull request with a change to the app. The app tells it that it can with `turn["proposals"] = True`; the plugin's answer then may hold `"proposals": [{"id": ..., "summary": ..., "confirm_by": ...}]`, with any other keys it needs. The bot posts each `summary` as plain text with Yes and No buttons and waits for a yes, as for a settings change, from the person who asked (`confirm_by: asker`) or from one of `bot.admins` (`admins`). An `admins` proposal raised in a private chat by someone who is not an admin is asked in the household group (`bot.chat_id`), where the admins can answer; without a group it is refused. A yes that replies to one of the bot's messages settles only what that message offered. The app waits on `act()` for up to an hour; a plugin should give up, and cancel its work, a little before then. On the yes it calls the plugin's `act(proposal, settings)` with the same dict and posts what it returns: a text, or `{"text": ..., "proposals": [...]}` with follow-ups, offered the same way. `confirm_by: none` is for a step that changes nothing, such as drafting a change for a person to review: it runs at once, in the background, and only for a household member. If the bot restarts while `act()` runs, it tells that chat on the next start that the work was cut off and has to be asked for again. Nothing in the app carries out a proposal by itself; the plugin decides what each one does.

### Changes from the chat

The AI ends its answer with a `change` block, and the app makes the change for the person who was talking. The block may change a short list of settings: `household.lunch_time`, `shopping_day`, `guests`, `batch.preferred_days`, `batch.sessions_per_week`, `batch.avoid_days`, the weekly `rules`, additions to the asker's own `avoid_ingredients`, the asker's own `swaps` and `training_days` (the whole new map or list; `training_extra_kcal` stays by hand), `shopping.primary` and `alternatives`, `sections`, `reminders` (a `pills_lunch` one only by a person it names, and never for someone else), `llm.instructions` (how the assistant answers: the whole new text, at most 4000 characters; the confirmation shows only the lines that change), a new recipe, and some fields of a recipe that exists (`name`, `meals`, `tags`, `season`, `serves_note`, `video`, `steps`, `ingredients`, `nutrition`; never `id`, `kind` or how long it keeps). It may also add to and remove from a person's `chat_notes`, the facts the household told the assistant (at most 20 per person, 300 characters each). A person's `notes` are written by hand and the chat never changes them. Pills, the pills reminder, calorie and protein targets and goals are never on it. Avoided foods are often medical, so nobody can change another person's list, swaps or training days from the chat, and a food comes off a list only by hand. When the block says `asked: true` (the person asked for exactly this change), the bot makes it at once and answers with what changed, the commit id and how to undo it. Without it (the change is the AI's own idea) the bot shows the change in plain words and waits for that person to answer yes or no; in the group the answer has to reply to the bot or mention it, so an "ok" said to someone else is not a yes. Either way the limits above hold. To save settings it pulls the data repo, edits the file (comments and layout stay), runs the same validation as `smp check` on a copy, and pushes one git commit, so every change can be reverted. That needs the data folder to be a git checkout and `SMP_GIT_WRITE_TOKEN` (a token that may push to the data repo). Without them the bot shows the change for a person to make by hand.

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
  - shift: {from: 2026-01-13, weeks: 1}                         # 1 to 4 weeks; the gap is paused
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
