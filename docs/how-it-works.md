# How it works

## The plan

The planner is plain code: the same data and the same start date always give the same plan, and it needs no AI.

1. It picks two batch afternoons a week from `batch.preferred_days`, skipping afternoons with a calendar event between `calendar.busy_afternoon` times, and never on a day in `batch.avoid_days`, even when every other afternoon is busy.
2. Each batch dish covers up to two lunches. A lunch eaten more than `fridge_days` after cooking comes from the freezer, so only dishes that freeze can go there. The batch sheet says which container goes where and when to move it to the fridge.
3. Dinners are quick or need no cooking. They are scored toward the week's missing legumes and fish, toward light dishes after a GLP-1 injection, and toward guest dishes on Sunday.
4. Breakfasts and snacks rotate through fixed staples. A `treat` snack lands on Saturday. A staple that uses an avoided ingredient is left out of the rotation, and the plan logs a warning naming the staple and the ingredient instead of failing.
5. A side salad is added whenever neither lunch nor dinner is a salad.
6. The validator checks every hard rule. A failing draft is redrawn with the next seed, up to 400 times.

The app keeps a plan ready on its own: once an hour it checks that a plan covers the next seven days and makes the next four weeks when it does not.

One function, `planner.day_menu`, gives a day's whole menu in eating order: breakfast, lunch with its side salad, the afternoon snack, dinner, then the snacks to nibble on. The week page, the top of the day page, the bot's `/today` and `/week`, and the daily menu reminder all read it, so none of them can leave a meal out. A reminder of kind `day` sends that menu at its time every day, with where lunch comes from and a link to the day's page; it stays silent on a paused day. Each day also lists up to three other snacks to nibble on. They are the snack recipes that are not a `treat`, use no avoided ingredient and are not that day's own snack, in a fixed rotation by date, so a day always shows the same ones. On a day with a batch session, the day page also carries that session's whole sheet (amounts, containers and the steps), and the steps of the dishes cooked that day.

The tasks page (`/tasks`) lists, for each day of a week, the texts the defrost, batch and shopping reminders send that day, at their times, plus the one-off reminders set from the chat.

Each lunch and dinner has 👍 and 👎, one vote per person (the person the page was opened as). Tapping the same one again takes the vote back. The next plans use the household's summed votes: a liked dish scores higher in each draw (two votes at most count), and a disliked one is left out unless nothing else fits that slot. A plan already made does not change.

## Changing the menu

The menu can be changed from the chat (see the bot below). Plans are generated files under `state/plans/`, outside the data repo's git, so a menu change needs no token and is written there directly; keep `state/` on storage that survives a restart, as the database already needs.

- A dish can be put in any meal of any day, and two days can swap one meal or all of them. A lunch that changes leaves the container it shared and is cooked in the same batch session when that is still to come and the dish keeps until its day; otherwise it is cooked that morning. When neither main dish of a changed day is a salad, a side salad is added.
- A pause covers a range of dates in which the household eats off the plan. The paused days are kept in `state/paused.yaml` and the plan on disk is not touched: those days show no dishes on any page or in the bot, the shopping list and the batch sheet leave them out, and the meal, batch, defrost and shopping reminders stay silent (the shopping reminder only when the rest of its week is paused; the soaking on the eve of the first session back still goes). A lunch for a later day that a paused day would have cooked is cooked on the first day back. The pause ends by itself after its last day, and it can be ended early.
- A shift moves the menu from a day on 1 to 4 whole weeks later, so each dish keeps its weekday: the days, the batch sessions and the paused days after that day move with it, and the plan grows by those weeks. The days it leaves are paused, so nothing new is drawn for them. A lunch cooked before that day for a day that moves is cooked again on the first day back. Calendar marks stay with their dates.
- A new draw starts on a Monday. The days before it stay as they are, the earlier plan stops cooking for the days it lost, and asking again gives another draw.
- Every change runs through the validator first. One that would newly break a hard rule (an avoided ingredient, a dinner rule, a weekly minimum, a lunch that would not keep) is refused with the reason and nothing is written. The limit on how often a dish repeats only shapes a draw: a dish someone asked for may go over it.
- The plans and paused days as they were before the last change are kept in `state/menu-undo/`, so the last menu change can be undone once.

## Portions

A recipe is written for one reference serving, what a person eating 2,000 kcal a day eats. A person's factor for a day is their current calorie target divided by 2,000, times the GLP-1 factor for that day of the week if they have one. On a day in the person's `training_days` their calorie target first grows by `training_extra_kcal`. A person's `swaps` put another ingredient on their own plate, in the same quantity; everyone else's plate stays as the recipe says. Plates show grams and kitchen units ("2 thighs", "½ onion"). When a day falls short of a person's protein target, the app suggests quick extras from `settings.toppers`.

## Targets

Weigh-ins are smoothed with a 10% daily moving average. Calories change by one step at most every `kcal.every_weeks` (four by default), only once at least three readings cover most of that window, and never below the person's floor. Losing faster than `goal.max_loss_kg_week` (0.5 by default) adds food; faster than 1.4 kg a week shows a doctor flag. At or below the goal band the person switches to maintenance, so nobody keeps losing past their goal. With the `binge-eating-safe` pack, a window with any binge never cuts food, and four binges in four weeks show a note to seek help. Editing `kcal.start` resets the target to the new value.

A plan is never redone when the data changes, because lunches may already be cooked. A slot whose recipe was deleted or renamed, or which now breaks an avoided ingredient or a dinner rule, gets a substitute of the same kind (same protein first, then the least used). A lunch already cooked stays as it is, and the log says so. Weekly minimums and repeat limits apply from the next plan.

## Shopping

The list adds up every plate of the week, each with its eater's swaps, so a swapped ingredient is bought only for the person who eats it. The batch sheet splits the pot the same way. Batch dishes are bought in the week they are cooked. With store catalogues, each line shows the first clean product, how many packs, and the price, plus the same at an alternative store. `/list?store=<id>` writes the whole list for one store, which is handy for an online order.

Ticks on the list are saved by the app, so every phone sees the same ones. «I have it» takes an ingredient off every week's list and puts it in a short "at home" part at the bottom, until someone puts it back. The list also leaves off what the pantry already holds enough of (below).

## The pantry

The pantry is what is at home, and it is worked out by the app, so nobody keeps it up to date. Only what comes in is stored:

- A tick on a week's list brings in what that line buys: the whole packs the store's catalogue sells (a 1 l bottle of oil, 0.4 kg of loose apples), or the line's amount when there is no catalogue. Unticking takes it out again. A tick on the phone does the same as one on the page.
- The chat says how much there is ("the oil ran out", "there is a kilo of rice"), and the AI sets it with a change (`pantry` in the [data format](data-format.md#changes-from-the-chat)). That amount replaces what the pantry had. Said before the household's `lunch_time` it is what that day's cooking starts from; from lunch time on, it is what that cooking left.

Everything else follows from the plan and the data, the same way every time:

- Each day the plan cooks takes its ingredients out at the end of that day: the lunches cooked in that day's sessions and the other meals eaten that day, at each person's servings and swaps, the same amounts the shopping list adds up. The oldest entry goes first. A paused day cooks nothing.
- An ingredient with `keeps_days` goes off that many days after it came in. One without keeps until it is used up.
- «I have it» stays as it was: at home in an amount nobody counted, never used up.

The list for a week leaves off an ingredient when the pantry, without what that week's own ticks brought in, holds enough of it for that week's cooking still to come, in food still good on the last day it is used. A next week's list counts what this week cooks first. A ticked line never leaves its own list. The list page shows the pantry at the bottom, with what each line of this week no longer asks for, and `GET /api/v1/pantry` gives the same to the phone.

The AI sees the pantry in its facts, across weeks, and is told that nothing else is at home as far as the app knows. The consistency check names food that goes off before any planned dish uses it up.

## The bot

The bot answers private chats with the people in `bot.people`, and the configured group; in the group only those people unless `bot.group_open` is set.

When the bot starts on a version it has not announced yet, it says so once ("Now running version X (was Y)") privately to each person in `bot.admins`, or in the group when none of them can be written to. A restart on the same version says nothing, and a version nobody was told about is announced at the next start.

- `/week` lists breakfast, lunch, snack and dinner for each day on one line.
- Commands: `/today`, `/tomorrow`, `/week`, `/list`, `/weight 82.4`, `/waist 95`, `/craving 7`, `/water`, `/pills`, `/about`, `/new`, `/chatid` (Spanish aliases: `/hoy`, `/manana`, `/semana`, `/lista`, `/peso`, `/cintura`, `/antojo`, `/agua`, `/pastillas`, `/presentacion`, `/nuevo`).
- `/about` presents everything the bot does for this household, built from the settings: the reminders with their real times, whether private chats are on.
- It works in a group, in private chats, or both: set `bot.chat_id` for the group and `bot.private_chats` for private chats. At every start it sets its own command menu and descriptions in Telegram, in English and in the household's language (`bot.sync_profile`).
- In a private chat the AI always answers. In the group it answers only a message that mentions the bot's @handle or replies to it. Telegram shows a bot with privacy mode on (the default) only commands and replies to it, so for mentions make the bot a group admin or turn privacy off in BotFather (`/setprivacy`). The bot logs a warning at start when it cannot see them.
- Voice notes are transcribed with the configured `transcription` provider and always reach the AI, in the group too, because a voice note cannot mention the bot or reply by itself. The bot first writes down what it heard, then answers.
- Photos, videos, GIFs, round video notes, stickers and files of any kind always reach the AI too when the provider is a plugin with `converse()`: it gets the files themselves, with their caption as the message. With any other provider the bot answers that it cannot look at them, and neither the file nor its caption goes to the AI.
- The AI writes Markdown; the bot sends it as Telegram formatting (bold, italic, links, lists) and falls back to plain text when Telegram refuses the markup.
- Each chat is one conversation. A provider that keeps it on its side gets only the new messages, with the household's data fresh every time; the others get the recent history, as before. `/new` starts the chat over.
- Messages sent together get one answer: the bot waits 2.5 seconds after the newest one, and for a voice note still being transcribed it waits until the text is there (30 seconds plus 3 per second of audio at most, the time a slow transcription server takes; a note that fails lets the answer go at once). A plugin provider takes what arrives while it writes into the same answer. When the answer comes back without one of those messages, it is not sent: the bot asks again with all of them, once, so the chat gets one answer and no proposal that a later message already changed. An answer to one message replies to it; an answer to several is sent plainly.
- A change still waiting for a yes loses its buttons, marked as replaced, when a newer change sets the same thing (the same recipe, setting, meal of a day or pantry item), so nobody taps an outdated one. A recipe change lists the steps it removes and adds, not only how many there are.
- A restart loses no message. A redeploy can replace the container at any moment, so each message is stored in the database before Telegram is told it arrived, and stays there until the answer that took it is done. At start the bot answers what was waiting, being transcribed or being answered when the last process stopped. A message whose answer went out just before the stop can be answered twice; none is dropped.
- Edits count: an edit of a waiting message sends the new text, an edit of a message being answered reaches that answer as a correction, an edit of an answered message gets a new answer that says what changes, and an edit that adds the bot's mention is answered.
- When the AI puts a choice to someone (which dish, which day), it can offer the choices as buttons under its answer; a tap is that person's answer, and they can ignore the buttons and keep talking.
- When the AI tells someone to do something at a time (cook on Sunday afternoon, take the fish out on Saturday night), the bot offers each step as a reminder, with buttons: here, in the calendar or No (only «here» without `bot.reminder_calendar`). Nothing happens without a tap, and the offer expires after 30 minutes. A reminder set here is posted in the chat at its time, naming the person, and survives a restart; `/reminders` lists them and cancels one. A reminder the person asked for in so many words ("remind me at 23:00") is set at once, with no buttons. The AI sees the chat's reminders still to come with their ids, and when a change makes one wrong (the dish changed, the step moved) it cancels it and says so, instead of asking the household to delete it. In the group, messages not addressed to the bot reach the AI as context, marked, so a remark between people is never taken as an order.
- The AI can change the menu (a dish, a swap, a pause, a shift by whole weeks, a new draw, an undo), a recipe that exists (taking it out of breakfast, for example, which also takes it out of the days planned from tomorrow on), its own notes about each person, and a short list of settings (reminders, batch days and days with no batch, lunch time, weekly rules, guests, foods the asker adds to their own avoided list, the asker's own ingredient swaps and training days, the shopping stores, its own instructions, a new recipe). A change the person asked for is made at once, and the bot answers with what changed and how to undo it. A change the AI comes up with on its own is shown first, with Yes and No buttons, and made only after that person taps or writes yes. Settings are saved as one git commit in the data repo. Pills, the pills reminder, calories, protein, goals and taking a food off an avoided list are never on the list ([data format](data-format.md#changes-from-the-chat)).
- A plugin provider can also offer to do something outside the chat, such as a pull request with a change to the app that a person reviews and merges. The bot shows it and carries it out only after a yes from the person who asked, or from one of `bot.admins` when the plugin says an admin must approve ([data format](data-format.md#settingsyaml)).
- When the bot addresses a person (a reminder for them, the lunch-pills check, the AI telling someone what to do), it uses their Telegram @handle so Telegram notifies them. It looks each username up at start; `bot.handles` overrides it, and someone without a username keeps their name.
- Reminders fire once each, inside a 10-minute window after their time: plain messages, today's meal with its recipe link, batch days, shopping day, defrosting, and lunch pills not logged yet.
- After every change the bot makes (a menu change, a settings change, a reminder set) and once every morning, it runs the consistency check (below). A reminder about a dish the menu no longer has is cancelled there and then, with one line in its chat; a reminder whose time comes before the check is checked as it fires. When a change left a problem that was not there before, one short line under the answer names it. The morning report goes to `bot.checkup_chat` (the group when unset) at `bot.checkup_at` (08:00 by default, `""` turns it off), and only when something is wrong.

The AI gets the household, today's and tomorrow's plates, the menu of this week and the next with the paused days, the recipe list, this week's shopping list with what is ticked and what is at home, the food bought that no dish uses any more (it is told to put it back in the menu or say how to keep it, never to let it go to waste), and the last week of logs. So after a shop it knows what was not found and can fit the menu to it. In a private chat it sees only the asker's own pills, notes, weigh-ins and logs. In the group it leaves out weigh-ins, binges, cravings, nausea and notes unless `bot.share_logs_in_group` is set. Group messages are kept as context only when the AI is asked (`bot.history_scope: all` keeps them all). It is told never to change a dose and never to suggest eating less after a binge.

Everything the AI sees goes to the provider you configure. With a local model nothing leaves your network. A provider with web search can find a video or a recipe for a dish; it is told to compare it with your recipe and say what differs. It is also told to look up a fact the household's data does not hold (a nutrition guideline, what a food contains), answer with the figure and its source, and only then say what a clinician should confirm, instead of sending the person to ask their doctor.

## The consistency check

The plan, the batch days, the cook dates and the chat's reminders are written at different times, by the planner, the chat and people editing the data, so they can drift apart. `doctor.problems(data, db, today)` reads them all and lists what no longer fits, with what to change. It is plain code with no AI, and it only reads. It looks from today to the end of next week, the menu the AI sees:

- **A session on a day that is not a batch day.** A batch session still to come on a day in `batch.avoid_days`, or on a day that is not in `batch.preferred_days` while the batch day the planner would pick for that half of the week (Monday to Wednesday, Thursday to Sunday) has no session. A one-off session beside the usual one is left alone, and so is one the planner moved because the batch day's afternoon was busy. Sessions cooked the morning a lunch is eaten, and the ones a pause moved to the first day back, are not batch sessions.
- **One dish cooked twice.** One recipe is lunch on two days in a row, cooked on two different days, when one cook still to come keeps for both. The Cook page shows it as two dishes.
- **A lunch that does not keep.** A lunch eaten today or later is cooked longer before its day than the recipe keeps in the fridge, and it does not freeze.
- **A lunch nobody cooked.** A lunch eaten today or later says it was cooked on a day already past, and no session of that day cooked that dish for that day. The Cook page never showed it on that day, so nobody made it.
- **A dish that counts on a session after it.** A dish whose recipe has `ahead` steps for the cooking session before its day, when no session comes in the 3 days before (a session on that day counts as after the meal), and no reminder has someone do it at home instead.
- **Prep with no reminder.** A dish that needs something done before its day (a dried pulse to soak the night before it is cooked, an `ahead` step before the meal) and no reminder linked to that dish and day, set or already sent in any chat, comes in time. When it is already too late, the fix it gives is to move the dish. Lunches cooked in a batch session are left out: the batch sheet and the batch reminder carry them.
- **A reminder about a dish the menu dropped.** A chat reminder linked to a dish and its day, when that day's menu no longer has the dish. The app cancels these itself.
- **A recipe nobody can have.** A recipe on some meal that uses a food someone avoids. The planner never picks it, so it never reaches the menu and nothing else looks at it again: swap that food or retire the recipe with `meals: []`. The line does not say who avoids the food, and it is left out of the morning report (it stays true until someone edits the recipe); the AI sees it every turn.
- **Food that goes off in the pantry.** Food the pantry holds that goes off, by its `keeps_days`, before the plan cooking as planned up to the end of next week uses it up.
- **Food bought and on no dish**, as the AI already sees it, without the food the line above already names.

The AI gets what the check found in its facts as "Problems the app found: fix them", each with the menu change that fixes it. Food bought and on no dish keeps its own section there, and is never posted in the chat by the check: once it is frozen it can stay true for days. Reminders about a dish the menu dropped stay out of it, because they may belong to another chat, a private one; the app cancels them and says so in their own chat. The AI also gets the limits the app's validation enforces on what a change may set (`sessions_per_week` 1 to 2, a recipe's `fridge_days` 0 to 4, a shift of 1 to 4 weeks, and so on), read from the code that enforces them, so it never proposes a value the app refuses.

## The verifier

An answer whose change block touches the menu (`plan`), a recipe (`recipe`, `recipes`) or the batch days is checked before anyone sees it. Other answers go out as they are.

1. **Structural, plain code.** The change is made on a copy of the data folder, and the consistency check runs on the copy, with the reminders the answer offers counted as set. What it finds that was not there before is the change's doing, and so is a change the app would refuse.
2. **Judgment, one call to the same AI provider.** Three checks, each answered on its own: does it match what the household asked for (each person's notes and chat notes, the household's own instructions, the weekly rules); the timeline (what is cooked when, which session prepares what, what is soaked, defrosted or cooked ahead, each with a reminder at the right time); health (the health export's allergies, intolerances, diet rules, medicines' food rules and fasting). A fourth catches plain nonsense, such as a step with equipment the kitchen does not have, an oven step without the air-fryer alternative, or ultra-processed food where real food would do.

When either finds something, the answer goes back to the AI once, in the same conversation, with the problems, and its new answer replaces the first. One check and at most one revision per answer: what the revision cannot fix, it says in its answer, and a change that still leaves a structural problem gets the consistency check's line once it is made. When the check or the revision fails, the first answer goes out as it was. The verifier logs only how many problems each check found, never their text.

The same checks run on the plan as it stands. Every 10 minutes, when no answer is being written, the bot builds the brief of this week and the next: the menu, what the consistency check finds, and the full text of every recipe on it (up to 150,000 characters; any left out are named, so they are never judged by their name). The judgment runs only when that brief changed since the last run, so a new or regenerated week, a change from the chat or an edit to a recipe gets one call. What it finds is kept with the brief's key and goes into the AI's facts under "Problems the app found" until the plan changes. A private chat sees only the timeline and the plain mistakes, since the others can name someone else's notes or health. Once a day, after the morning consistency check (or as soon as a check still running then ends, up to 2 hours later), the bot asks itself to fix what was found, like the weekly review, so the chef proposes the changes in the chat; a plan raised once is not raised again. A recipe nobody can have stays out of this check and out of private chats, since it names a food someone avoids.

## Other chat apps

Telegram is the chat app that ships. The commands, reminders and AI answers do not depend on it: a WhatsApp, Signal or Matrix adapter only has to receive messages and voice notes and send text, and it can be added beside `bot.py` without changing the rest.
