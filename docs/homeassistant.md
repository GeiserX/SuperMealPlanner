# Talking to the chef with Home Assistant's Assist

`homeassistant/custom_components/supermealplanner` is a Home Assistant integration that turns the
chef into an Assist conversation agent. With it, "Hey Siri, talk to the chef" (or a Voice PE in the
kitchen) opens a spoken conversation with the household's bot: you ask, it answers aloud, and the
microphone opens again for the next question or for a "yes" to a change it proposes. Each exchange
lands in the household group, said by you and answered there, exactly as described in
[api.md](api.md#talking-to-the-chef-by-voice).

## What it adds

Each config entry is one paired device, so one person. It adds:

- a **conversation agent** that says what Assist heard to the chef as that person (`POST /chef`), waits
  for the answer (up to two minutes; Assist's pipeline allows five), and reads it out;
- a **speech-to-text** entity that sends the recording to the server's own transcriber
  (`POST /transcribe`), the one the bot uses for voice notes, so Home Assistant needs no speech service
  of its own.

Every answer ends in a question, because Assist keeps listening after a reply that ends in "?": the
chef's own ("Yes or no?") or an added "Anything else?". A short "no", "nothing" or "that's all" after
that added question ends the conversation without asking the chef; a "no" after the chef's own
question goes to the chef, which is waiting for it.

## Set it up

1. Copy `homeassistant/custom_components/supermealplanner` into Home Assistant's
   `config/custom_components/` and restart Home Assistant.
2. Each person sends `/pair` to the bot in their private chat with it.
3. Settings → Devices & services → Add integration → "SuperMealPlanner chef": the server's address as
   Home Assistant reaches it, and the six digits. The entry is named after that person.
4. Settings → Voice assistants → Add assistant, one per person ("Chef Alex"): conversation agent and
   speech to text from that person's entry, a text-to-speech voice in the household's language
   (Piper runs locally), and "Prefer handling commands locally" off so a spoken "yes" reaches the chef.

## On an iPhone

The Home Assistant app opens Assist on a given pipeline, already listening, from the link
`homeassistant://assist?pipelineId=<pipeline id>&startListening=true` (the id is in the pipeline's
URL under Settings → Voice assistants). A shortcut that opens that link is all Siri needs:

1. Shortcuts → new shortcut → "URL" with that link, then "Open URLs".
2. Name it one short word Siri does not take for something else. "Chef" works; "Talk to the chef"
   does not, because Siri reads "talk to …" as a call to a contact.

Say only its name ("Hey Siri, chef"), wait for the microphone, then ask: Siri passes nothing said
after a shortcut's name into the shortcut. From a locked phone, Siri asks to unlock first, because
the shortcut opens an app.
