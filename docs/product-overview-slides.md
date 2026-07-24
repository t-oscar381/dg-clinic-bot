# Robby — Product Overview (slide-by-slide)

> Feed this into Claude's slideshow: "Make a presentation from this slide content."
> Each `## Slide` is one slide: **Title**, on-slide bullets, and _speaker notes_.
> Product: **Robby** — a WhatsApp AI assistant for DG Clinic (Jakarta luxury
> homecare aesthetic & wellness). Tone: clear, confident, non-jargon.

---

## Slide 1 — Title
**Robby**
Your clinic's WhatsApp AI assistant

- Turns a doctor's WhatsApp messages into structured clinical records — automatically.
- DG Clinic · Jakarta · Homecare aesthetic & wellness

_Speaker notes: Robby is a private AI assistant the doctors already talk to on WhatsApp — no new app to learn. This deck explains what it does and why it matters._

---

## Slide 2 — The problem
Doctors are great at medicine, not at paperwork

- After every homecare visit, patient data lives in the doctor's head, chat threads, or a messy shared spreadsheet ("Dokumen Hati Gembira").
- Each doctor keeps their own sheet → inconsistent, hard to total, easy to lose.
- Revenue, protocols, follow-ups — all trapped in free text.

_Speaker notes: The clinic runs on WhatsApp and one giant Excel file. It works, but nothing is queryable, nothing is standardized, and knowledge walks out the door with the doctor._

---

## Slide 3 — The solution
Robby listens on WhatsApp and files everything for you

- Doctor sends a normal message (typed **or** voice note, Indonesian/English).
- Robby understands it, confirms the details, and saves a clean record.
- No forms, no app, no new habit — just how they already communicate.

_Speaker notes: The core insight — meet the doctor where they already are. The AI does the structuring, not the human._

---

## Slide 4 — How it works (in one line)
WhatsApp → Robby (AI) → clean database → clinic dashboard

- **Doctor**: "Visit Radita, NAD+ IV 500mg, bayar 600rb + 250rb homecare, follow-up 2 minggu."
- **Robby**: confirms the patient, reads it back, saves the visit + the money.
- **Admin**: sees it instantly in a web dashboard, exportable to their format.

_Speaker notes: One message becomes a structured patient visit with revenue attached. The read-back is the safety step — the doctor confirms before anything is saved._

---

## Slide 5 — What Robby captures today
A full clinical + business record from a chat message

- **Patients & visits** — who, what treatment, dosage, route, follow-up date.
- **Revenue** — treatment vs. homecare fee, totals (queryable, not trapped in notes).
- **Voice notes** — transcribed automatically, echoed back to confirm.
- **Payment proofs** — forwarded transfer screenshots are read by AI, stored, and attached to the visit.
- **Daily recap** — the doctor types "/recap" and gets today's visits + revenue.

_Speaker notes: Every one of these is live in production today. Safety-first: identity confirmation before writing, never guesses a dose, reads back every entry._

---

## Slide 6 — The Knowledge Bank (the doctor's second brain)
Capture expertise before it's forgotten

- The head doctor talks to Robby; whatever he *knows* becomes a note.
- AI tidies each note into a **topic** + **summary**, keeping his exact words.
- Separate from patient records — it's his personal knowledge library.
- Foundation for training staff and, later, letting Robby answer questions from it.

_Speaker notes: This is the differentiator. Clinics lose senior expertise constantly. Robby captures it passively, just by the doctor talking._

---

## Slide 7 — The clinic dashboard
One place to see and manage everything

- **Patient Records** — every visit, filter by date, one-click CSV export in the clinic's own format.
- **Import** — bring in existing spreadsheet data (add-only, safe).
- **Recycle bin** — delete safely; recover within 30 days.
- **Knowledge Bank** — browse the doctor's notes, with an AI summary popup.

_Speaker notes: The doctors never touch this — it's for the admin/owner. The bot writes; the dashboard reads. Both share one secure database._

---

## Slide 8 — Under the hood (kept simple)
Proven, low-cost, boring-on-purpose infrastructure

- **WhatsApp Business Cloud API** — the doctor's interface.
- **Claude AI** (Anthropic) — the brain that understands and structures.
- **FastAPI on Railway** — the always-on service.
- **Supabase (Postgres)** — one secure database for patients, revenue, knowledge.
- **Next.js on Vercel** — the admin dashboard.

_Speaker notes: Nothing exotic. Everything here is standard, cheap, and scales. The intelligence is in the prompts and the workflow, not heavy infrastructure._

---

## Slide 9 — The economics
Cheap to run, priced to sell

- **~$0.02–0.03 per doctor inquiry** (measured, real usage).
- A busy clinic ≈ **$10–30/month** of AI cost.
- Hosting is negligible; the database free tier carries early volume.
- Leaves healthy margin at any reasonable monthly subscription.

_Speaker notes: This is the business case. The marginal cost per clinic is tiny and scales linearly, so the unit economics work from clinic #1._

---

## Slide 10 — Where it's going
Built to grow from one clinic to many

- **Richer capture** — full intake fields (address, diagnosis, delivery, vitals) matching the clinic's logbook.
- **Ask your notes** — Robby answers questions from the Knowledge Bank (semantic search).
- **Multi-clinic** — one deployment serving many clinics, each fully isolated.
- **Cheaper still** — routing simple tasks to a smaller AI model.

_Speaker notes: The architecture already leaves the door open for all of this. Nothing here is a rewrite — it's additive._

---

## Slide 11 — The vision
From one clinic's assistant to a platform

- Robby proves out at DG Clinic, then becomes a product other clinics pay for.
- Every clinic that joins keeps its own data, its own doctors, its own knowledge.
- The moat: passive capture of clinical + business + knowledge data no one else has.

_Speaker notes: The endgame — a WhatsApp-native operating system for small aesthetic/wellness clinics, sold as a subscription. DG Clinic is customer zero and the proof._

---

## Appendix — one-line summary
Robby turns the messages a doctor already sends on WhatsApp into a clean,
exportable clinical + revenue + knowledge database — no new app, ~$0.02 a
message, live today at DG Clinic.
