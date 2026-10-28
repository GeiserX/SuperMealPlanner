# The app API

The phone apps talk to `/api/v1` on the same server that serves the web pages. Pages and API are one
program reading one plan, so a shopping tick made on a phone is the tick the laptop shows, and a 👍
from the app is a vote the next draw reads.

The web pages are meant for a home network. This API is the part a phone carries out of the house, so
every route except two needs a token that belongs to a phone someone in the household paired.

## Pairing a phone

There is no password. The bot hands out a code and the app swaps it for a token.

1. In a **private** chat with the bot: `/pair` (`/vincular` in Spanish). The bot answers with six
   digits, good for ten minutes and for one phone. It refuses in the group, where everyone present
   would read it.
2. The app sends them:

   ```http
   POST /api/v1/pair
   {"code": "418 203", "device": "Alex's iPhone"}
   ```

   ```json
   {"token": "…", "device_id": 3, "device": "Alex's iPhone",
    "person": "alex", "person_name": "Alex", "household": "The Example Household"}
   ```

3. The app keeps `token` in the phone's keychain and sends it from then on:

   ```http
   Authorization: Bearer <token>
   ```

The token is bound to whoever asked for the code, so the app knows whose plate to open first. It does
not expire; it ends when someone takes the phone away with `/devices` (`/dispositivos`) in the bot or
`DELETE /api/v1/devices/{id}` from the app itself.

### What protects it

- Spaces and dashes are ignored, so `418 203` and `418-203` both work.
- A code is spent the moment it is read, inside one transaction: a second phone sending the same
  digits gets nothing, even in the same second.
- Wrong codes run out. After a handful the whole endpoint answers `429` with `Retry-After`, counted
  per caller and in total, so six digits cannot be walked.
- The database keeps only sha256 of the code and of the token. A copy of `supermealplanner.db`
  cannot pair a phone or impersonate one.
- Revoking is immediate: every request looks the device up, and a revoked row is not found.

## The routes

| Route | What it answers |
|---|---|
| `GET /ping` | **No token.** `{"app": "supermealplanner", "version", "api"}` — what an app checks before it asks for anything, and how it knows a server is one of ours |
| `POST /pair` | **No token.** A code for a token, as above |
| `GET /meta` | The household name, the language, who lives here, the stores, and which device this is |
| `GET /day?d=&person=` | One day in eating order: every meal, each dish, and a plate per person with its lines, kcal and protein. Plus the day's targets, what to take out of the freezer for tomorrow, what to leave to soak tonight for tomorrow's batch session (`soak_for_tomorrow`: each dish with the ingredient, `qty`, `unit` and the `amount` as the page writes it), and this person's votes. Each meal carries its `hours` (`{"from": "12:00", "until": "16:00"}`, local time) and `open`: whether it is the meal of the moment, by the household's clock at `now` (see below) |
| `GET /week?d=` | Seven days of dish names from that week's Monday |
| `GET /tasks?d=` | Each day's kitchen jobs with their times: defrost, batch (with the soaking on the eve of a session), shopping, and one-off reminders set from the chat |
| `GET /cook?d=` | A batch afternoon: every dish, its servings, its containers and where each one goes |
| `GET /recipe/{id}?d=&person=` | The whole recipe, with this person's plate for that day and their vote |
| `GET /list?d=&store=` | The week's shopping list per store, in the order the aisles come, with the ticks and what is already at home. Each section's lines come as the page shows them (see below) |
| `GET /list/ticks?d=` | Only the week's ticks, as below: small enough to ask every few seconds while the list is open, so a tick made on another phone or on the web page shows at once |
| `POST /list/tick` | `{"ingredient", "on", "d"}` — tick one off this week's list, or put it back. Answers the week's ticks, as below |
| `POST /list/have` | `{"ingredient", "on"}` — «I have it at home»: off every list until it goes back |
| `GET /pantry` | What is at home (see below): `{"day", "items": [{"ingredient", "name", "qty", "unit", "amount", "since", "expires"}], "have": [{"ingredient", "name"}]}` |
| `POST /rate` | `{"recipe", "vote": 1 \| -1 \| 0, "person"?}` |
| `POST /log` | `{"kg"?, "waist_cm"?, "hunger"?, "craving"?, "nausea"?, "water"?, "binge"?, "stool"?, "pills_lunch"?, "note"?, "person"?, "day"?}` — the Log page as JSON. Numbers out of range are clamped, as the page does; an impossible weight is a `400` |
| `GET /progress` | Each person's last readings, the smoothed trend, the current target and the flags, already in the household's language |
| `POST /chef` | `{"text"}`: say something to the chef, as if typed in your private chat with the bot. Answers `202` with `{"id", "state"}` at once (see below) |
| `GET /chef/{id}?wait=` | The answer to it: holds up to `wait` seconds (25 at most) until it exists. `{"id", "state": "thinking"}`, then `{"id", "state": "done" \| "failed", "text"}` |
| `POST /transcribe` | The body is audio (`Content-Type` `audio/wav`, `audio/ogg`, `audio/mpeg`, `audio/mp4`, `audio/webm`, `audio/flac`…): turned into text by the transcriber the bot uses for voice notes. `{"text"}` (see below) |
| `GET /devices` | Every paired phone, with `this` marking the caller |
| `DELETE /devices/{id}` | Unpair one |

### The ticks on the shopping list

`GET /list`, `GET /list/ticks` and `POST /list/tick` all carry the week's ticks the same way:

- `ticked`: the ticked ingredient ids, sorted by id.
- `ticked_order`: the same ids, the first ticked first.
- `ticked_at`: `{ingredient: "2026-01-05T18:30:00+00:00"}`, when each was ticked, in UTC. A tick made
  before the server kept these times has none.

In `GET /list` each section's `lines` come in the order the web page shows them: what is still to buy
first, in the section's usual order, then the ticked ones, the first ticked first. Each line carries:

- `ticked`: whether it is ticked this week.
- `ticked_at`: when it was ticked, or `null`.
- `position`: its place in the section's usual order, from 0. An unticked line goes back among the
  ones still to buy by this number; a newly ticked one goes to the end of its section.

So an app can reorder a section by itself the moment a tick changes, here or from `GET /list/ticks`,
without asking for the whole list again.

### The pantry

A tick through `POST /list/tick` feeds the pantry exactly as a tick on the web page does: it brings in what the line buys, and unticking takes it back out. `GET /pantry` lists what is at home now, by name: `qty` in the ingredient's `unit`, `amount` as the page writes it, `since` the day it came in, and `expires` the first day it is no longer good (`null` for food that keeps until it is used up). `have` is what «I have it» keeps off the lists, in an amount nobody counted. `GET /list` also says which of the week's ingredients the pantry already holds enough of: `stocked`, a list of ids that are not among the lines.

### The meal of the moment

On the today page every meal is a fold that opens and closes from its title and never disappears.
When the page opens, only the meal whose hours hold the clock is unfolded: breakfast from 00:00 to
12:00, lunch and salad from 12:00 to 16:00, snack from 16:00 to 19:00 and dinner from 19:00 to
24:00. A fold opened or closed by hand stays as it was left while the page is on screen.

`GET /day` sends what an app needs to do the same: each meal's `hours` (`from` included, `until`
not, as `HH:MM` in the household's timezone), `open` for the meals of the moment, and `now`, the
`HH:MM` they were judged at. The snacks to nibble on, the batch session, the defrost and soak notes
and the votes are not folds.

`person` defaults to the person the device is paired to, and may name anyone in the household — one
phone on the kitchen counter can log for everybody. It may not name anyone else: that is a `400`.

`d` is a date as `YYYY-MM-DD` and defaults to today in the household's timezone. Anything else is a
`400` saying so, never a stack trace.

## Talking to the chef by voice

Any client that can turn speech into text (an iOS Shortcut with "Dictate text", a watch app, a
car) can talk to the household's bot and read its answer out. It is the same bot and the same
conversation as the Telegram chat, not a second assistant:

- What is said goes into the household group, as if the person had written it there to the bot:
  the bot posts "🎙️ <name> (by voice): <what they said>" and answers it there, so everyone
  reads the conversation once, in one place. With no group (`chat_id`) or with
  `bot.voice_to_group: false`, it goes into the private chat between the bot and the person the
  device is paired to instead, shown as "🎙️ …".
- It runs through the path a typed message to the bot takes: messages sent together are answered
  together, a change the person asked for is made at once, and a change that waits for their yes
  is settled by a spoken "yes" or "no" exactly as a typed one is.
- The AI is told the message was spoken and answers in a few plain sentences. The `text` the API
  returns is made to be read out: no Markdown, links, code or emojis; choices come as "You can say:
  A, B or C"; a change that waits ends "Yes or no?", so a voice client that listens again after a question keeps the mic open; reminder offers are one sentence pointing to
  Telegram, where their buttons are.

A turn is two calls, so no request has to stay open while the AI thinks:

```http
POST /api/v1/chef
Authorization: Bearer <token>
{"text": "what's for dinner tonight?"}
```

```json
{"id": "q3Yx0b1VZt2mJw9C", "state": "thinking"}
```

```http
GET /api/v1/chef/q3Yx0b1VZt2mJw9C?wait=25
```

```json
{"id": "q3Yx0b1VZt2mJw9C", "state": "done", "text": "Tonight it is baked hake with a salad."}
```

While the answer is not there, `GET` returns `{"state": "thinking"}` after `wait` seconds (25 at
most, so a client's request never times out): ask again until the state is `done` or `failed`.
`failed` still carries a `text` to read out ("I could not answer that…"). Only the person who said it
can read a turn; anyone else, an unknown id or a turn older than an hour gets `404`. `409` means the
device's person has no Telegram user in `bot.people` (or private chats are off), and `503` that the
bot is not running on this server.

```sh
id=$(curl -s -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"text": "what is for dinner?"}' https://comidas-api.example.com/api/v1/chef | jq -r .id)
curl -s -H "Authorization: Bearer $TOKEN" "https://comidas-api.example.com/api/v1/chef/$id?wait=25"
```

### Speech to text

A voice client that has no speech recognition of its own can send what it recorded to
`POST /transcribe` and then send the text to `POST /chef`. The server uses the same transcriber it
uses for voice notes in the chat (`transcription` in the settings), so the client needs no speech
service or key of its own.

```sh
curl -s -H "Authorization: Bearer $TOKEN" -H 'content-type: audio/wav' \
  --data-binary @question.wav https://comidas-api.example.com/api/v1/transcribe
```

```json
{"text": "what is for dinner?"}
```

`415` is a type it does not take, `413` audio over 10 MB, `400` an empty body, `503` a server with no
transcription configured, and `502` a transcriber that failed.

## Rules the apps rely on

An app in someone's pocket cannot be updated in step with the server, so:

- Fields are **added**, never renamed or removed, inside `api` version 1.
- An unknown query parameter is ignored rather than refused.
- `GET /ping` carries `api`, so an app that needs something newer can say so instead of failing.

## Reaching it from outside

Nothing here listens on its own: it is the same Uvicorn the pages use. Put the API on a public name
and keep the pages private, so what leaves the house is the token-protected part only:

```caddyfile
comidas-api.example.com {
	@api path /api/v1/*
	handle @api {
		reverse_proxy supermealplanner:8080
	}
	handle {
		respond "not here" 404
	}
}
```

The household's own copy keeps the pages on their home-network name as before.
