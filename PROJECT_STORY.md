## Inspiration

At a solo-doctor ENT clinic in Tirupati, the phone asks the same four questions all day.

*Where exactly are you?* *What time do you open?* *Are you open on Sunday?* *Do you do hearing tests?*

Over and over, dozens of times a day, every day. The answers never change. They are written on a board on the wall and printed in a leaflet on the desk, and none of that helps, because the person asking is on a phone in an auto-rickshaw. So someone has to say the address out loud again. And again. And they have to say it patiently the fortieth time, because the fortieth caller has never asked before.

That work is genuinely necessary and completely unrewarding. It is also what makes the *rest* of the phone unmanageable. The doctor is mid-consultation when it rings, so it goes unanswered — and there is no way to tell from the ringing whether that was the fortieth "what are your timings" or a patient trying to move tomorrow's appointment. Both get missed together.

Hiring someone to absorb it costs more than a small clinic makes back. And a receptionist still goes home at six, still takes lunch, and still cannot answer two lines at once — while callers keep ringing at nine at night, because that is when they finally have a moment to think about their ear.

So the real target was never "answer the phone." It was: **take the repetition away entirely, and make the repetitive answers exactly as reliable as the wall.**

That last part is the whole difficulty. A wall does not improvise. The obvious answer — "put an AI on the phone" — is suspicious precisely because its failure mode is worse than the problem it solves. A tired human answering the address for the fortieth time still gets the address right. A language model that is unsure *invents a plausible answer*, in a confident voice, on a recorded call, to a patient making a medical decision.

Told the wrong closing time, someone arrives to a locked door. Told a slot exists when it does not, they take an afternoon off work for nothing. Told a price that was never set, they expect that price. The repetitive questions are the *easiest* ones to answer and therefore the most damaging to answer wrongly, because nobody thinks to double-check the opening hours.

So the goal was an agent that is as helpful as a good receptionist and no more presumptuous than one: endlessly patient about location, timings and services, never bored on the fortieth call, and quick to say *"the doctor will confirm that for you"* the moment a question becomes a matter of judgement.

Which made the project less about "can it talk" and more about one constraint:

> **The agent decides what to say. It never decides what is true.**

Every fact a caller hears must come from a tool that read the database. The model understands speech and picks a tool. That is all it does. It cannot invent availability, cannot confirm a save that did not happen, and cannot answer a question about the clinic from its training data.

Everything interesting we built came out of defending that line.

## What it does

**Live demo — call it yourself: https://d21u7cmj563imv.cloudfront.net/voice**

Press *Start call* and speak. Your microphone streams as 16 kHz mono PCM to **Amazon Nova Sonic** over a bidirectional **Amazon Bedrock** stream, and the reply returns at 24 kHz. It is genuine speech-to-speech with no transcribe-then-synthesise hop, so it feels like a conversation rather than a voice assistant. Interrupt it mid-sentence and playback stops in under 500 ms.

A real call from our testing:

```
agent    Just so you know, this call is recorded for quality and record-keeping.
         How can I help you with your appointment or clinic information today?
caller   I'd like to book a hearing test
agent    I can help with that. Let me check the calendar.
         I have Monday the fourteenth at nine, nine-thirty, or ten. Which suits you?
caller   can you do sunday the thirteenth
agent    The thirteenth falls on a Sunday, which is our clinic holiday — we're closed
         that day. The nearest I have is Monday the fourteenth at nine. Shall I book it?
caller   yes, my name is Sailaja Devi
agent    Thank you. That's Sailaja Devi — S-A-I-L-A-J-A?
caller   yes
agent    And a callback mobile number?
caller   nine nine zero zero zero one two three zero seven
agent    Booked: Hearing Test, Monday the fourteenth of September at nine.
         Your patient ID is S-I-3-0-7. Please quote that next time and I'll find
         you straight away.
```

### On the phone

**Booking.** The caller names a service, matched against the clinic's offered services by exact name — no fuzzy guessing. The agent reads the calendar and offers up to **three concrete dated slots**, because three is what a person can hold in their head on a phone call. It looks the caller up or creates a record, writes the appointment, and reads back the service, date, time and patient code.

The appointment records **the service the caller asked for**, not the label the slot was published under. A slot is the doctor's half hour, published without knowing who will take it. An appointment is one named person coming in for one named thing.

**Rescheduling and cancelling.** It finds the caller's existing appointments first — that is the only source of an appointment id, and it never asks a caller for a booking reference, because nobody has one to hand. A reschedule **releases the old slot** so someone else can take it. Cancellation requires explicit confirmation, enforced by the orchestrator rather than left to the prompt.

**Calendar awareness that holds up.** Availability is a bounded index query. It never offers a slot that has already started, never offers the same minute twice, never offers a slot the doctor did not publish, and treats a requested hour as a **floor rather than a filter**, so a full afternoon rolls into the next day instead of returning nothing. One hard boundary: the agent can book and release slots, but **can never create one**. Otherwise a caller pressing for an earlier time would eventually be offered availability the doctor never opened.

**Closed days explained, not silently skipped.** Ask for Sunday the 13th and it names the reason — *"that's a Sunday, our clinic holiday"* — then offers the nearest working day. Driven by the configured opening hours, so a clinic that opens Sunday and closes Tuesday needs no code change.

**A patient code a human can actually use.** `SI307` — first letter of the first name, last letter of the last name, last three digits of the mobile. Read back at the end of every booking.

**Dictated numbers, however they arrive.** `9900012307` and *"nine nine zero zero zero one two three zero seven"* resolve to the same patient. So do `"double nine..."`, a `+91` prefix, and half-converted transcripts.

**Names confirmed aloud, health details never.** It spells unusual names back the way a receptionist does. It deliberately never reads blood groups or measurements aloud: a name is worth confirming out loud, a health detail announced to whoever else is in the room is not.

**Clinic questions answered from the doctor's own documents.** Hours, address, directions, the service list — from PDFs the doctor uploads through the portal, indexed with Bedrock embeddings. Updating what the agent knows is uploading a document, not redeploying.

**Waitlist.** When nothing suitable is free it records who wanted what and when, which the second agent later turns into a gap-fill suggestion.

**Every call recorded and transcribed.** A timestamped transcript, plus one stereo WAV per call at 24 kHz with **the patient on the left channel and the agent on the right**, laid out on the real timeline. You can hear who interrupted whom, and a barge-in sounds like an interruption instead of garbled mono.

### Helpful up to the line, and honest about where the line is

The agent is genuinely useful about the clinic itself. It will explain **what a service involves** in plain language, drawn from the doctor's own uploaded documents — what a hearing test is, roughly how long an appointment runs, what to bring, how to find the place, when to arrive. It lists every service the clinic offers, so a caller who does not know the vocabulary can still get somewhere.

**Symptom routing the doctor wrote herself.** A caller who says *"I've got severe itching inside my nose"* does not know that what they need is called an ENT Consultation. They should not have to. So the agent routes described problems to services — and the routing is **authored by the doctor, stored in the database, and only relayed by the agent.**

The doctor writes rules in her own words: match phrases like *itching in nose*, *blocked nose*, *ringing in ears*, and the service to book for each, with her own sentence explaining it. The agent looks the caller's words up through `suggest_service_for_problem` and reads back what it finds. It is not reasoning about the symptom. It is reading her instruction aloud.

The distinction is the whole point. We rejected the obvious version — let the model infer a service from the symptom — because that is a medical judgement in a booking's clothing, delivered in a confident voice on a recorded call. Instead there is no code path from a symptom to a service except through a mapping a clinician wrote. If she has written nothing for what the caller describes, the agent does not improvise: it says it would rather have someone from the clinic advise them, and hands the call to a person.

So the agent is specific where a clinician has been specific, and defers everywhere else. Matching is deliberately forgiving about grammar and unforgiving about meaning — stopwords dropped and one level of suffix stemming, so *"my nose is blocked"* finds the *blocked nose* rule, while `nose` never collapses into `nosebleed`.

And the routing reaches the guardrail as a structured turn signal rather than as text, so the policy decides over booleans it can be tested against, never over the model's prose.

The same discipline runs through the rest: no inventing a service the clinic does not offer, no claiming a write that did not happen, and no declaring a date unavailable until a tool has actually said so. All of it enforced in three independent layers — the system prompt, a deterministic guardrail policy over extracted turn signals, and the absence of any code path that could do otherwise.

### Handing a live call to a real person

Escalation that only writes a row and promises a callback is a dead end dressed up as a handover. So the doctor can take the call — actually take it, mid-conversation, while the caller is still on the line.

A console at `/live` shows calls happening right now, the ones asking for a person first. It rings when a call needs someone and puts it in the tab title, so the page does not have to be the thing being watched. The doctor presses one button and is on the call:

- **She speaks, in her own voice.** A second WebSocket carries her microphone to the caller as the same audio frame the caller's browser is already playing, and tees the caller's voice back to her. Nothing had to ship on the patient side for this to work.
- **Or she types**, and Amazon Polly speaks it down the same channel — useful in a room where talking aloud is awkward.
- **The agent goes fully quiet.** Not just muted: while a human holds the call the model is fed silence instead of the caller's audio, so it stops forming replies at all. Muting only its speaker was our bug, and the caller heard the agent answering questions meant for the doctor.
- **Nobody picks up? The caller is not left in silence.** At twelve seconds they hear that someone is still being fetched; at forty-five, an honest apology and a choice — leave a number, or ring back during opening hours. Spoken, not printed, because a caller is holding a phone rather than watching a screen.
- **If her tab dies, the agent takes the call back**, rather than the line going dead.

### Writing down what the human and the caller said

The written transcript pauses while she is on the call, because a model fed silence transcribes nothing. So the conversation is recovered from the audio afterwards.

The recording is stereo by design — caller left, clinic right — which turns out to be the thing that makes this possible. **Amazon Transcribe** reads the finished WAV with channel identification, so it labels who spoke rather than guessing from voices, and the result is appended to the call record:

```
--- transcribed from the call recording (both sides, after the call) ---
[00:02] patient: My ear has been hurting since Monday
[00:06] clinic: I can see you tomorrow morning at ten o'clock
```

**Batch, not streaming, and the reason is a dependency conflict worth naming.** The official streaming SDK, `amazon-transcribe`, pins `awscrt~=0.26.1`. Nova Sonic's bidirectional stream runs on `0.36.2`. Installing it silently downgraded the transport the entire voice agent depends on by ten minor versions — to add a transcript. That is the wrong trade, so we backed it out. Batch transcription needs only `boto3`, which was already a dependency: no new package, and nothing leaves AWS.

We considered the browser's own Web Speech API, which would have been live and free. We rejected it because Chrome's implementation ships audio to a third party, and a doctor discussing a patient is not a conversation to hand to someone else.

It runs only for calls a human took over, which needed a flag that survives the handback — every other call already has a transcript, and transcribing those would pay to re-derive one. It starts after the call record is persisted, is never awaited so it cannot delay a hang-up, and returns nothing on every failure path: a missing transcript is a gap in the record, while an exception there would damage the record itself.

One honest limit: the **audio is authoritative** and the text is a searchable aid. A verification run turned *"I can see you tomorrow morning at ten o'clock"* into *"I can see it at 10 o'clock"*. The words are captured; the exactness lives on the WAV.

### For the doctor

A role-aware dashboard: the day calendar with publish and block controls, a call activity log with transcripts and playback, patient records the doctor can correct in place, impact metrics, and a decisions feed. Live updates arrive over server-sent events, so no polling and no reload.

Behind it, a second agent nobody talks to. **Practice Intelligence** runs on a schedule over accumulated appointments, slots, waitlist entries and calls, and surfaces Decisions the doctor approves or dismisses:

| Decision | What triggered it |
| --- | --- |
| Gap fill | An open slot matches someone on the waitlist |
| Schedule gap | The same weekday is persistently empty at low utilisation |
| Unmet demand | Callers keep asking for a service the clinic does not offer |
| No-show trend | The no-show rate moved against the preceding equal-length period |

Approving the gap-fill books the earliest matching waitlisted patient and removes their entry. That is what makes this more than an answering machine: the calls become data, and the data becomes suggestions about how the practice runs.

## How we built it

**Four subsystems over one shared data layer.** Agents never touch storage directly.

```
                        browser (microphone)
                               |
                    wss:// /ws    16 kHz up · 24 kHz down
                               |
        +----------------------v-----------------------+
        |            Voice_Front_Desk                  |
        |  Strands BidiAgent + Nova Sonic              |
        |  12 tools · guardrails · barge-in            |
        |  turn signals -> policy -> tool orchestrator |
        +----------------------+-----------------------+
                               |
        +----------------------v-----------------------+
        |                 Data_Layer                   |
        |   per-entity store interfaces, swappable     |
        |   memory fakes   <->   DynamoDB single table |
        +------+-------------------------+-------------+
               |                         |
   +-----------v-----------+   +---------v-------------+
   | Practice_Intelligence |   |      Dashboard        |
   | scheduled detectors   |   | role-aware BFF + SSE  |
   | -> Decisions          |   | schedule · calls ·    |
   | doctor approves       |   | metrics · decisions   |
   +-----------------------+   +-----------------------+
```

### Amazon Nova Sonic, and why speech-to-speech changes the design

Most voice bots are three systems in a row: speech-to-text, then a language model on the transcript, then text-to-speech. Every hop adds delay, and worse, every hop throws information away. The transcript keeps the words and discards the hesitation, the rising tone of a question, the moment the caller started to interrupt.

Nova Sonic is a **single speech-to-speech model**. Audio goes in, audio comes out, and the model reasons over the sound rather than over a transcript of it. It runs on Bedrock over a persistent bidirectional stream — the `InvokeModelWithBidirectionalStream` API — rather than request-and-response, so both directions are open at once for the whole call. We send the caller's microphone as base64 PCM at **16 kHz mono**; the reply arrives as **24 kHz** audio in chunks we play as they land, so the agent starts speaking before it has finished deciding what to say.

Two consequences shaped the whole architecture.

**Barge-in is native, not bolted on.** Because the stream is always open in both directions, the model hears the caller talking over it. We pin the **v2 model**, `amazon.nova-2-sonic-v1:0`, specifically because turn detection with a configurable `endpointingSensitivity` is a v2-only provider setting — the v1 model silently ignores it, and endpointing is exactly what decides whether "mm-hm" is an interruption or just listening. (A trap worth naming: the v2 id really is `amazon.nova-2-sonic-v1:0`. There is no `amazon.nova-sonic-v2:0`, and granting that non-existent id in IAM fails the voice path with `AccessDeniedException` at connection time.)

**Tool calls race the agent's own speech.** This is the part that surprised us most and is unique to speech-to-speech. In a text agent the tool result always arrives before the reply is composed. Here the model may already be mid-sentence when a tool returns — which is why a slow lookup does not produce a delayed answer, it produces a *guessed* one. Nova Sonic's speed is what makes the experience feel human, and it is also what makes tool latency a factuality problem rather than a UX one.

We wrapped all of it behind a narrow `VoiceStream` boundary — a Protocol with `start`, `send_audio`, `stop_playback`, `close` and an event iterator. The Bedrock events (`BidiConnectionStartEvent`, `BidiResponseCompleteEvent`, `BidiConnectionCloseEvent`, `BidiErrorEvent`) are normalised into our own small vocabulary of stream events. That boundary is why the entire agent, including turn control and barge-in logic, is testable against a fake stream with no AWS and no audio hardware.

### The Strands Agents SDK, and what BidiAgent gives us

Strands supplies the agent loop we would otherwise have written badly. Its `BidiAgent` owns the conversation over a bidirectional model: it holds the session open, streams audio both ways, decides when the model wants a tool, invokes it, feeds the result back into the live stream, and keeps going — all without us hand-rolling a state machine over raw Bedrock events.

The part that mattered most for a clinic is the **tool contract**. A Python function decorated with `@tool` becomes something the model can call, and Strands derives the model-facing schema from the function's own signature and docstring. That has a consequence we leaned on heavily: **the docstring is not documentation, it is the interface.** When we needed the agent to explain a closed Sunday rather than say it found nothing, part of the fix was the tool's own description telling the model what to do with that field. The behaviour and its instructions live in the same place, so they cannot drift apart.

Every tool returns the same shape — success with a value, or failure with a typed error — so the model always receives a discriminated result rather than a stringly-typed maybe. Each tool is closed over its data-layer stores before the model ever sees it, so no store, table name or credential appears in the model-facing schema. The model can call `book_appointment`; it cannot reach the database.

Strands also let us keep the model **swappable and injectable**. The voice adapter builds a real `BidiNovaSonicModel` in production, but accepts an injected model or a fully-formed agent instead — which is exactly how 1,758 tests run without touching Bedrock. The real Strands and Bedrock imports happen lazily inside `start()` rather than at module import, so the rest of the system imports and tests cleanly on a machine with no AWS credentials and no native AWS Common Runtime build.

**The tool boundary is the architecture.** Twelve patient-facing tools: `match_offered_service`, `suggest_service_for_problem`, `check_availability`, `register_patient`, `lookup_patient`, `list_appointments`, `book_appointment`, `reschedule`, `cancel`, `add_to_waitlist`, `answer_faq`, `flag_for_human`. Two are deliberately absent — `fill_gap_from_waitlist` is doctor-approved only, and `analyze_patterns` belongs to Practice Intelligence.

**Guardrails run on structured signals, not on text.** Each patient turn is deterministically classified — *asks for a human, names a symptom, requests clinical content, expresses distress* — before the model responds, and the policy decides over those booleans rather than over raw text or the model's discretion.

**Two store implementations held to one contract.** In-memory fakes and DynamoDB, exercised by the same tests. That is what lets the whole suite run offline with no AWS and no cost, and what stops the DynamoDB implementation quietly drifting from the fake.

**One DynamoDB table, four GSIs.** GSI1 is the one that earns its keep: its sort key is the slot start, so availability skips past days *at the index* rather than reading and discarding them. Slot ids are derived from day, provider and start time rather than random — `slot-prov-raana-2026-09-15-0900` — which makes republishing a day idempotent, while booked and blocked slots are deliberately preserved so a republish can never strand a patient's appointment.

**Spec-driven, in Kiro.** Requirements, then design, then a task list in `.kiro/specs/`, then implementation against them. Every requirement has an id, and those ids appear in the code and test docstrings. When we later asked "why does availability take a `from_time`?", the answer was in the spec rather than in someone's memory.

**Deployed on CloudFront + EC2**, for an unglamorous reason: browsers refuse microphone access outside a secure context, so HTTPS was non-negotiable. CloudFront gives a valid certificate on `*.cloudfront.net` with no domain to buy, and forwards WebSocket upgrades. A `t4g.small` origin accepts traffic **only** from CloudFront's origin-facing IP ranges, so nobody can bypass the certificate over plain HTTP.

$$
\text{compute} = \$0.0168/\text{hr} \times 24\,\text{hr} \times 20\,\text{days} \approx \$8.06
$$

The public deployment serves the caller-facing routes, and the doctor's calendar, patient records and documents are not routed at all — every one of those paths returns **404 rather than 403**, because there is no handler mounted and therefore no role check to bypass.

The live console is the one exception, and it had to be. Calls in progress are held **in memory, per process**: a caller on the public URL is registered inside that container, so a console running on a laptop sees an empty list however much it is permitted to see. The call is not there to be taken. To answer a real call, the console has to be served by the process holding it.

So it is published behind a shared secret — opt-in via `CLINIC_CONSOLE_TOKEN`, compared with `hmac.compare_digest`, absent by default — and it publishes the live console *only*. Stored records, the calendar, documents, onboarding and the JSON tool surface stay unrouted **even with a valid token**. The secret buys calls in progress, never the clinic's history. Verified through CloudFront rather than just against the instance: console 200 with the token, 403 without, the doctor's audio socket upgrading to 101 only with it, and every record path still 404.

## Challenges we ran into

### The agent told a patient she did not exist

She quoted her patient code. Then her name and mobile number. Twice, the agent said it had no record of her. Her record was in the table the whole time.

The cause was speech-to-text inconsistency. The same speaker saying the same mobile number produced `9900012307` on one call and `"nine nine zero zero zero one two three zero seven"` on the next. Our phone comparison key was built by discarding non-digits, so a number transcribed as words reduced to the **empty string**:

```
'nine nine zero zero zero one two three zero seven'  ->  ''                  (wanted 9900012307)
's i three zero seven'                               ->  'SITHREEZEROSEVEN'  (wanted SI307)
```

An empty key matches nothing, silently. And the fallback path — *"let me look you up by name and number instead"* — failed for exactly the same reason, which is why she was told twice.

We fixed it in the normaliser rather than the prompt, because a deterministic conversion beats asking a model to behave. The subtle part: `"oh"` is both the digit zero and the name of the letter O. A patient code is two letters then three digits, so **position** settles it — `"s oh three zero seven"` is `SO307`, while `"nine oh two"` is `902`.

### Latency turned out to be a correctness bug, not a comfort one

The agent told a caller there were no Hearing Test slots on a day the calendar had plenty. Our first instinct was hallucination.

It was not. Nova Sonic runs tool calls *concurrently with speech*. The availability lookup took **8.1 seconds** against a year of published slots. The agent had already finished its turn, so it answered from its own guess because the tool had not come back yet.

The fix pushed both bounds into the query: the date floor as a sort-key condition, and `limit` consumed lazily so the earliest three slots are the first three read, rather than reading the partition and sorting afterwards.

$$
8.1\,\text{s} \;\longrightarrow\; 0.28\,\text{s}
$$

Still 0.28 s with 2,090 slots published across the rest of the year. That is the difference between the model having an answer before it speaks and inventing one.

### It confirmed a save that never happened

A caller gave her blood group, height and weight after booking. The agent said they were saved. We checked the record: `blood_group=None`, `height_cm=None`, `weight_kg=None`.

This was our own design error, and the worst kind. `register_patient` returned the existing record untouched when details arrived after booking — **and reported success**. The agent read "ok" and passed it on. Worse, the patient store had no update method at all, so there was no way to write to an existing record. The doctor would have read those empty fields as the patient declining to give them.

Fixed structurally rather than with wording. The store can now amend a record. A blank field gets filled; a field that already holds a value is never overwritten, since the caller may not be the person whose record it is. And the tool returns exactly which fields it wrote, which were already on file, and which it rejected — **the agent may only confirm what is in `recorded`.**

### "I couldn't find anything" sounded like "we're fully booked"

A caller asked for Sunday the 13th. Availability searches *on or after* a date, so it returned Monday's slots — correct times, reason missing. The agent said it could not find anything for the 13th.

That is word-for-word what a fully booked day sounds like. Callers gave up instead of taking Monday. The tool result now carries the closed weekday and a ready sentence, so the agent states a fact instead of filling a gap.

### Names

"Sidda Deepika" came back as "Siddha Devika". No normaliser fixes that — the audio genuinely sounded like that.

Two changes, and the split between them is deliberate. The agent now reads a name back and spells anything unusual. But it **never** reads health details aloud. And the doctor can edit any record from the page they are already on, which is the real safety net for every transcription error the agent did not catch.

### The deployment installed perfectly and never started

Cloud-init finished. Every dependency installed. The service was dead.

```
/etc/cron.d/clinic-restart: No such file or directory
Failed to run module scripts-user
```

Amazon Linux 2023 ships without cronie, so `/etc/cron.d` does not exist. Under `set -e`, writing a nightly-restart cron file aborted the script **two lines before `systemctl enable`**. Replaced with a systemd timer, which now also runs *after* the service starts, so it can never prevent it starting again.

Then the call itself failed twice more on things only a real connection reveals: a missing `dynamodb:Scan` grant, a missing `s3:GetObject` on the documents prefix, and finally a missing `[voice]` extra — which meant the page loaded, the WebSocket upgraded, and the call died the instant Nova Sonic was constructed.

### Every transcript held only half the conversation

The doctor reads the transcript to see what happened on a call. It showed her the questions and none of the answers — the agent's side was simply absent, on every call ever made.

The first guess was case sensitivity: Nova Sonic labels roles in upper case, and the code compared against `"assistant"`. Plausible, wrong. Fixed, deployed, still one-sided.

So we stopped reasoning about it and logged what the model actually emits:

```
role='user'      is_final=True
role='assistant' is_final=False
role='assistant' is_final=False
```

**Nova Sonic never marks its own output final.** The rule "only record finalised transcripts" is correct for the caller — their recognition changes word by word as they speak, and recording it would fill the record with half-heard guesses. Applied evenly, it deleted the agent's entire half. Finality is now required of the caller and not of the agent.

That fix had a consequence: unfinalised turns can arrive more than once as they are produced, so a turn extending the previous one replaces it and an exact repeat is dropped. Scoped to the agent only — and that scoping came from a test failing. Our first version collapsed 4,000 identical caller turns into one, which is when we realised a caller saying "yes" twice is a *fact about the call*, not noise to merge away.

This one hid better than any other bug in the project. Nothing errored, the transcript existed, and it read like a quiet call. You only notice if you already know what the agent said.

### Prices were quoted in dollars

`$500.00`, for a clinic in Tirupati charging rupees. A caller asking the consultation fee would have been told a number roughly eighty times the real one, in a confident voice, on a recorded line.

It is the same failure as inventing availability — a commitment stated as the clinic's word — and it had been sitting in the code the whole time, because no price had ever been configured, so the format had never been spoken aloud. The moment we set a real fee it became audible.

Fixed as a word rather than a symbol: `500 rupees`, not `₹500.00`. A speech model handed `₹` may read the symbol's name or skip it, and whole amounts drop the decimals because "five hundred point zero zero rupees" is not how anyone says a price.

### We had to fix our own verification twice

This one stung. Our deployment check reported PASS on a set of dashboard routes returning 404 — but four of those routes **did not exist in either mode**. It was asserting that nothing was serving paths nothing had ever served, while `/slots`, `/onboarding` and the whole `/dashboard/*` tree went untested.

Later, a check counting slots on a page searched for `>OPEN<`, found zero on every day, and printed "ok" anyway. The card renders "Open" and CSS uppercases it.

A test that passes while proving nothing is worse than no test, because it buys false confidence. Both now fail when the thing they describe is actually broken.

## Accomplishments that we're proud of

**It is live, and anyone can call it.** Not a video, not a localhost demo — a public HTTPS URL with a real certificate, real Nova Sonic audio and real DynamoDB writes. Verified end to end: `/ping`, the page, the assets, a genuine `wss://` handshake returning **101 Switching Protocols** through CloudFront, and a `session_started` frame that only arrives *after* the Bedrock stream opens.

**1,758 tests. `mypy --strict` clean across 112 source files.** All offline, no credentials needed — including property-based tests with Hypothesis and latency tests asserting response start $\le 1.5$ s and barge-in stop $\le 500$ ms.

**Fifty simultaneous callers, zero failures.** Every one got its own Nova Sonic session and its own distinct session id, with no Bedrock throttling: 3, 5, 10 and 50 concurrent calls against the live public URL. Greeting latency held near half a second at five callers and about four seconds at fifty — which we traced to thread-pool queueing on two vCPUs rather than anything in the model path.

**The dashboard is protected by not existing.** Access is decided by a `?role=` query parameter that the code itself documents as *not a security control*. On a public URL, anyone with the link would otherwise be the doctor, reading patient names, mobiles and blood groups. So the public build does not route those pages at all. **Not routing is a stronger guarantee than guarding**, and it is less code.

**A patient code a human can actually use** — and which we refused to treat as identity. With $N = 26 \times 26 \times 1000 = 676{,}000$ possible codes, the birthday approximation gives, for $n$ patients:

$$
P(\text{collision}) \approx 1 - e^{-n^{2}/2N}
$$

At $n = 1000$ that is already $\approx 52\%$, and real initials cluster far from uniform, so it is worse in practice. So a code **narrows a search and never settles it.** A caller quoting one still confirms their name, and a code matching two records is disambiguated, never guessed. Showing one patient another's appointments would be far worse than asking someone to repeat themselves.

**Every bug above became a test.** The dictated-number failure alone is now 26 tests covering `"double nine"`, `+91` prefixes, half-converted input like `"nine nine 000 one two 307"`, and the `"oh"` ambiguity in both directions.

## What we learned

**Latency is a correctness property in voice.** In a text chatbot a slow tool is a spinner. In speech-to-speech the model has already started talking, so a slow tool does not degrade the answer — it *replaces* it with a guess. We now treat every millisecond on the tool path as a factuality budget.

**"The model hallucinated" is usually a design excuse.** Every time we caught the agent saying something false, the cause was ours: a tool returning success on a no-op, a lookup key normalising to empty, a query too slow to arrive in time, a result carrying the right slots but not the reason. We fixed almost none of these with prompt wording.

**Say the reason, not just the outcome.** "I couldn't find anything" and "we're closed on Sundays" are the same fact and completely different answers. The first sends a patient elsewhere; the second sends them to Monday.

**Guard the capability, not the behaviour.** Asking a model to leave a judgement to the doctor is persuasion. Having no code path that maps symptoms to services is a guarantee. The interesting part was learning that this makes the agent *more* helpful rather than less: once it could not guess, we had to give it something real to say instead — the plain-language description of each service, and a clear "the doctor will confirm what's right for you."

**Verification has to be able to fail.** Two of our own checks passed while proving nothing. We now ask of every assertion: *what would make this fail?* If we cannot answer, it is not a test.

**Deploy early, because deployment finds bugs nothing else does.** Three failures — a missing IAM action, a missing IAM prefix, a missing dependency extra — were invisible to 1,500 passing tests and appeared within minutes of a real connection.

## What's next for Clinic Front Desk — the voice agent always on the line

**Amazon Connect, to carry the handover onto a real phone line.** The handover itself is done — a caller who asks for a person gets one, live, in the doctor's own voice, as described above. What Connect adds is the *telephone*: today both ends are browser tabs, and a clinic's front desk belongs on the clinic's actual number.

The integration is written, not merely planned: `flag_for_human` has a transport seam, the Connect client is implemented behind it, and 23 tests cover it. It activates on two environment variables, `CLINIC_CONNECT_INSTANCE_ID` and `CLINIC_CONNECT_FLOW_ID`.

It is not switched on because it cannot be, in this account:

```
InvalidRequestException: You're signed in with an AWS account that was provided
by AISPL. These accounts cannot create Amazon Connect instances.
```

We tested a valid alias in all nine Connect regions and got the same refusal in each; `ap-south-1` does not offer the service at all. AISPL is Amazon's Indian reseller, and the restriction is account-level and documented — not permissions, not a quota, nothing a support ticket moves. The only route is an AWS account with non-Indian billing.

That blockage is what produced the browser-based live takeover, and we would keep it either way: it works without a telephony provider at all, which matters for a clinic that has not bought one yet.

**Why Connect is the right instrument for this.** A handover is a telephony problem, and telephony is the part nobody should build themselves. Amazon Connect is a managed contact centre: it provides the phone number, the call routing, the hold behaviour, the queueing when the doctor is already on a call, and the agent-side interface — none of which we would want to assemble from SIP trunks and hope. What it exposes to us is an ordinary AWS API surface, scoped with ordinary IAM, so the agent asks for a call the same way it asks for anything else.

The vocabulary maps onto the clinic almost one-to-one. An **instance** is the clinic's contact centre. A **contact flow** is the script that runs when a contact starts — greet, look up who is calling, route them. A **queue** is where a contact waits when nobody is free. An **agent endpoint** is where a human actually answers, which for a solo practice is simply the doctor's mobile. And **contact attributes** are key-value pairs that travel with the contact through the whole flow, which is the mechanism that lets us hand over context rather than just a ringing phone.

The distinction between the two APIs matters for what the caller experiences. `StartOutboundVoiceContact` places a fresh call — right for a callback, where the patient has already hung up. `StartTaskContact` creates a work item on the live contact, which is what a **warm transfer** needs: the caller stays on the line and is passed to a person, rather than being told someone will ring back. The second is the experience we want, and the first is the honest fallback when nobody is available.

There is also a symmetry we like. The same Connect integration that carries a handover *out* is the one that lets calls come *in* over a real phone number instead of a browser tab. Today the agent answers a WebSocket; with Connect in place it answers the clinic's actual line, which is where a front desk belongs.

The seam is already there: escalation is a single tool with a typed result, not logic smeared through a prompt. The design we will implement:

1. **A Connect instance with a contact flow** holding a clinic queue, with the doctor's mobile as an agent endpoint.
2. **`flag_for_human` gains a transport.** After writing the `Escalation` row it calls `StartOutboundVoiceContact` — or `StartTaskContact` for a warm transfer of the live call.
3. **Escalation context travels with the contact** as Connect contact attributes: patient name, mobile, the reason, and the last few transcript turns. Whoever picks up starts informed instead of making the caller repeat everything.
4. **The `Escalation` row becomes the correlation key.** Connect's contact id is written back onto it, so the dashboard shows *offered → transferred → answered → resolved* rather than a row that only ever says "open".
5. **Failure stays explicit.** If the transfer does not connect, the agent says so and takes a message. Silently claiming a callback is the exact failure mode this system exists to avoid.

Two properties stay unchanged by that work, on purpose. The escalation is still **recorded first**, so a handover is never lost because a phone network was down. And a pending transfer is still not a licence to start advising — the agent keeps directing the judgement to the doctor while the caller waits.

**Outbound calls on the same stack.** No-show follow-ups, appointment reminders, and ringing the waitlist when a gap opens rather than waiting for the doctor to approve a Decision.

**Tool-call auditing.** The transcript captures speech. For a clinic we want every tool invocation and result recorded against the call, so a booking can be explained after the fact.

**Concurrent dashboard reads.** The metrics snapshot issues its per-day and per-service queries sequentially:

$$
90 \times 400\,\text{ms} \approx 36\,\text{s}
$$

which matches what we measured from a laptop. In-region it is a second or two. The reads are independent, so issuing them concurrently is a 10–20× win with no schema change.

**Telugu and Hindi.** Tirupati is not an English-first city. The clinic's patients would rather speak Telugu, and the whole premise — a front desk that is always on the line — only fully lands when it answers in the language the caller actually rings in.
