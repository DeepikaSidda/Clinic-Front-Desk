# Clinic Front-Desk Voice Agent

**A voice agent that answers the clinic phone at 2am, and never invents an answer.**

A solo-doctor ENT clinic loses patients to a ringing phone. The doctor is mid-consultation,
nobody picks up, and the caller books somewhere else. A receptionist costs more than the
missed calls — and still goes home at six.

This answers every call, at any hour, and does the actual work: books, reschedules and
cancels against a real calendar, answers questions about the clinic from the doctor's own
uploaded documents, issues a patient code the caller can remember, and hands over to a
human the moment someone needs one.

It is deliberately, strictly administrative. Asked *"my ear hurts, what's wrong with me?"*
it does not guess. It declines and escalates.

> ### Call it yourself
> ### **https://d21u7cmj563imv.cloudfront.net/voice**
>
> Press **Start call** and speak. Needs a microphone and a Chromium-based browser.
> Try: *"I'd like to book a hearing test"*, then *"Sunday the thirteenth"*.
>
> The doctor's dashboard is deliberately **not** published at that URL — see the
> **Security posture** section below.

| | |
| --- | --- |
| **Voice** | Amazon Nova Sonic, speech-to-speech over Amazon Bedrock bidirectional streaming |
| **Agent** | Strands Agents SDK (`BidiAgent`), 12 tools |
| **Storage** | Amazon DynamoDB, single table, 4 GSIs |
| **Documents** | Amazon S3 + Bedrock embeddings, doctor-uploaded PDFs |
| **Hosting** | CloudFront + EC2 `t4g.small` (public demo) · Bedrock AgentCore Runtime (container) |
| **Quality** | **1,672 tests**, `mypy --strict` clean across **110** source files |
| **Language** | Python 3.12 |



---

## The core principle

**The agent decides what to say. It never decides what is true.**

Every fact a caller hears comes from a tool that read the database. The model's job is to
understand speech and choose which tool to call. It cannot invent availability, cannot
confirm a save that did not happen, and cannot answer a question about the clinic from its
own training.

That constraint is why the interesting parts of this repo are the tool boundaries rather
than the prompt.

---

## What happens on a call

1. The caller opens the page and presses **Start call**. Their microphone streams as
   **16 kHz mono PCM** to Nova Sonic over a bidirectional Bedrock stream; the reply comes
   back as **24 kHz** audio. Speech-to-speech — no transcribe-then-synthesise hop, which is
   why it sounds like a conversation rather than a voice assistant.
2. If a recordings bucket is configured, its **first sentence** says the call is recorded
   and offers a human if the caller objects. That notice is attached wherever a recording
   store exists, so it cannot be silently skipped — and it is absent when recording is off,
   so it never claims to record when it is not.
3. Every patient turn is classified into **structured signals** — asks for a human, names a
   symptom, requests clinical content, expresses distress — before the model gets to
   respond. Guardrails run on those signals, not on raw text, and not on the model's
   discretion.
4. Tools run, the calendar is read and written, and the caller hears only what the tools
   returned.
5. **Interrupt it mid-sentence and playback stops immediately** (barge-in, under 500ms —
   there is a latency test for it). No waiting out a paragraph.
6. On hang-up the call is finalised with an outcome: `booked`, `rescheduled`, `cancelled`,
   `waitlisted`, `escalated`, `no_action`, or `interrupted`.


---

## What the agent can do

### Book an appointment

The caller names a service. It is matched against the clinic's offered services **by exact
name** — no inference, no fuzzy guessing. The agent then reads the calendar and offers up to
**three concrete dated slots** ("Monday the fourteenth at nine, nine-thirty, or ten"),
because three is what a person can hold in their head on a phone call.

It looks the caller up or creates a record, writes the appointment, and reads back the
service, the date, the time, and the patient code.

The service on the appointment is **the service the caller asked for**, not the label the
slot was published under. A slot is the doctor's half hour, published without knowing who
will take it; an appointment is one named person coming in for one named thing. A diary
where every row says "ENT Consultation" tells the doctor nothing they could not read off the
clock.

### Reschedule

*"I want to move my Tuesday appointment to eleven."*

It finds the caller's existing appointments **first** — that is the only source of an
appointment id. It never asks a caller for a booking reference, because nobody has one to
hand. Then it moves the appointment and **releases the old slot** back onto the calendar so
someone else can take it.

### Cancel

Same lookup, then cancel and release the slot — after explicit confirmation. Confirm-before-mutate
is enforced by the orchestrator, not left to the prompt.

### Read the calendar correctly

Availability is a bounded index query, not a scan:

- never offers a slot that has already started
- never offers the same minute twice, even if two records share a start time
- never offers a slot outside what the doctor actually published
- respects the hour the caller asked for as a **floor**, not a filter — a full afternoon
  rolls into the next day rather than returning nothing

And a hard boundary: the agent can **book and release** slots. It can **never create** one.
No patient-facing tool can publish availability. Otherwise a caller pressing for an earlier
time would eventually be offered a slot the doctor never opened.

### Explain closed days instead of failing

Ask for Sunday the thirteenth:

> *"The thirteenth falls on a Sunday, which is our clinic holiday — we're closed that day.
> The nearest I have is Monday the fourteenth at nine."*

Availability searches *on or after* a date, so a Sunday request used to return Monday's
slots with the reason missing — and the agent said *"I couldn't find anything for the
thirteenth"*, which is exactly what a fully booked day sounds like. Callers gave up instead
of taking Monday.

Driven by the configured opening hours, so a clinic that opens Sunday and shuts Tuesday
needs no code change.

### Issue a patient code the caller can remember

After booking, the agent issues and reads back a five-character code: **first letter of the
first name, last letter of the last name, last three digits of the mobile.** *Sailaja Devi*
on 9900012307 becomes **SI307**.

Quoting it next call resolves the caller instantly. It is explicitly **not proof of
identity** — five characters collide, so a code narrows a search and never settles it. A
caller quoting one still confirms their name, and a code matching two records is
disambiguated, never guessed.

### Understand a number however it is spoken

Speech-to-text is inconsistent: the same person saying the same mobile number produces
`9900012307` on one call and *"nine nine zero zero zero one two three zero seven"* on the
next. Both resolve to the same patient. So does `"s i three zero seven"` → `SI307`, and
`"double nine..."`, and a `+91` prefix, and a half-converted `"nine nine 000 one two 307"`.

### Confirm names aloud, and never health details

It reads a name back and spells anything unusual — *"that's Deepika, D-E-E-P-I-K-A?"* — the
way a receptionist does. Names are exactly where transcription fails and exactly where a
mistake follows the patient around.

Deliberately the opposite for health details, which it **never reads aloud**. A name is
worth confirming out loud; a blood group announced to whoever else is in the room is not.

### Take and amend patient details

Age, blood group, height, weight, callback mobile. Blood group is normalised into a closed
set, because speech-to-text renders "A positive" a dozen ways and a free-text blood group in
a medical record is worse than none — it looks authoritative and cannot be relied on.

Two rules on writes:

- A **blank** field gets filled. A field that **already holds a value is never
  overwritten**, since the person calling may not be the person in the record.
- The tool returns exactly which fields it wrote, which were already on file, and which it
  rejected. **The agent may only confirm what is in `recorded`.**

### Answer questions about the clinic

Hours, address, directions, the full service list, and fees. Service details come from
**PDFs the doctor uploads** through the portal, indexed with Bedrock embeddings — so updating
what the agent knows is uploading a document, not editing code or redeploying.

Pricing is the sharpest illustration of the core principle. The demo clinic's document does
**not** list fees, so the agent says reception will confirm the price. It does not estimate,
does not reason from typical ENT pricing, and does not quote a figure the doctor never
entered. A wrong price quoted with confidence on a recorded call is a commitment the clinic
has to honour. If the doctor enters a price, the agent quotes it; if not, it says so.

The clinic briefing is built fresh at the start of every call, so a configuration change is
live on the next call with no restart.

### Take a waitlist entry

If nothing suitable is free, it records who wanted what and when. That is not a dead end:
the second agent later spots an open gap and proposes filling it from the waitlist, and the
doctor approves with one click.

### Route a described problem using the doctor's own rules

*"I'm facing severe itching inside my nose — which service should I book?"*

Callers do not know that what they need is called an ENT Consultation, and they should not
have to. So the agent routes described problems to services — reading **rules the doctor wrote
herself**, stored in the table alongside everything else:

| The doctor writes | The caller hears |
| --- | --- |
| phrases: `itching in nose`, `blocked nose` → **ENT Consultation**, with her own sentence explaining why | that sentence, then an offer to book it |
| nothing matching what they described | that the clinic would rather advise them directly — and the call is handed to a person |

The agent is not reasoning about the symptom; it is reading a clinician's instruction aloud.
We rejected letting the model infer a service, because that is a medical judgement in a
booking's clothing, delivered confidently on a recorded line.

Matching is forgiving about grammar and strict about meaning: stopwords dropped, one level of
suffix stemming, all phrase words required. So *"my nose is blocked"* finds the *blocked nose*
rule, while `nose` never collapses into `nosebleed`. The result reaches the guardrail as a
**structured turn signal**, so the policy decides over testable booleans rather than prose.

Manage the rules with `scripts/set_symptom_routes.py`; check them with
`scripts/check_symptom_routing.py`.

### Hand over to a human — live, in the doctor's voice

If a caller asks for a person, becomes distressed, or asks anything clinical, it stops and
hands over — recording the reason, the transcript so far, and the signals that triggered it.
Distress alone *offers* a handover rather than forcing one; a following "yes" accepts it.

And the handover is **delivered, not just filed**. The doctor opens `/live?role=doctor`, which
rings when a call needs someone, and takes the call with her own microphone while the caller is
still on the line. See [Human handover](#human-handover-and-amazon-connect).


---

## What the agent refuses to do

This is as much of the product as the feature list, and it is enforced in **three
independent layers**: the system prompt, a deterministic guardrail policy over extracted
turn signals, and the absence of any code path that could do the forbidden thing.

**No clinical advice, triage, or diagnosis.** *"My ear hurts, what's wrong with me?"* gets a
refusal and an offer of a human.

**No symptom-to-service inference of its own.** The agent *does* route described problems to
services — but only by reading rules **the doctor wrote herself**, never by reasoning from the
symptom. There is no code path from a symptom to a service except through a clinician-authored
mapping, so the guardrail cannot be talked around: the capability to guess does not exist. If
she has written nothing for what the caller describes, the agent says it would rather have
someone from the clinic advise them, and hands the call over. See
[Symptom routing](#route-a-described-problem-using-the-doctors-own-rules).

**No inventing services.** Asked about a treatment the clinic does not offer, it says so
rather than improvising something plausible.

**No claiming a write that did not happen.** It may only confirm the fields the store
reports as written.

**No declaring a date unavailable** until a tool has actually said so.

**No creating availability**, ever.

---

## What the doctor sees

Every call produces a durable record.

**Transcript** — always captured, timestamped per turn:

```
[00:04] patient: hi, what are your clinic hours and how much does a hearing test cost?
[00:09] agent:   We're open Monday to Saturday, 9am to 8pm. On the fee, our
                 information doesn't list a price for the Hearing Test, so
                 reception will confirm that for you.
```

**Recording** — only when a bucket is configured. One **stereo WAV per call at 24 kHz:
patient on the left channel, agent on the right**, laid out on the real timeline so pauses
are preserved and the two stay in sync. You can hear who interrupted whom, and a barge-in
sounds like an interruption instead of garbled mono. Stored at
`call-recordings/<YYYY>/<MM>/<DD>/<call_session_id>.wav`, served as a short-lived presigned
URL so audio goes browser → S3 directly.

**The dashboard**, role-gated:

| View | Doctor | Assistant |
| --- | --- | --- |
| Day schedule and calendar | ✅ | ✅ |
| Call activity log + transcripts + playback | ✅ | ✅ |
| Patient records (and correcting them) | ✅ | ✅ |
| Impact metrics | ✅ | ❌ |
| Decisions feed | ✅ | ❌ |
| Uploaded documents | ✅ | ❌ |

The assistant's permitted set is **enumerated rather than subtracted**, so a view added later
is not silently granted to them.

Live updates arrive over server-sent `ChangeEvent`s — no polling, no reload. Approve the
gap-fill decision and the schedule updates in place.

The doctor can also **edit any patient record** from the page they are already looking at.
That is the real safety net for every transcription error the agent did not catch.

---

## The second agent: Practice Intelligence

A background agent nobody talks to. It runs on a schedule (≤ 24h, configurable) over
accumulated appointments, slots, waitlist entries and call sessions, and surfaces
**Decisions** for the doctor to approve or dismiss:

| Decision | What triggered it |
| --- | --- |
| **Gap fill** | An open slot matches someone on the waitlist |
| **Schedule gap** | The same weekday is persistently empty at low utilisation |
| **Unmet demand** | Callers keep asking for a service the clinic does not offer |
| **No-show trend** | The no-show rate moved against the preceding equal-length period |

Approving the **gap-fill** decision is the one with a real automated action: it books the
earliest matching waitlisted patient and removes their entry. The rest are advisory —
operational changes the doctor makes off-system — so approving records the approval.

This is what makes the system more than an answering service: the calls become data, and the
data becomes suggestions about how the practice runs.

---

## Architecture

Four subsystems over one shared data layer. Agents never touch storage directly.

```
                        browser (microphone)
                               |
                    wss:// /ws    16 kHz PCM up · 24 kHz down
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
        |   + ChangeEvent emitter                      |
        +------+-------------------------+-------------+
               |                         |
   +-----------v-----------+   +---------v---------------------+
   | Practice_Intelligence |   |          Dashboard            |
   | scheduled detectors   |   |  role-aware BFF + SSE         |
   | -> Decisions          |   |  schedule · calls · metrics   |
   | doctor approves       |   |  decisions · patients · docs  |
   +-----------------------+   +-------------------------------+
```

**Voice_Front_Desk** — reactive, patient-facing. A Strands `BidiAgent` voiced by Nova Sonic.
Per-call `SessionContext` retains gathered facts across turns, so a caller never repeats a
detail they already gave.

**Practice_Intelligence** — autonomous, doctor-facing, scheduled.

**Dashboard** — a role-aware backend-for-frontend rendering server-side HTML plus live
partials, with an SSE change stream.

**Data_Layer** — abstract per-entity stores with two implementations, **held to the same
contract by the same tests.** That is what makes the entire agent testable offline with no
AWS and no cost, and what stops the DynamoDB implementation drifting from the fake.

---

## The tool suite

Twelve patient-facing tools. The model chooses *which* to call; it never gets to decide what
is true.

| Tool | Purpose |
| --- | --- |
| `match_offered_service` | Map a caller-named service to one the clinic offers. Exact match only |
| `suggest_service_for_problem` | Route a *described* problem using the doctor's own written rules. Never infers |
| `check_availability` | Open slots for a service — dated, at most three, closed days flagged |
| `register_patient` | Create or amend a patient record; returns exactly what it wrote |
| `lookup_patient` | Find by name + mobile, or by five-character code |
| `list_appointments` | The caller's appointments — the only source of an appointment id |
| `book_appointment` | Write the appointment against a slot |
| `reschedule` | Move an appointment, releasing the old slot |
| `cancel` | Cancel and release, after confirmation |
| `add_to_waitlist` | Record demand when nothing suitable is free |
| `answer_faq` | Hours, address, services, pricing — from config and uploaded documents |
| `flag_for_human` | Record a handover with its reason and context |

Two tools are deliberately **absent** from the patient-facing set: `fill_gap_from_waitlist`
is doctor-approved only, and `analyze_patterns` belongs to Practice_Intelligence.

---

## Design decisions that came from real failures

Every item here is a bug that happened on a real call, and the fix that followed. The
reasoning is the point.

### Five-character patient codes, not UUIDs

Asked for a reference on a live call, a caller had nothing to give. Nobody memorises
`3422c38f-637e-4d43-a187-34ad6038a3f5`, so she spelled her name out twice instead. Five
characters can be said once and written on the back of a hand.

### Dictated numbers broke lookup silently

The phone comparison key was built by discarding non-digits — so a number transcribed as
words reduced to the **empty string**. An empty key matches nothing, quietly. The caller was
told the clinic had no record of her. It did. Then the fallback path asked for her name and
number and failed for the same reason, so she was told twice.

Fixed in the normaliser rather than the prompt, because a deterministic conversion beats
asking a model to behave. `"oh"` is both zero and the letter O, so position settles it: a
patient code is two letters then three digits.

### Closed days are named, not silently skipped

Covered above, under **Explain closed days instead of failing**. The tool result now carries
the closed weekday and a ready sentence, so the agent states a fact instead of filling a gap.

### The agent may only confirm what was written

`register_patient` once returned the existing record untouched when details arrived *after*
booking — and reported success. The agent read "ok" and told the caller her blood group,
height and weight were saved. They were not, and the doctor would have read those empty
fields as her declining to give them.

Fixed structurally: the store can amend an existing record, and the tool reports exactly
which fields it wrote.

### Latency is a correctness problem, not a comfort problem

Nova Sonic runs tool calls concurrently with speech. A slot lookup taking **8.1 seconds**
against a year of published slots finished *after* the agent had already taken its turn — so
it answered from its own guess instead of the calendar.

Availability now pushes both bounds into the query: the date floor as a sort-key condition,
and `limit` consumed lazily so the earliest three slots are the first three read. Measured
**8.1s → 0.28s** with a full year published. That is the difference between the model having
an answer before it speaks and inventing one.

### One service label for a whole published day is a data bug, not a display bug

A day published as "ENT Consultation" reported every *other* service as fully booked, and the
doctor's diary showed twenty identical rows. Availability now ignores the label a day was
published under and searches the provider's own calendar; the appointment records the service
the caller actually asked for.

### Republishing must never strand an appointment

Slot ids are derived from day, provider and start time rather than random, so republishing a
day replaces rather than duplicates. But republishing deliberately **preserves booked and
blocked slots** — reopening a booked slot would leave a patient holding an appointment on a
slot advertising itself as free, and reopening a blocked one would hand back time the doctor
deliberately took off the calendar.

---

## Human handover and Amazon Connect

When a caller asks for a person, becomes distressed, or asks anything clinical, the agent
stops. `flag_for_human` records the reason, the transcript so far, and the signals that
triggered it, and the escalation appears in the doctor's call activity log within five
seconds.

### Current state — the handover is delivered, live

A caller who asks for a person gets one, while still on the line.

`/live?role=doctor` lists calls in progress, the ones needing someone first. It **rings** and
shows a count in the tab title, so the page does not have to be the thing being watched. One
button puts the doctor on the call:

| Control | What happens |
| --- | --- |
| **🎤 Take over & talk** | Her microphone goes to the caller, and the caller's voice comes back to her — a real two-way conversation |
| **Say** | She types; Amazon Polly speaks it down the caller's existing audio channel |
| **Hand back** | The agent resumes the call |

**The agent stops listening, not just speaking.** While a human holds the call the model is
fed silence in place of the caller's audio, so it forms no replies at all. Muting only its
output was a real bug: the agent kept answering questions meant for the doctor and printed
"interrupted — playback stopped" every time the caller spoke to her.

**Nobody available? The caller is not left in silence.** At 12 seconds they hear that someone
is still being fetched; at 45, an honest apology and a choice — leave a number, or call back
during opening hours. Spoken aloud, because a caller is holding a phone, not watching a
screen. Both intervals are configurable.

**If the doctor's tab dies, the call goes back to the agent** rather than to dead air.

The written transcript pauses while she is on the call — a model fed silence transcribes
nothing — so the gap is **marked** in the record, with the conversation itself preserved on
the call recording.

### Amazon Connect — implemented, blocked by the account

Connect would put both ends on a real phone number instead of browser tabs. The code is
written and covered by 23 tests, activating on `CLINIC_CONNECT_INSTANCE_ID` and
`CLINIC_CONNECT_FLOW_ID`. It cannot be enabled in this account:

```
InvalidRequestException: You're signed in with an AWS account that was provided
by AISPL. These accounts cannot create Amazon Connect instances.
```

Tested with a valid alias in all nine Connect regions — the same refusal each time;
`ap-south-1` does not offer the service at all. AISPL is Amazon's Indian reseller and the
restriction is account-level and documented: not permissions, not a quota, not something a
support ticket changes. The only route is an AWS account with non-Indian billing.

The design, for whoever has such an account:

1. **A Connect instance with a contact flow** holding a clinic queue, with the doctor's
   mobile as an agent endpoint.
2. **`flag_for_human` gains a transport.** After writing the `Escalation` row it calls
   `connect:StartOutboundVoiceContact` — or, for a warm transfer of the live call,
   `StartTaskContact` carrying the escalation id.
3. **Escalation context travels with the contact** as Connect contact attributes: patient
   name, mobile, the reason, and the last few transcript turns. Whoever picks up starts
   informed instead of making the caller repeat everything.
4. **The `Escalation` row is the correlation key.** Connect's contact id is written back onto
   it, so the dashboard shows *offered → transferred → answered → resolved* rather than a row
   that only ever says "open".
5. **Failure is explicit.** If the transfer does not connect, the agent must say so and take
   a message. Silently claiming a human will call back is the exact failure mode this system
   exists to avoid.

Two things stay unchanged by that work, on purpose. The escalation is still **recorded
first**, so a handover is never lost because a phone network was down. And the agent still
refuses clinical questions — a pending transfer is not permission to triage while waiting.



---

## Data model

One DynamoDB table, four GSIs, every entity in it: appointments, slots, patients, waitlist
entries, call sessions, escalations, decisions, clinic configuration, documents.

| Access pattern | Served by |
| --- | --- |
| A provider's day, every slot status | main partition |
| Open slots for a service, from a date, in start order | **GSI1** (sort key is slot start) |
| A patient's appointments | **GSI2** |
| A patient by name + mobile | **GSI3** |
| A patient by five-character code | attribute query |
| Recent calls, escalations, decisions by status | per-entity partitions |

GSI1 is what makes availability fast with a year published: the date floor is a sort-key
condition, so past days are skipped **at the index** rather than read and discarded, and the
read stops as soon as `limit` slots are collected.

Slot ids are human-legible and deterministic: `slot-prov-raana-2026-09-15-0900`.

---

## Setup

Python **3.12+**.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[test]"
```

**Extras:** `voice` (real Nova Sonic speech-to-speech via the AWS Common Runtime), `deploy`
(Starlette + uvicorn), `test`, `dev` (`test` plus mypy and ruff).

The `voice` extra is what makes this a voice agent — `aws-sdk-bedrock-runtime` and `awscrt`
are kept out of the base install so the agent, data layer and dashboard stay installable and
testable without native builds. Omitting it means the page loads, the WebSocket upgrades, and
the call dies the moment Nova Sonic is constructed.

### Provisioning real AWS

```powershell
# once
python scripts/create_table.py --table clinic-front-desk --region us-east-1
python scripts/create_recordings_bucket.py --bucket clinic-recordings-<account-id>

# configure the clinic
python scripts/set_clinic_services.py
python scripts/set_clinic_hours.py --open 09:00 --close 20:00

# publish a calendar (skips days the clinic has no hours for)
python scripts/publish_slots.py --until 2026-12-31 --dry-run
python scripts/publish_slots.py --until 2026-12-31
python scripts/publish_slots.py --until 2026-12-31 --report

# serve
python scripts/serve_aws.py --seed-config
```

`create_recordings_bucket.py` provisions with public access blocked, default encryption, a
TLS-only policy, and a lifecycle rule expiring recordings after 90 days. **Versioning is
left off deliberately** — with it on, lifecycle expiration only adds a delete marker and the
audio survives as a noncurrent version, which is the opposite of a retention policy.

---

## Running it locally

### In-memory, no AWS, no cost

```powershell
python scripts/demo_dashboard.py
```

Seeds a realistic clinic — a day of appointments including a no-show, open slots, a
waitlist, 48 handled calls, an escalation, three open Decisions — into in-memory stores.

| URL | Shows |
| --- | --- |
| `/?role=doctor` | Every view |
| `/?role=assistant` | Schedule + call log only |
| `/` | Access denied, no data regions at all |
| `/slots?role=doctor` | Day calendar, publish and block controls |
| `/documents?role=doctor` | Uploaded clinic documents |
| `/onboarding` | Clinic setup wizard |
| `/voice` | Browser voice client |

`127.0.0.1` counts as a secure context, so the browser grants microphone access without a
certificate.

### Against real persistence

```powershell
$env:CLINIC_TABLE_NAME="clinic-front-desk"
$env:AWS_REGION="us-east-1"
$env:CLINIC_RECORDINGS_BUCKET="clinic-recordings-<account-id>"
python scripts/serve_aws.py
```

### Without a microphone

`talk_to_agent.py` synthesises your line with Amazon Polly and streams it in:

```powershell
python scripts/talk_to_agent.py
python scripts/talk_to_agent.py --speak "I'd like to book a hearing test" --save-audio reply.wav
python scripts/talk_to_agent.py --session-id my-call --speak "What are your hours?"
curl "http://127.0.0.1:8080/dashboard/calls/my-call?role=doctor"
```

Nova Sonic is speech-to-speech, so a bare text turn gets no reply — everything goes in as
audio either way. `--debug` prints every server message.

### Worth trying

| Say | Expect |
| --- | --- |
| "What are your hours?" | `answer_faq` |
| "How much is a hearing test?" | `answer_faq`. No price is configured, so it says reception will confirm — it does not invent a figure |
| "I'd like to book a hearing test" | `check_availability`, up to 3 dated slots |
| "Sunday the thirteenth" | Named as the clinic holiday, nearest working day offered |
| "My code is S-I-3-0-7" | Resolved to the patient record |
| "Do you treat tinnitus?" | Not offered — says so, no guessing |
| "My ear hurts, what's wrong with me?" | Declines, escalates, call ends `escalated` |
| "My ear hurts" (symptom only) | Routes via the doctor's own rules, or hands over if she wrote none |
| "Can I speak to a human?" | Escalates immediately |
| "This is unacceptable" | *Offers* a handover; a following "yes" accepts it |

---

## Configuration reference

Every setting is an environment variable, so one image runs against any table or region
without a rebuild.

| Variable | Default | Purpose |
| --- | --- | --- |
| `CLINIC_TABLE_NAME` | `clinic-front-desk` | DynamoDB table |
| `AWS_REGION` / `CLINIC_REGION` | `us-east-1` | Region for DynamoDB, S3, Bedrock |
| `CLINIC_BACKEND` | `dynamodb` | `memory` swaps in in-memory fakes |
| `CLINIC_VOICE_ONLY` | *(off)* | `1` serves only the caller-facing voice routes |
| `CLINIC_CONSOLE_TOKEN` | *(unset)* | Publishes the **live console only** on a voice-only host, behind this shared secret. Minimum 24 characters, refused at startup below that. Unset = no console routed |
| `CLINIC_UNATTENDED_AFTER_SECONDS` | `45` | How long a caller waits for a human before the agent apologises and offers a message. Must be long enough to actually answer — see below |
| `CLINIC_HOLDING_AFTER_SECONDS` | `12` | When the caller is told someone is still being fetched, so the wait above is not silence |
| `CLINIC_CONNECT_INSTANCE_ID` | *(unset)* | Amazon Connect instance; set with the flow id to route handovers to a phone line |
| `CLINIC_CONNECT_FLOW_ID` | *(unset)* | Connect contact flow for the handover |
| `CLINIC_NOVA_SONIC_MODEL_ID` | *(v1)* | Nova Sonic model id |
| `CLINIC_RECORDINGS_BUCKET` | *(unset)* | **Unset = no audio captured at all** |
| `CLINIC_RECORDINGS_PREFIX` | `call-recordings/` | S3 key prefix |
| `CLINIC_RECORDINGS_SSE` | `AES256` | Or `aws:kms` |
| `CLINIC_RECORDINGS_KMS_KEY_ID` | *(unset)* | Required with `aws:kms` |
| `CLINIC_DOCUMENTS_BUCKET` | *(recordings bucket)* | Uploaded clinic PDFs |
| `CLINIC_DOCUMENTS_PREFIX` | `clinic-documents/` | S3 key prefix |
| `CLINIC_DOCUMENTS_SSE` | `AES256` | Or `aws:kms` |
| `CLINIC_DOCUMENTS_KMS_KEY_ID` | *(unset)* | Required with `aws:kms` |
| `CLINIC_EMBEDDING_MODEL_ID` | *(Bedrock)* | Document search embeddings |
| `CLINIC_EXTRACTION_MODEL_ID` | *(Bedrock)* | Pre-fills clinic config from a PDF |
| `CLINIC_ANALYSIS_INTERVAL_HOURS` | `24` | Practice Intelligence cadence |
| `CLINIC_CREATE_TABLE_IF_MISSING` | *(off)* | `1`/`true`/`yes` |
| `CLINIC_DYNAMODB_ENDPOINT_URL` | *(unset)* | Point at DynamoDB Local |
| `CLINIC_LOG_LEVEL` | `INFO` | |
| `PORT` | `8080` | Bind port |

`CLINIC_VOICE_ONLY` fails safe: anything other than `1`, `true`, `yes` or `on` serves the
full application. Recording is **opt-in by configuration, never by default** — no bucket
means no patient audio is captured.

---

## Testing

**1,672 tests. `mypy --strict` clean across 110 source files.** The whole suite runs
offline against in-memory stores and a fake voice stream — no credentials, no cost.

```powershell
pytest                    # everything
pytest -m property        # property-based correctness (Hypothesis, >=100 iterations)
pytest -m integration     # cross-component propagation
pytest -m latency         # response start <= 1.5s, barge-in stop <= 500ms
pytest -m "not latency"
mypy src
```

Tiers are markers in `pytest.ini`: `property`, `integration`, `smoke`, `latency`; unit tests
are unmarked. Both store implementations are held to one shared contract, so a DynamoDB
behaviour that drifts from the fake fails the same test.

### Live checks, cheapest first

```powershell
python scripts/check_aws.py                  # credentials, region, Nova Sonic access
python scripts/live_voice_smoke.py           # Polly speaks, Nova Sonic answers
python scripts/check_availability_speed.py   # the lookup the agent makes mid-call
python scripts/check_closed_day.py           # what the agent is told about a Sunday
python scripts/check_slots_page.py           # the calendar renders published days
python scripts/lookup_patient.py --code SI307
python scripts/publish_slots.py --report     # calendar coverage
```

Anything past `check_aws.py` makes real, billable Bedrock and Polly calls.

---

## Deployment

### CloudFront + EC2 — the public demo

Browsers refuse microphone access outside a secure context, so HTTPS is **mandatory**.
CloudFront supplies a valid certificate on `*.cloudfront.net` with no domain to buy, and
forwards WebSockets.

```powershell
python deploy/deploy_voice_agent.py --plan       # print the plan, change nothing
python deploy/deploy_voice_agent.py              # build it
python deploy/verify_public.py                   # prove the public surface
python deploy/smoke_call.py                      # place a real call through it
python deploy/deploy_voice_agent.py --teardown
```

Re-runnable — every resource is looked up by name and reused. It builds an IAM role scoped to
one table and the two real Nova Sonic model ids, a `t4g.small` behind a security group
admitting **only CloudFront's origin-facing prefix list**, an Elastic IP so the origin
survives an instance rebuild, and a distribution with caching disabled and all viewer headers
forwarded — the latter is what makes the WebSocket `Upgrade` header survive.

The instance runs a systemd unit with `Restart=always`, log rotation, and a nightly restart
timer.

Operating it, over SSM, since the instance has no SSH key and admits only CloudFront:

```powershell
python deploy/remote_exec.py --status
python deploy/remote_exec.py --logs
python deploy/remote_exec.py --update    # pull the current source bundle and restart
python deploy/remote_exec.py "df -h; free -m"
```

Roughly **$8 of compute for 20 days**. Bedrock audio is per-second and is the variable
cost — **set a budget alarm before publishing the link.**

### Bedrock AgentCore Runtime

A single ARM64 container serving `GET /ping`, `POST /invocations` and `WebSocket /ws` on
port 8080. Full walkthrough in [`deploy/README.md`](deploy/README.md), including the
execution role and the scheduled Practice Intelligence run via EventBridge.

---

## Security posture

**The dashboard is not published.** The hosted demo runs with `CLINIC_VOICE_ONLY=1`, mounting
only `/ping`, `/voice`, `/ws` and `/static/*`. Every dashboard route returns **404 — not
403**, because no handler is mounted, so there is no role check to get past.

**The live console is the one publishable exception, and it is opt-in.** Calls in progress are
held in memory *per process*, so a caller on the public URL is registered inside that
container and a console running anywhere else sees an empty list no matter what it is allowed
to see — the call is not there to take. Setting `CLINIC_CONSOLE_TOKEN` publishes the live
console on the public host, gated on that shared secret rather than on `?role=`, compared with
`hmac.compare_digest`. Unset by default, and tokens shorter than 24 characters are refused at
startup rather than quietly accepted.

It publishes the **live console only**. Stored patient records, the calendar, documents,
onboarding and the `/invocations` tool surface stay unrouted *even with a valid token* — the
secret buys calls in progress, never the clinic's history. The doctor's audio socket honours
it too, and refuses the WebSocket handshake before accepting rather than after.

Worth stating plainly: the token travels in the URL, so it lands in browser history and
anyone it is forwarded to keeps access until it is rotated. `python deploy/publish_console.py
--revoke` returns the host to voice-only.

That matters because dashboard access is decided by a `?role=` query parameter, documented in
the code as **not a security control**. On a public URL anyone with the link would otherwise
be the doctor, reading patient names, mobile numbers and blood groups. Not routing is a
stronger guarantee than guarding, and less code.

`deploy/verify_public.py` asserts this against the live URL — all nine dashboard routes plus
`POST /invocations`, each asked as `?role=doctor`.

Also in place:

- **The origin accepts traffic only from CloudFront**, so nobody can bypass the certificate
  over plain HTTP.
- **The execution role is scoped** to one table and its indexes, two S3 prefixes, and the two
  real Nova Sonic model ids.
- **No `s3:DeleteObject` on recordings** — audio is expired by a lifecycle rule, not by the
  application.
- **The recording notice cannot be skipped** where a recording store exists, and is absent
  where one does not.
- **Health details are never spoken aloud.**
- **IMDSv2 is required** on the instance.

---



## Acknowledgements

Amazon Nova Sonic for speech-to-speech, the Strands Agents SDK for the bidirectional agent
and tool loop, and Amazon Bedrock, DynamoDB, S3, CloudFront and Polly for everything
underneath.
