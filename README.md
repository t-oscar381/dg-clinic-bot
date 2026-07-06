# DG Clinic — WhatsApp Doctor Assistant
**Private AI assistant for the head doctor. Doctor-facing only. MVP Phase 1.**

---

## What this does
- Doctor sends a WhatsApp message → bot classifies intent → returns patient data or logs treatment
- All data stored in Supabase (PostgreSQL). AI brain is Claude API. Hosted on Railway.
- Only the registered doctor's WhatsApp number can use the bot.

---

## Week-by-Week Setup

### WEEK 1 — Foundation

#### Step 1: Clone & Install
```bash
git clone <your-private-repo-url>
cd dg-clinic-bot
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

#### Step 2: Supabase Database
1. Go to https://supabase.com → New Project → Singapore region
2. Project Settings → Database → Copy connection string
3. Go to SQL Editor → New Query → paste contents of `scripts/setup_db.sql` → Run All
4. Copy Project URL and anon/service_role key into `.env`

#### Step 3: WhatsApp Business API (Meta)
1. Go to https://developers.facebook.com → My Apps → Create App → Business
2. Add "WhatsApp" product to your app
3. In WhatsApp → Getting Started:
   - Register a dedicated phone number (not your personal WhatsApp)
   - Copy the **Phone Number ID** and **Temporary Access Token** to `.env`
4. In App Settings → Basic → copy **App Secret** to `.env`
5. Set `WHATSAPP_VERIFY_TOKEN` to any random string you choose (e.g. `dg_clinic_secret_2026`)

#### Step 4: Claude API
1. Go to https://console.anthropic.com → API Keys → Create Key
2. Copy key into `.env` as `ANTHROPIC_API_KEY`

#### Step 5: Deploy to Railway
```bash
# Install Railway CLI
npm install -g @railway/cli

# Login and deploy
railway login
railway init
railway up
```
Copy the Railway deployment URL (e.g. `https://dg-clinic-bot.railway.app`)

#### Step 6: Register Webhook with Meta
1. In Meta Developer Portal → WhatsApp → Configuration → Webhook
2. Callback URL: `https://your-railway-url.railway.app/webhook`
3. Verify Token: same value as `WHATSAPP_VERIFY_TOKEN` in `.env`
4. Subscribe to: `messages`
5. Click Verify and Save

#### Step 7: Set your Doctor's number
In `.env`, set `DOCTOR_WHATSAPP_NUMBER` to the doctor's number in E.164 format without +
Example: for +62 812-3456-789 → `628123456789`

#### Step 8: Test the echo bot
Send any message from the doctor's WhatsApp to the bot number.
Check Railway logs: `railway logs`
You should see the message received and a response sent back.

---

### WEEK 2 — Add Patient Data

#### Migrate existing patients
Prepare an Excel file with columns: Full Name, Nickname, DOB, Gender, VIP Tier, Allergies

```bash
# Preview first (dry run)
python scripts/migrate.py --file patients.xlsx --dry-run

# Import for real
python scripts/migrate.py --file patients.xlsx
```

#### Test patient lookup
Send from doctor's WhatsApp: `Gimana Sita?`
Bot should return Sita's profile card.

---

### WEEK 3 — Treatment Logging

Send from doctor's WhatsApp:
```
Log Sita: Retatrutide 10mg SC hari ini, next 4 weeks
```
Bot should confirm what was logged and show the next visit date.

---

## Environment Variables Reference

| Variable | Description | Where to get |
|---|---|---|
| `WHATSAPP_TOKEN` | Meta access token | Meta Developer Portal → WhatsApp → Getting Started |
| `WHATSAPP_PHONE_NUMBER_ID` | Bot's phone number ID | Same page as above |
| `WHATSAPP_VERIFY_TOKEN` | Any string you choose | Set this yourself |
| `WHATSAPP_APP_SECRET` | Meta app secret | App Settings → Basic |
| `DOCTOR_WHATSAPP_NUMBER` | Doctor's number (no +) | Doctor's phone |
| `ANTHROPIC_API_KEY` | Claude API key | console.anthropic.com |
| `SUPABASE_URL` | Supabase project URL | Supabase → Project Settings → API |
| `SUPABASE_KEY` | Supabase anon key | Same page as above |

---

## Conversation Examples

**Patient Lookup:**
```
Doctor: Sita siapa?
Bot:    🟢 Sita Rahardjo  ·  F  ·  38y
        🥇 Gold Member
        ⚠️ ALLERGIES: Penicillin
        
        — Last Treatment —
        📌 Retatrutide 10mg SC
        🗓  12 Jun 2026
        
        — Next Session —
        📅 26 Jun 2026 (in 14 days)
```

**Treatment Log:**
```
Doctor: Log Sita: Reta 12mg SC hari ini tolerated well, next 3 weeks
Bot:    ✅ Treatment logged
        Patient:   Sita Rahardjo
        Date:      29 Jun 2026
        Protocol:  Retatrutide 12mg SC
        Notes:     Tolerated well
        Next:      20 Jul 2026 (21 days)
```

**Help:**
```
Doctor: /help
Bot:    👋 DG Clinic Doctor Assistant
        ...full command list...
```

---

## Cost Monitoring
- Check `api_usage` table in Supabase to monitor Claude API token spend
- At ~300 queries/month, expect USD 10–20/month on Claude
- Railway starter: USD 5/month
- Supabase free tier: handles MVP volume easily

---

## Project Structure
```
dg-clinic-bot/
├── app/
│   ├── main.py              # FastAPI entry point
│   ├── config.py            # Settings from .env
│   ├── routes/
│   │   └── webhook.py       # GET+POST /webhook — core message handler
│   ├── services/
│   │   ├── whatsapp.py      # WhatsApp Cloud API client
│   │   ├── claude_ai.py     # Claude prompts + AI calls
│   │   └── patient.py       # DB operations + message formatting
│   └── models/
│       └── schemas.py       # Pydantic data models
├── scripts/
│   ├── setup_db.sql         # Run once in Supabase SQL Editor
│   └── migrate.py           # Import existing patients from Excel
├── .env.example             # Copy to .env and fill in
├── requirements.txt
├── Procfile                 # Railway start command
└── railway.toml             # Railway config
```

---

## Phase 2 (Weeks 4–6) — Coming Next
- Monday weekly digest (auto-sent 8am)
- Overdue patient alerts
- PDF lab result parsing
- Progression metrics tracking

---

*DG Clinic · Private · Not for distribution*
