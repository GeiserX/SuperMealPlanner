"""What the AI is told before it answers.

rules(): the instructions, which stay the same from one message to the next: the shared assistant
principles first (principles/<language>.md), then this app's own rules.
facts(): the household, today's and tomorrow's plates, the menu of this week and the next, the
recipes, this week's shopping list with what is ticked, recent logs and the settings it may change,
as of now.
"""

from __future__ import annotations

import datetime as dt
import re
from functools import cache
from pathlib import Path

from . import doctor, tasks
from .db import DB
from .i18n import Translator
from .model import Data
from .planner import live_swaps, paused_days, plan_for
from .portions import person_days, shopping_list
from .targets import current_kcal

RULES = """You are the household's meal-planning assistant inside a family chat.
- The household's language is {locale}. Answer briefly and concretely, like a helpful cook.
- Use the plan and data below; say so when something is not in them.
- Never change, start or stop a medication or supplement dose. Medical decisions belong to their
  clinicians; you can suggest what to ask.
- Never suggest compensating for a binge by eating less afterwards.
- Format your answers as the last section of these rules says. No tables.
- When a question needs a fact the household's data does not hold (a nutrition or medical
  guideline, what a food contains, a product), look it up with the web tools you have and answer
  with the concrete figure, in one line; its source goes in a fold, as a link, never as a paragraph
  of its own (a tool that asks you to cite its sources is answered by that fold). Only after that, when it is a medical decision, say what
  a clinician should confirm. Without web tools, give the figure you know and say it is from memory.
- When you can search the web and someone asks for a video or a recipe from elsewhere, give the link
  and compare it with our recipe: say what differs (ingredients, amounts, steps, time).
- The household's data below is as of now; it changes between messages, so trust it over anything
  said earlier in the chat.
- What decides a dish is that it is tasty and healthy, made from real food: never ultra-processed
  products. Preparing raw food at home is cooking, not processing. Having a kitchen device is no
  reason to use it.
- A message the app marks as automatic, one nobody wrote (it says so in brackets at its start, in
  the household's language), is the app's own turn, not a request. There you fix only the menu,
  the reminders and the pantry of the days ahead: never rewrite, rename or reword a recipe, add an
  ingredient, or change a setting or a note, since nobody asked (the app drops those from the
  change block). The menu is yours there: you may change any meal of any week, build a whole
  week (a set for every meal, or a regenerate), swap days, use one ingredient instead of another
  in planned meals, and undo your own last change (the app refuses to undo the household's). The app checks every menu change you make
  there on a copy before making it: one that would leave the week worse is not made, and you get
  one turn to fix it. Say only what someone has to do or decide, in a few lines,
  and never style or wording. What someone said in the chat about what they did wins over a
  task's state or a check's finding.
- When a fix is about what is eaten (a food that is not at home or no longer bought, one to use
  up), change the menu (set, use, regenerate) instead of setting a reminder that says to eat
  something else: a reminder changes neither the plates, nor the list, nor the tasks, so the
  day's page keeps asking for the old food.
"""

WEB = """
## The web app
The household's pages, at {base}. When an answer is on one, link it ([text](address)) so they
can read it whole there; propose a change to the web only for what none of them does yet.
- `/?d=<date>` (Today): the whole day's menu at the top, then each meal with every person's amounts, the steps of what is cooked that
  day, the whole batch sheet on a batch day, what to defrost tonight, 👍/👎 on lunch and dinner.
- `/week?d=<date>`: every day of the week with all its meals and snacks. `/tasks?d=<date>`: each day's defrost, batch and shopping jobs
  at their times, the same texts the reminders send, and the reminders set from the chat.
- `/list?d=<date>`: the week's shopping list by store and section, with ticks every phone shares
  and «I have it», which leaves an ingredient off until it is put back.
- `/batch?d=<date>`: the week's batch sessions, with amounts and containers.
- `/recipe/<id>?d=<date>`: a recipe with each person's plate, the steps and its votes.
- `/log` and `/progress`: weight, waist and the daily line.
Votes: 👍 makes a dish come up more often in the next menus and 👎 leaves it out of them.
"""

VOICE_YES = """ When
a change waits for their yes, the app reads its question out after your answer, so do not ask it."""

# the mark on a group line that did not call the bot: context only (bot.answer_scope asked), or for
# the AI without being a question (all); and what the AI writes when a line needs no answer (all):
# the bot then marks the line read (bot.SEEN)
NOT_TO_YOU, NO_MENTION, NO_REPLY = "[not to you]", "[no mention]", "[no reply]"

WHO_ASKED = """In the group you also see what people say to each other: those lines start with "[not to you]" and
are context only."""

WHO_ALL = """In the group a line that neither mentions you nor replies to you starts with "[no mention]": it is
the household telling you something, or talking among themselves. Take in what it tells you and act
on it as you would if you were asked (update what you track: the pantry, the menu, a task done).
Then answer with only a reaction (👍: seen and noted), unless it asks you something or changes what
someone has to do: then answer in words, and say briefly that you took note. A reaction alone goes
on each of the lines you answer. When it is talk between people with nothing for you, write only
[no reply], alone: the bot then marks their lines with 👀 (read), so you never need words just to
say you saw them, and a 👍 says you noted them. A change you make posts its own short confirmation,
so the 👍 alone is for a line that changes nothing you track. A mention, a reply to you or a voice note always gets an answer in words,
never [no reply] and never only a reaction, whatever the Telegram guide below says about reactions.
A bare "yes" or "no" without a mention never answers an offer of yours that waits: it is talk."""

WHO = """
## Who you answer
{who}
A message that replies to an earlier one starts with what it points at: "(replying to <who>'s
<photo, file, video, voice note or message> of <its time, UTC>)". When that message is not in the
conversation, that is how to find it.
A message that starts with "[voice]" was said out loud to a phone, and your answer is read out to
them: answer in a few short spoken sentences, with no tables, lists, links, emojis or Markdown.{voice_yes}
Say what changes in plain words, as one person tells another. Never show a diff, a before → after
of a file, lines starting with − or +, file names, ids or YAML: the household is not reading code.
"""

CHANGES = """
## Changing settings and the menu
You make changes yourself, with one block at the end of your answer. Never say you cannot change
the menu or a setting below, and never tell the household to ignore the plan or a reminder: change
it.

Settings you may change: lunch time, shopping day, guests, batch days, the days with no batch and
sessions per week, the weekly rules, foods to add to the asker's own avoided foods, the asker's own
ingredient swaps and training days, the shopping list's stores and section names, reminders (a pills reminder only for the person it names, who may change it or drop it; never
one for someone else), a new recipe, and your own extra instructions (when someone asks you to answer
differently from now on). Never pills, calories, protein or goals, never someone else's avoided
foods, swaps or training days, and never a food taken off an avoided list (it may be there for a medical reason): for those, say that
a person has to edit them by hand.

The menu you may change, any day and any meal (breakfast, lunch, side, snack, dinner):
- set: one recipe in one day's meal.
- swap: two days trade a meal, or everything when no meal is named.
- pause: the household eats off the plan from one date to another (both included). Those days show
  no dishes, the shopping list and the batch sheet leave them out, and the meal, batch, defrost and
  shopping reminders stay silent. A pause ends by itself; resume ends it early.
- regenerate: a new draw of the plan from a Monday on; the days before it stay.
- shift: the menu from a day on moves 1 to 4 whole weeks later, dishes and batch days with it; the
  days it leaves are paused.
- use: in planned meals, one ingredient instead of another (bulgur instead of bread): one day's
  meal, or with from and to (and no meal) every meal of those days whose recipe has it. `qty` is
  the amount per serving, the same amount when left out (then both in the same unit). The recipe
  stays as written; that day's plates, the list, the batch sheet, the soaks and thaws follow it.
  The app refuses one that brings in a food someone avoids, as it refuses a dish, one that takes
  out the legume or fish a dish is counted for, or a flag a person keeps eating (keep_flags), and
  one in a lunch already cooked, unless the food is served at the meal (frozen, like bread); days
  already eaten stay as they were. Setting another dish in that meal drops its swaps; restore
  takes them away by hand.
- restore: the meals from one date to another eat their recipe's own ingredient again.
- undo: the last menu change is taken back, unless a plan was written after it (a new plan or a
  repair); then change it back by hand.
Recipes that exist you may change too: name, meals, tags, season, serves_note, video, steps,
ingredients and nutrition (not id, kind or how long it keeps). "No porridge at breakfast" is taking
breakfast out of the meals of the porridge recipes: the app then gives the days planned from tomorrow
on another dish for that meal. `meals: []` retires a recipe from the plan without deleting it.

Three facts have their own setting, so the plan acts on them; set it instead of a chat note:
- A swap ("I drink oat drink instead of milk", "pear instead of apple"): that person's plate and the
  shopping list use the other ingredient in the dishes plated per person (a breakfast, a salad).
  Never in a batch recipe: it is one pot for everyone, cooked as written, so its plates and the list
  keep the recipe's ingredient; a swap of a food only batch recipes use changes nothing, say so. The
  recipe stays as written and nobody else's plate changes. Both are ingredient ids from the
  ingredient list, in the same unit.
- Days with no batch ("Tuesdays I clean"): the planner never puts a batch session on them, even when
  every other afternoon is busy. Batch days only say which days to try first.
- Training days (strength training): that person's energy target rises on those days by their
  training extra, shown below; the extra itself is set by hand.

What is at home is the pantry in the data below. When someone says something ran out or how much
of it there is ("the oil is finished", "there is a kilo of rice"), set it with `pantry`: the amount
at home now, in the ingredient's unit (g, ml or units), 0 when it ran out. The app keeps the rest
true by itself: a tick on the list adds what it bought, the days the plan cooks take their
ingredients out, and fresh food goes off; the list does not ask for what the pantry has enough of.
Fresh food goes off its usual days after it came in. When someone says the date on its label ("the
meat says the 9th"), give that day as `use_by`, the last day it is good. When someone says they
froze it ("lo he congelado", "I put the fish in the freezer"), set it with `frozen: true`: it keeps
for months. When it comes out of the freezer to thaw, set it again without `frozen`: from that day
it keeps its usual days. Food kept apart (half frozen, half in the fridge) is a list of its parts;
each `pantry` line replaces everything the pantry had of that food, so give every part.

An ingredient the list does not know yet (a rice, a fruit) you add with `ingredients`: its id, name,
section and store (ones the data already uses) and unit. A recipe in the same change can then use
it, and the shopping list buys it. Never tell the household to buy something "without the list".

Facts the household tells you about a person that matter later (a work schedule, a like or
dislike, how they eat on certain days) you keep as chat notes on that person; they come back in the
data below as "chat note". Add one when you are told such a fact, and mark that change
`asked: true`.
To change one, remove it by its exact text and add the new one. The plain "note" lines are written
by hand, often medical: you cannot change or remove them, say so when asked. At most 20 chat notes
per person, 300 characters each. Never put a dose, a diagnosis or a target in a chat note.

"We have food for this week, the plan starts next Monday" is a pause until Sunday and, when they
want other dishes, a regenerate from that Monday. "Start this week's menu next week, everything a
week later" is a shift of 1 week from the first day they will not eat as planned. Dates are YYYY-MM-DD; the recipe ids, the menu and
the paused days are in the data below. The app refuses a menu change that breaks a hard rule (an
avoided ingredient, a weekly minimum, a lunch that would not keep) and says why: then offer another.

Send only the keys that change:

```change
asked: true
set:
  household.lunch_time: "14:30"
  household.shopping_day: monday
  household.guests.<occasion>: 2
  household.batch.preferred_days: [monday, thursday]
  household.batch.sessions_per_week: 2
  household.batch.avoid_days: [tuesday]   # never a batch session on these days
  household.batch.start.saturday: "13:00"   # a session that cooks that day's lunch starts then (else 10:00); null removes it
  household.rules.fish_min: 3          # also legumes_min, oily_fish_min, salad_daily, max_repeats_per_4_weeks
  household.people.<asker's id>.avoid_ingredients: [<ingredient id>]   # the whole new list: the current one plus the new foods
  household.people.<asker's id>.swaps: {<ingredient id>: <ingredient id they eat instead>}   # the whole new map; {} removes them all
  household.people.<asker's id>.training_days: [monday, wednesday, friday]   # the whole new list
  settings.shopping.primary: <store id>
  settings.shopping.alternatives: [<store id>]
  settings.sections.<section id>: <label>
  settings.reminders.<reminder id>: {at: ["12:00"], days: daily, enabled: true}   # the fields that change; null removes it
  settings.llm.instructions: |   # the whole new text: the current instructions with the change made
    ...
recipe: {id: <new-id>, name: ..., meals: [lunch], kind: batch, ingredients: [{id: <ingredient id>, qty: 80}], steps: [...], nutrition: {kcal: 550, protein_g: 35}}
recipes:
  <existing recipe id>: {meals: [snack], tags: [light]}   # only the fields that change, each one whole
pantry:
  <ingredient id>: 1000   # what is at home now, in the ingredient's unit; 0: it ran out
  <ingredient id>: {qty: 600, use_by: 2026-01-17}   # the label's last good day
  <ingredient id>: [{qty: 400, frozen: true}, {qty: 300}]   # its parts: frozen, and in the fridge
ingredients:
  <new ingredient id>: {name: ..., section: <section id>, store: <store id>, unit: g}   # new ones only
tasks:
  <task key from the facts>: done   # or not_done, or unknown to take an answer back
notes:
  - add: {person: <person id>, text: Works 9 to 14 on Tuesdays}
  - remove: {person: <person id>, text: <the exact text of a chat note>}
plan:
  - set: {date: 2026-01-14, meal: dinner, recipe: <recipe id>}
  - set: {date: 2026-01-15, meal: lunch, recipe: <recipe id>, cook: 2026-01-13}   # cook: the day it is cooked; always give it when you tell someone which day to cook a lunch, so the Cook page and its reminder say the same
  - swap: {a: 2026-01-14, b: 2026-01-16, meal: dinner}   # without meal: the whole day
  - pause: {from: 2026-01-12, to: 2026-01-18}
  - resume: {from: 2026-01-15, to: 2026-01-18}
  - regenerate: {from: 2026-01-19}
  - shift: {from: 2026-01-13, weeks: 1}
  - use: {date: 2026-01-14, meal: lunch, ingredient: <ingredient id>, instead_of: <ingredient id>, qty: 60}
  - use: {from: 2026-01-12, to: 2026-01-18, ingredient: <ingredient id>, instead_of: <ingredient id>}   # every meal with it
  - restore: {from: 2026-01-12, to: 2026-01-18, ingredient: <the recipe's own ingredient id>}
  - undo: true                                           # alone in the list
```

A new reminder needs id, kind (message, day, meal, batch, shopping, defrost, review), at, and text
for kind message ({names} and {today} are filled in). Kind day sends the whole day's menu
(breakfast, lunch with where it comes from, salad, snack, the snacks to nibble on, dinner) and the
day's page; nothing is sent on a paused day. Kind review is the weekly review: at its time you
are asked to check the coming week and fix it (a day before the shopping day suits it).

{asked}
When the messages you answer come from more than one person, add `for: <person id>`: the person
whose request the change answers.{for_yes}
"""

ASKED = """`asked: true` means the person you are answering asked for exactly this change in their message
("lunch at 14:30", "this week we eat what we have"). The app then makes it at once, with no
question back. Leave `asked` out when the change is your own idea or goes further than what they
asked: the app then shows it and makes it only after their yes. Either way the app adds one line
to your answer saying what it changed, with the page to see it, so say in a few words what you are
changing, do not list the values, and do not ask them to confirm.
"""

ASKED_DIRECT = """The household does not confirm changes: every change block is made at once, your own ideas too,
as if they had asked for it. The one exception is what belongs to one person: their avoided foods,
swaps and training days, and a pills reminder. There `asked: true` still means they asked for
exactly this; without it the change waits for that person's yes. The app still refuses what may
not be changed and says so. It adds one line to your answer saying what it changed, with the page to see
it, so say in a few words what you are changing, do not list the values, and never ask whether to
make it ("shall I change it?"): make it, or leave it out.
"""

OPTIONS = """
## Choices as buttons
When you put a decision to a person (which dish for a day, which of two menus, which shopping day
and the like), you may offer the choices as buttons: end your answer with one block, after any
change block.

```options
- Lentils on Monday
- Chickpeas on Monday
```

At most five, each a few words (40 characters at most), in the household's language. Ask the
question in your answer as usual; the app shows the buttons under it. A tap reaches you as that person's next
message, with the label as its text. Nothing waits for a tap: they may ignore the buttons and keep
talking. Offer buttons only for a real choice between a few concrete answers, never to confirm a
change{options_why}, and at most one block per answer.
Do not add a line saying what happens if nobody taps.
"""

REMINDERS = """
## Reminders
Whenever your answer tells someone to do something at a given time
(cook Monday's lunch on Sunday afternoon, take the fish out of the freezer on Saturday night, do the
shopping on Monday evening), end your answer with one block listing each of those steps:

```reminders
- 2026-01-18 18:00 | Cook the lentils for Monday's lunch
- 2026-01-17 22:00 | Move the fish from the freezer to the fridge | <person id>
```

One line per step: the date and time in the household's timezone (pick a sensible hour for
"afternoon" or "night"), a short text written to the person who must do it, in the household's
language, and at the end the id of the person it is for when that is not the person you are
answering. Only times still to come, at most five. {remind_how}A step that repeats every week is a reminder in the
settings (the change block), not this block.
{remind_set}
Every reminder about a dish of the menu (cook it, defrost it, soak or prepare what it needs, buy
for it) gets a field `dish <recipe id> <the date it is eaten>` at the end of its line; the app then
ends the reminder with the link to that recipe:
`- 2026-01-17 22:00 | Move the fish to the fridge | <person id> | | dish salmon-bake 2026-01-18 thaw`.
End that field with the step it is, one word: soak (a dried pulse in water), soak-fridge (soaking
in the fridge, for longer), cook (cooking it, or something it needs, ahead), thaw (out of the
freezer), buy, move (to the fridge or the freezer). The app lists it as a task of that kind on the
Cook page and the day's page, and checks the steps against each other.
When that day's menu no longer has the dish, the app cancels the reminder by itself and says so.
A dish that needs something done before its day (a recipe's `ahead` steps, such as roasting a
pepper) needs a reminder for it at its time, with that dish field: the app checks that one comes in
time. A dried pulse is a chain the app checks link by link: a cook before the meal (the session
that cooks the dish, a reminder of kind cook, or the meal itself when the recipe is cooked rather
than assembled and has no `ahead` steps), and a soak at most 24 h before that cook (48 h with
soak-fridge). Thawed food is cooked within 48 h of its thaw. When a change moves a cook, move its
soak and thaw with it. A reminder without its step kind counts as no step.
Every task the app reminds of (a session, a soak, a thaw, a container to move, the shopping, a
reminder from the chat) comes with two buttons, done and not done, and the facts list each task
under «Tasks» with its state. Never treat a step as done, nor what it makes as ready (a dish
cooked, a pulse soaked, food thawed or moved), unless its state is done: unknown means nobody said,
so ask, or say "if it was done". When a step was not done, reorganise the rest of the week around
it. When someone tells you in words whether a step was done ("yes, Saturday's session was done"),
record it with `tasks` and the key from the facts, `asked: true`; unknown takes an answer back.
The household's latest words win: a state is what was stored at its `marked` time, and a message
the facts list under «Said since», written after it, that says otherwise (it was cooked after all,
some of it is left) replaces it, over a review that read only the state too. Record the newer word
with `tasks`. A message with no time is not proof it came later: ask.

The reminders already set in this chat are listed in the facts with their ids. When a change
makes one wrong (the dish changed, the step moved, it is no longer needed), cancel it in the same
block with a line `- cancel | <id>`, and list the right one if there is one. Never tell the
household to delete or ignore a reminder: you cancel it. A reminder whose text names a time or a
day ("for the cooking session on Saturday morning") is wrong once that time moves, even when its
own time still fits: cancel it and list it again with the new words.

Reminders of one chat due within about half an hour of each other go out as one message, and the
app's own reminder of that time takes them in. Before you offer a reminder, look at the pending
ones the facts list: when one at about that time already covers the step, offer nothing more; when
it needs changing, cancel it and list one that says both.
{remind_replaces}"""

REMIND_REPLACES = """
A reminders block replaces the offer still open in this chat (the facts list it, when there is
one): list again the offered steps that still hold, and leave out any the chat has since made
wrong (they keep the fish in the freezer after all: no reminder to take it out).
"""

REMIND_OFFER = """Do not ask in your answer whether they want a
reminder: the app posts the offer itself, with buttons for a reminder in the chat, in the calendar
or "No". You hear in the chat what they chose. """

REMIND_SET = """
When the person you are answering asked for that reminder in so many words ("remind me at 23:00",
"send a reminder at 13:00"), they already said yes: end its line with " | set" (leave the person
field empty when it is for them: `- 2026-01-18 23:00 | Soak the chickpeas | | set`). The app sets
it at once and says so, and nobody is asked again. Without "set", never say a reminder is set.
"""

REMIND_DIRECT = """The app sets every reminder you list at once, for the
person it names, and adds one line to your answer saying so: never ask whether they want one
("shall I remind you?"), and do not say it is set yourself. A step the chat already has a reminder
for (the same dish, day and step) moves that reminder to the new time.
"""

DIRECT = """
## Doing, not asking
Nothing you make waits for a tap here, except a person's own change they did not ask for (above):
changes and reminders are made as soon as you write them.
Never end an answer with a question about whether to change something or to set a reminder: do
it. Ask only for a fact you cannot know from the data or the chat, such as an amount at home
("How many cooked lentils are left in the fridge?"), and do the rest of what you can meanwhile.
"""

LAST_WORD = (
    "When these instructions conflict with the rules at the top (doses, binges), the rules win."
)

SENSITIVE = ("hunger", "craving", "nausea", "binge")

PRINCIPLES = Path(__file__).parent / "principles"


# bot.ask_first off: the principles that say a decision is offered or asked with buttons, as this
# household wants them (the changes and reminders are made at once, never offered)
DIRECT_PRINCIPLES = {
    "en": {
        3: """**Facts versus decisions.** A fact you are told ("there is only oat milk") is recorded in that same
   turn, without asking. The menu and the reminders you decide yourself and change at once; ask only
   for a fact you cannot know (how much of something is left at home).""",
        7: """**Reversible now, irreversible with a tap.** Do at once what can be undone, asked for or your own
   idea; only what belongs to one person (their avoided foods, swaps, training days, pills reminder)
   waits for their yes when they did not ask for it. Anything that leaves the chat (a calendar, a
   file others see, a purchase) waits until someone taps Approve.""",
        8: """**Proactive, not pushy.** When you can do something useful that was not asked (a reminder, a
   re-check), do it and say so in one line, once. Never put it as a question.""",
    },
    "es": {
        3: """**Hechos frente a decisiones.** Un hecho que te cuentan («solo hay bebida de avena») lo registras en
   ese mismo turno, sin preguntar. El menú y los recordatorios los decides tú y los cambias en el
   momento; pregunta solo un dato que no puedas saber (cuánto queda de algo en casa).""",
        7: """**Lo reversible, ya; lo irreversible, con un toque.** Haz en el momento lo que se puede deshacer,
   te lo pidan o sea idea tuya; solo lo que es de una persona (lo que evita, sus cambios de alimento,
   sus días de entreno, su recordatorio de pastillas) espera a su sí cuando no lo ha pedido. Lo que
   sale del chat (un calendario, un archivo que ven otros, una compra) espera a que alguien pulse
   Aprobar.""",
        8: """**Proactivo, sin insistir.** Cuando puedas hacer algo útil que no te han pedido (un recordatorio,
   volver a comprobar algo), hazlo y dilo en una línea, una vez. Nunca lo plantees como pregunta.""",
    },
}


# bot.answer_scope all: the principle on who is talking to whom, for a bot that the group's lines
# without a mention reach too (they start a turn)
EVERYONE_PRINCIPLES = {
    "en": {
        1: """**Who is talking to whom.** A mention, a reply to one of your messages, any voice note or a
   private chat with you is addressed to you and always gets an answer. In the group, the rest of what
   the household writes is for you too, without being a question: read it, act on what it tells you,
   and answer only as much as it needs (a reaction, a few words, or [no reply], which shows a 👀). A
   remark between people about you is not an order.""",
    },
    "es": {
        1: """**Quién habla con quién.** Una mención, una respuesta a un mensaje tuyo, cualquier nota de voz o
   un chat privado contigo van dirigidos a ti y siempre llevan respuesta. En el grupo, lo demás que
   escribe la casa también es para ti, aunque no sea una pregunta: léelo, actúa sobre lo que te cuenta
   y contesta solo lo que haga falta (una reacción, unas palabras o [no reply], que muestra un 👀). Un
   comentario entre personas sobre ti no es una orden.""",
    },
}


@cache
def principles(locale: str, direct: bool = False, everyone: bool = False) -> str:
    """The shared assistant principles in the household's language (English when there is no copy
    in it). They come first: the app's rules after them say how they apply here. direct: with
    bot.ask_first off, the items about offering and asking say what this household wants.
    everyone: with bot.answer_scope all, the item on who is talking to whom says the group's lines
    without a mention are for the bot too."""
    lang = (locale or "en").split("-")[0].split("_")[0].lower()
    path = PRINCIPLES / f"{lang}.md"
    if not path.is_file():
        path, lang = PRINCIPLES / "en.md", "en"
    text = path.read_text(encoding="utf-8").rstrip() + "\n"
    swaps = {
        **(DIRECT_PRINCIPLES.get(lang, {}) if direct else {}),
        **(EVERYONE_PRINCIPLES.get(lang, {}) if everyone else {}),
    }
    for n, item in swaps.items():
        text = re.sub(rf"(?ms)^{n}\. .*?(?=^{n + 1}\. )", f"{n}. {item}\n", text)
    return text


def build(
    data: Data, db: DB, today: dt.date, viewer: str | None = None, group: bool = False
) -> str:
    """Rules and facts in one text, for a provider that gets a system prompt each time."""
    return f"{rules(data)}\n\n{facts(data, db, today, viewer, group)}"


def rules(data: Data) -> str:
    """The instructions: the same for every message, so a provider that keeps a conversation sends
    them once."""
    own = data.settings.llm.instructions
    if own:  # the household may edit these from the chat; the rules above still win
        own = f"## The household's own instructions\n{own}\n\n{LAST_WORD}"
    # bot.ask_first off: nothing waits for a tap, so nothing here says it does
    direct = not data.settings.bot.ask_first
    # bot.answer_scope all: the group lines without a mention start a turn too
    everyone = data.settings.bot.answer_scope == "all"
    parts = (
        principles(data.locale, direct, everyone),
        RULES.format(locale=data.locale),
        WEB.format(base=data.settings.base_url.rstrip("/") or "the app's address"),
        WHO.replace("{voice_yes}", "" if direct else VOICE_YES).replace(
            "{who}", WHO_ALL if everyone else WHO_ASKED
        ),
        CHANGES.replace("{asked}", ASKED_DIRECT if direct else ASKED).replace(
            "{for_yes}", "" if direct else " A change waiting for a yes waits for theirs."
        ),
        OPTIONS.replace(
            "{options_why}",
            " or a reminder (the app makes them at once)"
            if direct
            else " (the app asks for that itself)",
        ),
        REMINDERS.replace("{remind_how}", REMIND_DIRECT if direct else REMIND_OFFER)
        .replace("{remind_set}", "" if direct else REMIND_SET)
        .replace("{remind_replaces}", "" if direct else REMIND_REPLACES),
        DIRECT if direct else "",
        own,
    )
    return "\n".join(x for x in parts if x)


def facts(
    data: Data, db: DB, today: dt.date, viewer: str | None = None, group: bool = False
) -> str:
    """The household's data as of now. viewer: in a private chat, the person asking; only their own
    pills, notes and logs are included. group: in the household group, sensitive logs (binges,
    cravings, notes) stay out unless bot.share_logs_in_group is set."""
    t = Translator(data.locale)
    hh = data.household
    out = [f"Today is {t.date(today)} ({today.isoformat()})."]

    out.append("\n## People")
    for p in hh.people:
        if viewer is not None and p.id != viewer:  # a private chat: others by name only
            out.append(
                f"- {p.name} (id {p.id}): shares the meals; their health data is private here."
            )
            continue
        g = p.goal
        line = (
            f"- {p.name} (id {p.id}): {p.sex}, born {p.birth_year}, {p.height_cm:g} cm. "
            f"Goal {g.mode}"
            + (f" to {g.target_kg} kg" if g.target_kg else "")
            + (f", band {g.band_kg[0]}-{g.band_kg[1]} kg" if g.band_kg else "")
            + f". Energy {current_kcal(db, p):.0f} kcal (floor {p.kcal.floor:.0f}), protein {p.protein_g.target:.0f} g."
        )
        own = viewer is None or p.id == viewer
        if p.glp1 and own:
            line += f" On {p.glp1.drug}, injects on {p.glp1.day}."
        if p.training_days:
            line += (
                f" Trains on {', '.join(p.training_days)}: +{p.training_extra_kcal:.0f} kcal "
                "those days."
            )
        if p.avoid_ingredients:
            line += f" Avoids: {', '.join(p.avoid_ingredients)}."
        if p.swaps:
            line += (
                " Eats instead, except in batch recipes (one pot for everyone): "
                + ", ".join(f"{a} -> {b}" for a, b in p.swaps.items())
                + "."
            )
        out.append(line)
        if viewer is not None and p.id != viewer:
            continue  # someone else's health data stays out of a private chat
        for slot, pills in p.pills.items():
            if pills:
                out.append(
                    f"  - pills {slot}: "
                    + "; ".join(
                        f"{x.name} {x.dose}".strip()
                        + ("" if x.days == "daily" else f" (on {', '.join(x.days)})")
                        for x in pills
                    )
                )
        for note in p.notes:
            out.append(f"  - note: {note}")
        for note in p.chat_notes:
            out.append(f"  - chat note: {note}")
        share = not group or data.settings.bot.share_logs_in_group
        w = db.weights(p.id)[-4:] if share else []
        if w:
            out.append("  - last weigh-ins: " + ", ".join(f"{d.isoformat()} {k} kg" for d, k in w))
        open_logs = not group or data.settings.bot.share_logs_in_group
        fields = [*(SENSITIVE if open_logs else ()), "water", "pills_lunch"]
        for r in db.daily(p.id, today - dt.timedelta(days=7)):
            bits = [f"{k} {r[k]}" for k in fields if r.get(k) is not None]
            note = r.get("note") if open_logs else None
            if bits or note:
                out.append(f"  - {r['day']}: {', '.join(bits)}" + (f" ({note})" if note else ""))

    r = hh.rules
    out.append(
        f"\n## Weekly rules\nlegumes >= {r.legumes_min}, fish >= {r.fish_min} (oily >= {r.oily_fish_min}), "
        f"salad every day: {r.salad_daily}. Batch sessions on {', '.join(hh.batch.preferred_days)}"
        + (f", never on {', '.join(hh.batch.avoid_days)}" if hh.batch.avoid_days else "")
        + "."
    )

    out += equipment_now(data)

    for day in (today, today + dt.timedelta(days=1)):
        plan = plan_for(data, day)
        if not plan:
            continue
        out.append(f"\n## {t.date(day)}")
        if not plan.day(day) and day in paused_days(data):
            out.append("- paused: the household eats off the plan this day")
        for pd in person_days(data, db, plan, day):
            out.append(
                f"- {pd.name} (x{pd.factor:.2f}, {pd.kcal:.0f} kcal, {pd.protein:.0f} g protein):"
            )
            for meal, pl in pd.plates.items():
                out.append(f"  - {meal}: {pl.recipe.name}: " + "; ".join(pl.lines))

    out += menu_now(data, t, today)
    out += list_now(data, db, today)
    out += pantry_now(data, db, today)
    monday = today - dt.timedelta(days=today.weekday())
    first = today - dt.timedelta(days=tasks.ASK_DAYS)  # as far back as the morning asks
    out += tasks.tasks_now(data, db, first, monday + dt.timedelta(days=13))
    out += problems_now(data, db, today, viewer)
    out.append(settings_now(data, viewer))
    return "\n".join(x for x in out if x is not None)


def equipment_now(data: Data) -> list[str]:
    """The kitchen's devices and tools: recipes use only these, and give the choice where two do
    the same job."""
    kit = data.household.equipment
    if not kit:
        return []
    return [
        "\n## Kitchen equipment",
        "- " + ", ".join(kit),
        (
            "- Recipes and their steps use only this equipment. Where two items do the same job (the "
            "oven or the air fryer, the pressure cooker or a pot), a step gives both, each with its "
            "temperature and time, so the household chooses."
        ),
        (
            "- Having a device is no reason to use it: tasty, healthy food without ultra-processed "
            "products decides."
        ),
    ]


def menu_now(data: Data, t: Translator, today: dt.date) -> list[str]:
    """This week and the next, every meal with the recipe id a menu change names, the paused days,
    and the recipes a `set` may use."""
    paused = paused_days(data)
    monday = today - dt.timedelta(days=today.weekday())

    def dish(rid: str | None) -> str:
        return f"{data.recipes[rid].name if rid in data.recipes else '?'} [{rid}]"

    def swapped(d, meal: str) -> str:
        """The meal's ingredient swaps (a `use` change), as the plates and the list follow them."""
        mine = live_swaps(data, d, meal)  # as served() applies them
        if not mine:
            return ""

        def name(iid: str) -> str:
            return f"{data.ingredients[iid].name if iid in data.ingredients else '?'} [{iid}]"

        said = "; ".join(
            f"{name(x.use)}{'' if x.qty is None else f' {x.qty:g} per serving'} instead of {name(x.out)}"
            for x in mine
        )
        return f" (using {said})"

    out, plan = [], None
    for week, title in ((0, "This week"), (1, "Next week")):
        lines = []
        for i in range(7):
            day = monday + dt.timedelta(days=7 * week + i)
            if plan is None or not plan.start <= day < plan.end:
                plan = plan_for(data, day)
            d = plan.day(day) if plan else None
            head = f"- {t.date(day)} ({day.isoformat()})"
            if day in paused:
                lines.append(f"{head}: paused, the household eats off the plan")
            elif d:
                side = f" + {dish(d.side)}{swapped(d, 'side')}" if d.side else ""
                lines.append(
                    f"{head}: breakfast {dish(d.breakfast)}{swapped(d, 'breakfast')}; "
                    f"lunch {dish(d.lunch)}{swapped(d, 'lunch')}{side}; "
                    f"snack {dish(d.snack)}{swapped(d, 'snack')}; "
                    f"dinner {dish(d.dinner)}{swapped(d, 'dinner')}"
                )
        if lines:
            out += [f"\n## {title}", *lines]
    out.append("\n## Recipes (id: name; meals; kind)")
    for r in sorted(data.recipes.values(), key=lambda r: r.id):
        side = ", side salad" if r.has("side") else ""
        out.append(f"- {r.id}: {r.name}; {', '.join(r.meals)}; {r.kind}{side}")
    return out


def list_now(data: Data, db: DB, today: dt.date) -> list[str]:
    """This week's shopping list as the phones share it: each line ticked (bought) or still to buy,
    by store and section, and what is at home and off the list. Someone back from the shop can ask
    what was missing, and the menu can be fitted to it, without reading the list out."""
    monday = today - dt.timedelta(days=today.weekday())
    plan = plan_for(data, monday)
    if not plan:
        return []
    shop = shopping_list(data, db, plan, monday, today=today)
    ticked = db.ticked(monday)
    head = "\n## Shopping list this week (bought = ticked on the list; after a shop, what is still "
    out = [head + "to buy was not found or not bought yet)"]
    for s in shop.stores:
        out.append(f"- {s.name}:")
        for sec, lines in s.sections.items():
            for line in lines:
                state = "bought" if line.ingredient.id in ticked else "to buy"
                out.append(
                    f"  - {sec}: {line.ingredient.name} [{line.ingredient.id}], {line.amount}: {state}"
                )
    if shop.have:
        out.append("- at home, off the list: " + ", ".join(i.name for i in shop.have))
    from . import pantry

    late = [
        f"{ing.name} [{ing.id}] for {rec.name} on {d.isoformat()}"
        for d in (today, today + dt.timedelta(days=1))
        if (p := plan_for(data, d))
        for ing, rec in pantry.unbought(data, db, p, d)
    ]
    if late:
        out.append(
            "- needed today or tomorrow and not bought on the shopping day (missing at home, "
            "say so and get it bought): " + "; ".join(late)
        )
    out += unused_now(data, db, monday)
    return out


def unused_now(data: Data, db: DB, monday: dt.date) -> list[str]:
    """Food ticked as bought on last week's or this week's list that no dish uses, from last Monday
    to the end of next week: a dish was moved or dropped after the shopping. It must not go to
    waste, so the AI is told to put it back in the menu or say how to keep it."""
    left = doctor.unused_food(data, db, monday)
    if not left:
        return []
    head = (
        "\n## Bought and on no dish (ticked since last Monday; a dish was moved or dropped after "
    )
    rule = (
        "- Never let it go to waste: put each in the menu before it spoils, with a menu change, or "
    )
    return [
        head + "the shopping)",
        "- " + ", ".join(left),
        rule + "say how to keep it (freeze it); ask only if it may already be eaten.",
    ]


def pantry_now(data: Data, db: DB, today: dt.date) -> list[str]:
    """What is at home now (pantry.py), across weeks: each ingredient with its amount, since when
    and until when it is good, and what «I have it» keeps off the lists without counting it."""
    from . import pantry

    items = pantry.now(data, db, today)
    have = sorted(data.ingredients[i].name for i in pantry.at_home(data, db, today))
    out = [
        (
            "\n## Pantry (at home now: what the list's ticks bought and what the chat said, less what "
            "the plan cooked since and what went off)"
        )
    ]
    for x in items:
        good = f", good until {x.expires - dt.timedelta(days=1)}" if x.expires else ""
        cold = f"; {x.frozen_amount} of it in the freezer" if x.frozen_amount else ""
        out.append(f"- {x.name} [{x.ingredient}]: {x.amount}, since {x.since}{good}{cold}")
    if have:
        out.append("- at home in an amount nobody counted («I have it»): " + ", ".join(have))
    out.append(
        "- Nothing else is at home as far as the app knows; when someone says otherwise, set it "
        "with `pantry`."
        if items or have
        else "- Empty: nothing ticked or said to be at home."
    )
    return out


def problems_now(data: Data, db: DB, today: dt.date, viewer: str | None = None) -> list[str]:
    """What the consistency check (doctor.py) and the last check of the plan as it stands
    (verifier.check_plan) found, with what to change. Food bought and on no dish has its own
    section above, so it is not repeated here. Stale reminders stay out: they may be another
    chat's (a private one), and the app cancels them itself, saying so in their own chat."""
    from .verifier import plan_findings

    # a recipe one person cannot have names the food: in a private chat it may be someone else's
    skip = ("unused", "stale_reminder") + (("avoided",) if viewer is not None else ())
    found = [p.text for p in doctor.problems(data, db, today) if p.kind not in skip]
    found += plan_findings(data, db, today, viewer)
    if not found:
        return []
    return [
        "\n## Problems the app found: fix them",
        (
            "- Fix each with a change or reminders block in your answer when the person you answer "
            "can decide it, or say what you would change; never leave one unsaid."
        ),
        *(f"- {p}" for p in found),
    ]


def limits() -> list[str]:
    """The limits the app's validation enforces on what a change may set, read from the code that
    enforces them, so a proposal never carries a value the app refuses."""
    from .changes import INSTRUCTIONS_MAX, NOTE_MAX, NOTES_MAX
    from .llm import REMINDER_CHARS, REMINDERS_MAX
    from .menu import PAUSE_MAX, SHIFT_MAX
    from .model import Batch, Recipe, Reminder

    def bounds(model, name: str) -> str:
        lo = hi = None
        for m in model.model_fields[name].metadata:
            lo = getattr(m, "ge", lo)
            hi = getattr(m, "le", hi)
        return f"{lo} to {hi}"

    kinds = Reminder.model_fields["kind"].annotation.__args__
    return [
        "- limits the app enforces (a change outside them is refused):",
        f"  - household.batch.sessions_per_week: {bounds(Batch, 'sessions_per_week')}",
        "  - household.batch.avoid_days: at least one weekday left to cook on",
        "  - household.batch.start.<weekday>: HH:MM, before household.lunch_time",
        (
            f"  - settings.reminders: kind one of {', '.join(kinds)}; at: HH:MM (00:00 to 23:59); "
            "days: daily or a non-empty list of weekdays; kind message needs text, which may use only "
            "{names} and {today}"
        ),
        (
            f"  - a new recipe: fridge_days {bounds(Recipe, 'fridge_days')}; every ingredient qty "
            "above 0"
        ),
        (
            f"  - menu: shift 1 to {SHIFT_MAX} weeks; a pause at most {PAUSE_MAX} days; regenerate "
            "from a Monday; a lunch's cook is not past and not after the day it is eaten"
        ),
        (
            f"  - chat notes: at most {NOTES_MAX} per person, {NOTE_MAX} characters each; "
            f"settings.llm.instructions at most {INSTRUCTIONS_MAX} characters"
        ),
        (
            f"  - reminders block: at most {REMINDERS_MAX} per answer, {REMINDER_CHARS} characters "
            "each"
        ),
    ]


def settings_now(data: Data, viewer: str | None = None) -> str:
    """The values a change may touch, with the ids a change block names. viewer: in a private chat,
    only their own avoided foods are listed (they can be medical, and only the asker may add to
    their own list anyway)."""
    hh, st = data.household, data.settings
    stores = ", ".join(f"{sid} ({s.name})" for sid, s in st.stores.items()) or "none"
    out = [
        "\n## Settings now",
        f"- household.lunch_time: {hh.lunch_time}; household.shopping_day: {hh.shopping_day}",
        f"- household.guests: {hh.guests or '{}'}",
        f"- household.batch: days {hh.batch.preferred_days}, avoid_days {hh.batch.avoid_days}, "
        + f"{hh.batch.sessions_per_week} sessions a week, "
        + f"start {hh.batch.start or '{}'} (a session on the day of its lunch; unset: 10:00)",
        f"- household.rules: {hh.rules.model_dump()}",
        f"- stores: {stores}; primary {st.shopping.primary}; alternatives {st.shopping.alternatives}",
        f"- settings.sections: {st.sections or '{}'}",
    ]
    for p in hh.people:
        if viewer is None or p.id == viewer:
            out.append(f"- household.people.{p.id}.avoid_ingredients: {p.avoid_ingredients}")
            out.append(f"- household.people.{p.id}.swaps: {p.swaps}")
            out.append(
                f"- household.people.{p.id}.training_days: {p.training_days} "
                f"(+{p.training_extra_kcal:.0f} kcal those days)"
            )
    out.append("- reminders:")
    for r in st.reminders:
        out.append(
            f"  - {r.id}: kind {r.kind}, at {r.at}, days {r.days}"
            + (f", meal {r.meal}" if r.kind == "meal" else "")
            + (f", people {r.people}" if r.people else "")
            + (f", text {r.text!r}" if r.text else "")
            + ("" if r.enabled else ", disabled")
        )
    out += limits()
    return "\n".join(out)
