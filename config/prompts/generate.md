You write one realistic phone-call transcript at a time for a synthetic corpus of resident calls to the City of Newport, Rhode Island, in the winter of 2025-2026. Every person is fictional. The corpus will be read by people and by AI systems looking for patterns, so each call must read like a real, unscripted call, and nothing in it may give away that it was planned.

You receive a call spec as JSON. Write the transcript it describes.

## What the spec means

- `caller`, `clerk`: who is talking and how they talk. Hold the voice for the whole call.
- `start`, `duration_s`, `target_words`: when the call happens and roughly how long it runs. Land within about 15% of `target_words`.
- `topic`, `tone`: what the caller wants and their mood. The spec tone must be the caller's clearest, most sustained mood from their first turn to their last; a reader asked to name the caller's mood in one word should pick exactly this one. Politeness does not cancel it: a frustrated caller can be courteous and still plainly aggrieved. Show the tone through what they say and how, never by naming it. Tone meanings:
- `frustrated`: aggrieved, the problem keeps happening or the city has let them down; impatience, "again", "nobody came", pushing for action.
- `annoyed`: irritated by a specific hassle (a bill, a missed pickup, a ticket); short or sarcastic, but not aggrieved about the city failing them.
- `confused`: does not understand the rules, the bill, or the process; asks for things to be explained or repeated.
- `worried`: anxious about safety, damage, money, or what happens next.
- `neutral`: businesslike; wants information or to report something and is done.
- `friendly`: warm, chatty, upbeat. Ordinary politeness is not friendliness.

Readers tend to hear warmth or worry where none was meant, so write the quieter moods firmly:
- A `neutral` caller is matter-of-fact from start to finish: states the problem, answers questions, gets what they need, says thanks and goes. No jokes, no chit-chat, no personal stories, no expressions of worry or fear, no complaints. The clerk's warmth does not change the caller's mood.
- A `confused` caller keeps not understanding: they misread the form or the rules, ask the clerk to explain again, get a detail wrong and are corrected, and are still a little unsure at the end. Gratitude when it finally clicks is fine; banter is not.
- An `annoyed` caller is irritated about this specific hassle and lets it show in short, clipped or sarcastic lines; they are not lost or confused about it. An annoyed caller about a bill already understands the bill: they think a charge, fee, or reading is wrong or unfair and say so, and they push to have it fixed. They do not ask the clerk to explain how billing works.
- A `frustrated` caller about flooding is aggrieved at the city more than afraid: the point is that it keeps happening and nobody fixed it.
- `address`: if present, the caller states it in the call (number and street, said naturally, sometimes with a cross street or "down by" a landmark). If null, the caller never gives an address.
- `storm`: if present, the call follows that storm by `days_after_end` days. `call_weekday`, `storm.start_weekday`, `storm.end_weekday` and `storm.when_phrase` give the real timing; any day the caller names for the storm must agree with them. Refer to it the way a resident would, never by the spec's internal name.
- `household`: present for some addresses. It is the fixed truth about the people and the history at that address: who lives there, their ages, jobs and voices, the basement, each flood, and a dated timeline of earlier calls and city actions. The caller is one of these people and speaks as described. They may refer only to timeline events dated before this call, and must get those right (who called, when, what was promised, what the crew did). Never contradict the fact sheet; never add relatives, floods, visits, or promises it does not contain. Its specifics are exact: water depths, whether the basement is finished, how the water got in, jobs, ages, spouses and who lives there. Repeat them as given; do not round, upgrade, or improvise them. If the color beat conflicts with the household sheet (a job, how long they have lived there, where they are calling from), the sheet wins: adapt the color beat to fit or drop it.
- `caller.phone`: if a callback number is spoken, it is exactly this one. Most calls need no number at all: only take one when a callback or work order truly needs it, and ask for it in this clerk's own words.
- The color beat (the last of the `beats`) is the only personal background detail in the call. Do not invent other biography, hobbies, jobs, or life events beyond the spec and the household sheet.
- `beats`: story points the call must contain, in any natural order. Each must be unambiguous to a careful reader.
- `must_not`: words and moves that must not appear anywhere in the transcript, from either speaker. Treat these as hard constraints.
- `planted` and `decoys`: internal flags that explain why the beats exist. Never mention them. Specifically:
  - `planted.lawyer` true: the caller says the word "lawyer" once or twice, naturally (for example that they are calling a lawyer or have talked to one). Otherwise nobody says "lawyer" or "lawyers".
  - `planted.card_full`: the caller reads the full card number aloud, digit groups as spoken words or digits, exactly as given, to pay `amount_usd`. The whole number is said in one caller turn (the clerk may ask for it again or confirm it afterwards). The clerk takes it. Expiration and security code may follow. Otherwise no complete card number appears.
  - `planted.after_hours_pay` true: the caller asks, in their own words, whether or how they can pay a bill online or when City Hall is closed (at night, weekends, drop box, a phone line after hours). Phrase it freshly; do not reuse stock wording like "Can I pay my bill after you close?"
  - `planted.fence_permit` true: the caller asks whether they need a permit to put up a fence, in their own words and context.
  - `planted.downed_tree_where` true: the caller asks where or how to report a downed tree.
  - `planted.repeat_flood_second` true: the caller says clearly that their basement has flooded for the second time this winter.
  - `planted.cluster` true: the call is about a storm drain backing up or flooding at the stated address.

## How the call should read

- The clerk speaks first with a department greeting. Vary greetings across clerks.
- Real phone talk: interruptions, restarts, filler ("um", "so", "yeah, no"), clerks putting people on hold, spelling names, reading things back, small digressions, misunderstandings that get corrected. Not every call ends neatly.
- Speech on the page follows the persona: Rhode Island speech where specified ("wicked", "down the Point", "up Bellevue", dropped words), non-native English, elderly, rushed, chatty, terse. Keep it respectful and readable; no phonetic caricature.
- Channel noise for `cell-poor` and `speakerphone`: occasional `[inaudible]`, `[crosstalk]`, `[line breaks up]`, repeated questions. `[pause]` is allowed in any call. No other bracketed markers.
- Clerks are competent and human, and they mostly just answer. They can give ordinary, general answers (a fence usually needs a permit, there is a payment drop box, crews are busy after a storm) but never cite specific fees, ordinance numbers, dollar thresholds, or staff names beyond the spec. When a clerk truly does not know, they transfer, take a message, or check with someone, and they say so plainly in their own way. Do not have clerks repeatedly announce that they don't want to guess or overpromise; that is a tic, not a voice.
- Fit the clock: greetings, sign-offs, and remarks about the day match the call's `start` time and weekday.
- Use real Newport street names only as given or as natural landmarks. No real personal data. Card numbers only as given in the spec.
- Every call is its own conversation. Avoid stock openings, stock closings, and recurring phrases. Never use "Ayuh" (it is Maine, not Rhode Island). Do not reach for these overused fillers unless the spec says so: a boatyard, a food pantry, a bakery or diner that closed, a kid home from college, a musician, recent surgery, a late spouse, "first time I've ever called", "do the crews work weekends". Clerks never say "in case the crew has questions" or "I can't give you an exact time"; when timing is uncertain, each clerk says so differently, or just gives a window.

## Output

Return only JSON matching the schema: `{"turns": [{"speaker": "clerk" | "caller", "text": "..."}]}`. One turn per speaker change. No narration, stage directions, timestamps, or speaker names inside `text`.
