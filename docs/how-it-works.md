# How it works

## The plan

The planner is plain code: the same data and the same start date always give the same plan, and it needs no AI.

1. It picks two batch afternoons a week from `batch.preferred_days`, skipping afternoons with a calendar event between `calendar.busy_afternoon` times.
2. Each batch dish covers up to two lunches. A lunch eaten more than `fridge_days` after cooking comes from the freezer, so only dishes that freeze can go there. The batch sheet says which container goes where and when to move it to the fridge.
3. Dinners are quick or need no cooking. They are scored toward the week's missing legumes and fish, toward light dishes after a GLP-1 injection, and toward guest dishes on Sunday.
4. Breakfasts and snacks rotate through fixed staples. A `treat` snack lands on Saturday. A staple that uses an avoided ingredient is left out of the rotation, and the plan logs a warning naming the staple and the ingredient instead of failing.
5. A side salad is added whenever neither lunch nor dinner is a salad.
6. The validator checks every hard rule. A failing draft is redrawn with the next seed, up to 400 times.

The app keeps a plan ready on its own: once an hour it checks that a plan covers the next seven days and makes the next four weeks when it does not.

The week page and the day page show the whole day: breakfast, lunch with its side salad, the afternoon snack and dinner. Each day also lists up to three other snacks to nibble on. They are the snack recipes that are not a `treat`, use no avoided ingredient and are not that day's own snack, in a fixed rotation by date, so a day always shows the same ones.

## Changing the menu

The menu can be changed from the chat (see the bot below). Plans are generated files under `state/plans/`, outside the data repo's git, so a menu change needs no token and is written there directly; keep `state/` on storage that survives a restart, as the database already needs.

- A dish can be put in any meal of any day, and two days can swap one meal or all of them. A lunch that changes leaves the container it shared and is cooked in the same batch session when that is still to come and the dish keeps until its day; otherwise it is cooked that morning. When neither main dish of a changed day is a salad, a side salad is added.
- A pause covers a range of dates in which the household eats off the plan. The paused days are kept in `state/paused.yaml` and the plan on disk is not touched: those days show no dishes on any page or in the bot, the shopping list and the batch sheet leave them out, and the meal, batch, defrost and shopping reminders stay silent (the shopping reminder only when the rest of its week is paused). A lunch for a later day that a paused day would have cooked is cooked on the first day back. The pause ends by itself after its last day, and it can be ended early.
- A shift moves the menu from a day on 1 to 4 whole weeks later, so each dish keeps its weekday: the days, the batch sessions and the paused days after that day move with it, and the plan grows by those weeks. The days it leaves are paused, so nothing new is drawn for them. A lunch cooked before that day for a day that moves is cooked again on the first day back. Calendar marks stay with their dates.
- A new draw starts on a Monday. The days before it stay as they are, the earlier plan stops cooking for the days it lost, and asking again gives another draw.
- Every change runs through the validator first. One that would newly break a hard rule (an avoided ingredient, a dinner rule, a weekly minimum, a lunch that would not keep) is refused with the reason and nothing is written. The limit on how often a dish repeats only shapes a draw: a dish someone asked for may go over it.
- The plans and paused days as they were before the last change are kept in `state/menu-undo/`, so the last menu change can be undone once.

## Portions

A recipe is written for one reference serving, what a person eating 2,000 kcal a day eats. A person's factor for a day is their current calorie target divided by 2,000, times the GLP-1 factor for that day of the week if they have one. Plates show grams and kitchen units ("2 thighs", "½ onion"). When a day falls short of a person's protein target, the app suggests quick extras from `settings.toppers`.

## Targets

Weigh-ins are smoothed with a 10% daily moving average. Calories change by one step at most every `kcal.every_weeks` (four by default), only once at least three readings cover most of that window, and never below the person's floor. Losing faster than `goal.max_loss_kg_week` (0.5 by default) adds food; faster than 1.4 kg a week shows a doctor flag. At or below the goal band the person switches to maintenance, so nobody keeps losing past their goal. With the `binge-eating-safe` pack, a window with any binge never cuts food, and four binges in four weeks show a note to seek help. Editing `kcal.start` resets the target to the new value.

A plan is never redone when the data changes, because lunches may already be cooked. A slot whose recipe was deleted or renamed, or which now breaks an avoided ingredient or a dinner rule, gets a substitute of the same kind (same protein first, then the least used). A lunch already cooked stays as it is, and the log says so. Weekly minimums and repeat limits apply from the next plan.

## Shopping

The list adds up every plate of the week. Batch dishes are bought in the week they are cooked. With store catalogues, each line shows the first clean product, how many packs, and the price, plus the same at an alternative store. `/list?store=<id>` writes the whole list for one store, which is handy for an online order.

## The bot

The bot answers private chats with the people in `bot.people`, and the configured group; in the group only those people unless `bot.group_open` is set.

- `/week` lists breakfast, lunch, snack and dinner for each day on one line.
- Commands: `/today`, `/tomorrow`, `/week`, `/list`, `/weight 82.4`, `/waist 95`, `/craving 7`, `/water`, `/pills`, `/about`, `/new`, `/chatid` (Spanish aliases: `/hoy`, `/manana`, `/semana`, `/lista`, `/peso`, `/cintura`, `/antojo`, `/agua`, `/pastillas`, `/presentacion`, `/nuevo`).
- `/about` presents everything the bot does for this household, built from the settings: the reminders with their real times, whether private chats are on.
- It works in a group, in private chats, or both: set `bot.chat_id` for the group and `bot.private_chats` for private chats. At every start it sets its own command menu and descriptions in Telegram, in English and in the household's language (`bot.sync_profile`).
- In a private chat the AI always answers. In the group it answers only a message that mentions the bot's @handle or replies to it. Telegram shows a bot with privacy mode on (the default) only commands and replies to it, so for mentions make the bot a group admin or turn privacy off in BotFather (`/setprivacy`). The bot logs a warning at start when it cannot see them.
- Voice notes are transcribed with the configured `transcription` provider and always reach the AI, in the group too, because a voice note cannot mention the bot or reply by itself. The bot first writes down what it heard, then answers.
- The AI writes Markdown; the bot sends it as Telegram formatting (bold, italic, links, lists) and falls back to plain text when Telegram refuses the markup.
- Each chat is one conversation. A provider that keeps it on its side gets only the new messages, with the household's data fresh every time; the others get the recent history, as before. `/new` starts the chat over.
- Messages sent together get one answer: the bot waits 2.5 seconds after the newest one (and up to 30 seconds for a voice note still being transcribed), and a plugin provider can take what arrives while it writes into the same answer. An answer to one message replies to it; an answer to several is sent plainly.
- Edits count: an edit of a waiting message sends the new text, an edit of a message being answered reaches that answer as a correction, an edit of an answered message gets a new answer that says what changes, and an edit that adds the bot's mention is answered.
- When the AI puts a choice to someone (which dish, which day), it can offer the choices as buttons under its answer; a tap is that person's answer, and they can ignore the buttons and keep talking.
- When the AI tells someone to do something at a time (cook on Sunday afternoon, take the fish out on Saturday night), the bot offers each step as a reminder, with buttons: here, in the calendar (with `bot.reminder_calendar`) or nothing. Nothing happens without a tap, and the offer expires after 30 minutes. A reminder set here is posted in the chat at its time, naming the person, and survives a restart; `/reminders` lists them and cancels one. In the group, messages not addressed to the bot reach the AI as context, marked, so a remark between people is never taken as an order.
- The AI can change the menu (a dish, a swap, a pause, a shift by whole weeks, a new draw, an undo), a recipe that exists (taking it out of breakfast, for example, which also takes it out of the days planned from tomorrow on), its own notes about each person, and a short list of settings (reminders, batch days, lunch time, weekly rules, guests, foods the asker adds to their own avoided list, the shopping stores, its own instructions, a new recipe). A change the person asked for is made at once, and the bot answers with what changed and how to undo it. A change the AI comes up with on its own is shown first, with Yes and No buttons, and made only after that person taps or writes yes. Settings are saved as one git commit in the data repo. Pills, the pills reminder, calories, protein, goals and taking a food off an avoided list are never on the list ([data format](data-format.md#changes-from-the-chat)).
- A plugin provider can also offer to do something outside the chat, such as a pull request with a change to the app that a person reviews and merges. The bot shows it and carries it out only after a yes from the person who asked, or from one of `bot.admins` when the plugin says an admin must approve ([data format](data-format.md#settingsyaml)).
- When the bot addresses a person (a reminder for them, the lunch-pills check, the AI telling someone what to do), it uses their Telegram @handle so Telegram notifies them. It looks each username up at start; `bot.handles` overrides it, and someone without a username keeps their name.
- Reminders fire once each, inside a 10-minute window after their time: plain messages, today's meal with its recipe link, batch days, shopping day, defrosting, and lunch pills not logged yet.

The AI gets the household, today's and tomorrow's plates, the menu of this week and the next with the paused days, the recipe list and the last week of logs. In a private chat it sees only the asker's own pills, notes, weigh-ins and logs. In the group it leaves out weigh-ins, binges, cravings, nausea and notes unless `bot.share_logs_in_group` is set. Group messages are kept as context only when the AI is asked (`bot.history_scope: all` keeps them all). It is told never to change a dose and never to suggest eating less after a binge.

Everything the AI sees goes to the provider you configure. With a local model nothing leaves your network. A provider with web search can find a video or a recipe for a dish; it is told to compare it with your recipe and say what differs. It is also told to look up a fact the household's data does not hold (a nutrition guideline, what a food contains), answer with the figure and its source, and only then say what a clinician should confirm, instead of sending the person to ask their doctor.

## Other chat apps

Telegram is the chat app that ships. The commands, reminders and AI answers do not depend on it: a WhatsApp, Signal or Matrix adapter only has to receive messages and voice notes and send text, and it can be added beside `bot.py` without changing the rest.
