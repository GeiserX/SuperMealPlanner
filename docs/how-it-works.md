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

Weigh-ins are smoothed with a 10% daily moving average. Calories change by one step at most every four weeks, only with three readings in that window, and never below the person's floor. Losing faster than 0.75 kg a week adds food; faster than 1.4 kg a week shows a doctor flag. Inside the goal band the person switches to maintenance. With the `binge-eating-safe` pack, four binges in four weeks show a note to seek help, and nothing ever cuts food after a binge.

## Shopping

The list adds up every plate of the week. Batch dishes are bought in the week they are cooked. With store catalogues, each line shows the first clean product, how many packs, and the price, plus the same at an alternative store. `/list?store=<id>` writes the whole list for one store, which is handy for an online order.

## The bot

The bot answers only the configured group and private chats with the people in `bot.people`.

- Commands: `/today`, `/tomorrow`, `/week`, `/list`, `/weight 82.4`, `/waist 95`, `/craving 7`, `/water`, `/pills`, `/chatid` (Spanish aliases: `/hoy`, `/manana`, `/semana`, `/lista`, `/peso`, `/cintura`, `/antojo`, `/agua`, `/pastillas`).
- The AI answers a group message that mentions the bot, replies to it, or contains one of `bot.wake_words`; with `bot.respond: all`, every message. Private chats always go to the AI.
- Voice notes are transcribed with the configured `transcription` provider and always go to the AI.
- Reminders fire once each, inside a 10-minute window after their time: plain messages, today's meal with its recipe link, batch days, shopping day, defrosting, and lunch pills not logged yet.

The AI gets the household, today's and tomorrow's plates, the week and the last week of logs. It is told never to change a dose and never to suggest eating less after a binge.
