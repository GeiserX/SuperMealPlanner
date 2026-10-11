# How it works

## The plan

The planner is plain code: the same data and the same start date always give the same plan, and it needs no AI.

1. It picks two batch afternoons a week from `batch.preferred_days`, skipping afternoons with a calendar event between `calendar.busy_afternoon` times.
2. Each batch dish covers up to two lunches. A lunch eaten more than `fridge_days` after cooking comes from the freezer, so only dishes that freeze can go there. The batch sheet says which container goes where and when to move it to the fridge.
3. Dinners are quick or need no cooking. They are scored toward the week's missing legumes and fish, toward light dishes after a GLP-1 injection, and toward guest dishes on Sunday.
4. Breakfasts and snacks rotate through fixed staples. A `treat` snack lands on Saturday.
5. A side salad is added whenever neither lunch nor dinner is a salad.
6. The validator checks every hard rule. A failing draft is redrawn with the next seed, up to 400 times.

The app keeps a plan ready on its own: once an hour it checks that a plan covers the next seven days and makes the next four weeks when it does not.

## Portions

A recipe is written for one reference serving, what a person eating 2,000 kcal a day eats. A person's factor for a day is their current calorie target divided by 2,000, times the GLP-1 factor for that day of the week if they have one. Plates show grams and kitchen units ("2 thighs", "½ onion"). When a day falls short of a person's protein target, the app suggests quick extras from `settings.toppers`.

## Targets

Weigh-ins are smoothed with a 10% daily moving average. Calories change by one step at most every `kcal.every_weeks` (four by default), only once at least three readings cover most of that window, and never below the person's floor. Losing faster than `goal.max_loss_kg_week` (0.5 by default) adds food; faster than 1.4 kg a week shows a doctor flag. At or below the goal band the person switches to maintenance, so nobody keeps losing past their goal. With the `binge-eating-safe` pack, a window with any binge never cuts food, and four binges in four weeks show a note to seek help. Editing `kcal.start` resets the target to the new value.

A plan is never redone when the data changes, because lunches may already be cooked. A slot whose recipe was deleted or renamed, or which now breaks an avoided ingredient or a dinner rule, gets a substitute of the same kind (same protein first, then the least used). A lunch already cooked stays as it is, and the log says so. Weekly minimums and repeat limits apply from the next plan.

## Shopping

The list adds up every plate of the week. Batch dishes are bought in the week they are cooked. With store catalogues, each line shows the first clean product, how many packs, and the price, plus the same at an alternative store. `/list?store=<id>` writes the whole list for one store, which is handy for an online order.

## The bot

The bot answers private chats with the people in `bot.people`, and the configured group; in the group only those people unless `bot.group_open` is set.

- Commands: `/today`, `/tomorrow`, `/week`, `/list`, `/weight 82.4`, `/waist 95`, `/craving 7`, `/water`, `/pills`, `/about`, `/chatid` (Spanish aliases: `/hoy`, `/manana`, `/semana`, `/lista`, `/peso`, `/cintura`, `/antojo`, `/agua`, `/pastillas`, `/presentacion`).
- `/about` presents everything the bot does for this household, built from the settings: the reminders with their real times, the wake words, whether private chats are on.
- It works in a group, in private chats, or both: set `bot.chat_id` for the group and `bot.private_chats` for private chats. At every start it sets its own command menu and descriptions in Telegram, in English and in the household's language (`bot.sync_profile`).
- The AI answers a group message that mentions the bot, replies to it, or contains one of `bot.wake_words`; with `bot.respond: all`, every message. Private chats always go to the AI.
- Voice notes are transcribed with the configured `transcription` provider and always go to the AI.
- When the bot addresses a person (a reminder for them, the lunch-pills check, the AI telling someone what to do), it uses their Telegram @handle so Telegram notifies them. It looks each username up at start; `bot.handles` overrides it, and someone without a username keeps their name.
- Reminders fire once each, inside a 10-minute window after their time: plain messages, today's meal with its recipe link, batch days, shopping day, defrosting, and lunch pills not logged yet.

The AI gets the household, today's and tomorrow's plates, the week and the last week of logs. In a private chat it sees only the asker's own pills, notes, weigh-ins and logs. In the group it leaves out weigh-ins, binges, cravings, nausea and notes unless `bot.share_logs_in_group` is set. Group messages are kept as context only when the AI is asked (`bot.history_scope: all` keeps them all). It is told never to change a dose and never to suggest eating less after a binge.

Everything the AI sees goes to the provider you configure. With a local model nothing leaves your network.

## Other chat apps

Telegram is the chat app that ships. The commands, reminders and AI answers do not depend on it: a WhatsApp, Signal or Matrix adapter only has to receive messages and voice notes and send text, and it can be added beside `bot.py` without changing the rest.
