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
   {"code": "418 203", "device": "iPhone de Sergio"}
   ```

   ```json
   {"token": "…", "device_id": 3, "device": "iPhone de Sergio",
    "person": "sergio", "person_name": "Sergio", "household": "Casa"}
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
| `GET /day?d=&person=` | One day in eating order: every meal, each dish, and a plate per person with its lines, kcal and protein. Plus the day's targets, what to take out of the freezer for tomorrow, and this person's votes |
| `GET /week?d=` | Seven days of dish names from that week's Monday |
| `GET /tasks?d=` | Each day's kitchen jobs with their times: defrost, batch, shopping, and one-off reminders set from the chat |
| `GET /cook?d=` | A batch afternoon: every dish, its servings, its containers and where each one goes |
| `GET /recipe/{id}?d=&person=` | The whole recipe, with this person's plate for that day and their vote |
| `GET /list?d=&store=` | The week's shopping list per store, in the order the aisles come, with the ticks and what is already at home. Each section's lines come as the page shows them (see below) |
| `GET /list/ticks?d=` | Only the week's ticks, as below: small enough to ask every few seconds while the list is open, so a tick made on another phone or on the web page shows at once |
| `POST /list/tick` | `{"ingredient", "on", "d"}` — tick one off this week's list, or put it back. Answers the week's ticks, as below |
| `POST /list/have` | `{"ingredient", "on"}` — «I have it at home»: off every list until it goes back |
| `POST /rate` | `{"recipe", "vote": 1 \| -1 \| 0, "person"?}` |
| `POST /log` | `{"kg"?, "waist_cm"?, "hunger"?, "craving"?, "nausea"?, "water"?, "binge"?, "stool"?, "pills_lunch"?, "note"?, "person"?, "day"?}` — the Log page as JSON. Numbers out of range are clamped, as the page does; an impossible weight is a `400` |
| `GET /progress` | Each person's last readings, the smoothed trend, the current target and the flags, already in the household's language |
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

`person` defaults to the person the device is paired to, and may name anyone in the household — one
phone on the kitchen counter can log for everybody. It may not name anyone else: that is a `400`.

`d` is a date as `YYYY-MM-DD` and defaults to today in the household's timezone. Anything else is a
`400` saying so, never a stack trace.

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
