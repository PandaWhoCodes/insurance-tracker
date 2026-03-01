# Insurance Policy Tracker — Playbook

A step-by-step guide to set up Gmail-based insurance policy extraction for anyone.

---

## Prerequisites

- Python 3.9+
- A Google account with Gmail
- ~10 minutes for initial setup

---

## Step 1: Google Cloud Setup (One-time, ~5 minutes)

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project (e.g., "Insurance Tracker")
3. Enable the **Gmail API**:
   - Navigate to **APIs & Services > Library**
   - Search "Gmail API" > Click **Enable**
4. Configure the **OAuth consent screen**:
   - Go to **APIs & Services > OAuth consent screen**
   - Choose **External** > Create
   - Fill in app name (e.g., "Insurance Tracker"), support email, developer email
   - Skip scopes for now
   - Under **Test users**, add the Gmail address you want to scan
   - Save
5. Create **OAuth credentials**:
   - Go to **APIs & Services > Credentials**
   - Click **Create Credentials > OAuth client ID**
   - Application type: **Web application** (not Desktop)
   - Name: anything (e.g., "Insurance Tracker")
   - Under **Authorized redirect URIs**, add: `http://localhost:8080/`
   - Click **Create**
6. Note down the **Client ID** and **Client Secret**

> **Why "Web application" and not "Desktop"?**
> Either works, but if you pick Web application, you must add `http://localhost:8080/` as a redirect URI. Desktop type handles localhost automatically but can be flaky with some setups. We use Web application with an explicit redirect URI for reliability.

---

## Step 2: Project Setup

```bash
# Create project directory
mkdir -p insurance_track/{attachments,data}
cd insurance_track

# Create .env file with your credentials
cat > .env << 'EOF'
GOOGLE_CLIENT_ID=<paste-your-client-id>
GOOGLE_CLIENT_SECRET=<paste-your-client-secret>
EOF

# Install dependencies (if not already installed)
pip install google-api-python-client google-auth-oauthlib pdfplumber python-dotenv
```

---

## Step 3: Copy the Script

Copy `fetch_insurance_emails.py` into the project directory.

The script does the following:
1. Authenticates with Gmail via OAuth (opens browser on first run)
2. Searches for insurance-related emails using configurable queries
3. Downloads PDF attachments
4. Extracts text from PDFs
5. Saves everything to `data/insurance_emails.json`

---

## Step 4: Customize Search Queries

Edit the `SEARCH_QUERIES` list in `fetch_insurance_emails.py` based on what the person has.

**For Indian insurance policies**, these queries work well:

```python
# Health insurance
"health insurance after:YYYY/MM/DD"
"mediclaim after:YYYY/MM/DD"

# Car/vehicle insurance
"car insurance after:YYYY/MM/DD"
"motor insurance after:YYYY/MM/DD"
"vehicle insurance after:YYYY/MM/DD"

# Term life insurance
"term insurance after:YYYY/MM/DD"
"term plan after:YYYY/MM/DD"
"term life after:YYYY/MM/DD"

# General (catches more but also more noise)
"insurance policy after:YYYY/MM/DD"
"policy renewal after:YYYY/MM/DD"
"policy document after:YYYY/MM/DD"
```

Replace `YYYY/MM/DD` with a date 1–2 years back.

**Important:** Also search for specific known policies by subject line or sender, especially older policies like term life that may have been emailed years ago. Example:

```python
"subject:\"Your Term Life policy copy\""
"from:iciciprulife.com"
"from:hdfclife.com"
```

### Common Indian Insurance Company Email Domains

| Company | Email domains to search |
|---------|------------------------|
| HDFC ERGO (Health/General) | `hdfcergo.com`, `hdfcergo.email` |
| ICICI Prudential (Life) | `iciciprulife.com` |
| ICICI Lombard (General) | `icicilombard.com` |
| Care Health | `careinsurance.com` |
| Star Health | `starhealth.in` |
| Max Life | `maxlifeinsurance.com` |
| SBI Life | `sbilife.co.in` |
| LIC | `licindia.in` |
| Acko | `acko.com` |
| Digit | `godigit.com` |
| Bajaj Allianz | `bajajallianz.co.in` |
| Tata AIA | `tataaia.com` |
| Niva Bupa | `nivabupa.com` |
| Policybazaar (broker) | `policybazaar.com` |

---

## Step 5: First Run

```bash
python3 fetch_insurance_emails.py
```

1. A browser window will open — sign in with the Gmail account
2. Grant "read email" permission
3. The script will search, download, and extract
4. A `token.json` file is created — subsequent runs won't need browser auth

**If you get `redirect_uri_mismatch`:** The redirect URI in Google Cloud Console doesn't match. Make sure `http://localhost:8080/` is listed exactly (with trailing slash) under Authorized redirect URIs.

**If you get `Address already in use`:** Port 8080 is occupied. Either kill the process on that port (`lsof -ti:8080 | xargs kill -9`) or change the port in the script and update the redirect URI in Google Cloud Console to match.

---

## Step 6: Analyze the Output

After the script runs, you'll have:

```
insurance_track/
├── data/
│   └── insurance_emails.json    # All emails + extracted PDF text
├── attachments/
│   ├── <policy_pdf_1>.pdf
│   ├── <policy_pdf_2>.pdf
│   └── ...
```

### What to Extract from Each Policy

Look for these fields in the PDF text:

| Field | Where to find it |
|-------|-----------------|
| Policy number | Policy schedule, cover letter |
| Plan name | Cover letter, schedule header |
| Insured name(s) | Policy schedule |
| Sum insured | Policy schedule, certificate |
| Premium amount | Premium receipt, cover letter |
| Policy start date | Schedule ("Policy Period - Start Date") |
| Policy end date | Schedule ("Policy Period - End Date") |
| Nominee | Policy schedule |
| Intermediary/Agent | Cover letter |

### Common Gotchas

1. **Term life policies** are often emailed only once at issuance. If the person bought it 3+ years ago, the `after:` date filter will miss it. Search without date filters: `subject:"policy copy" from:iciciprulife` or ask the person for the subject line.

2. **Some policies arrive as links, not attachments.** HDFC ERGO sometimes sends a "Complete your policy document" email with a download link rather than attaching the PDF. These need to be downloaded manually.

3. **Renewal emails vs policy documents.** Renewal reminders contain useful info (sum insured, premium, expiry date) but the actual policy document has the complete details. Look for emails with PDF attachments from the insurer.

4. **Multiple versions of the same policy.** Endorsements or corrections generate new policy documents. The most recent one is the active version.

5. **Promotional emails create noise.** Policybazaar, banks, and insurers send marketing emails that match insurance keywords. Filter by sender domain to separate real policies from spam.

---

## Step 7: Build the Summary

Create two files:

1. **`data/policy_summary.json`** — Structured, machine-readable policy data (see the existing file as a template)
2. **`POLICY_TRACKER.md`** — Human-readable summary with:
   - Policy overview table
   - Upcoming renewals sorted by date
   - Detailed info per policy
   - Insights and action items
   - Total annual spend

---

## Checklist for Each Person

```
[ ] Google Cloud project created with Gmail API enabled
[ ] OAuth credentials created (Web application type)
[ ] Redirect URI http://localhost:8080/ added
[ ] Person's email added as test user in OAuth consent screen
[ ] .env file created with client ID and secret
[ ] Search queries customized for their insurers
[ ] Script run successfully, browser auth completed
[ ] PDF attachments downloaded and text extracted
[ ] Manually search for older policies (term life, etc.) if not found
[ ] Policy summary JSON created
[ ] Tracker markdown created
[ ] All expiry dates verified against actual PDFs
[ ] Token.json and .env secured (not shared/committed)
```

---

## Re-running / Updating

To refresh data (e.g., after a renewal):

```bash
# Re-run the fetch script — token.json handles auth automatically
python3 fetch_insurance_emails.py

# Then update policy_summary.json and POLICY_TRACKER.md with new data
```

---

## Security Notes

- **Never commit `.env` or `token.json`** — these grant read access to the person's Gmail
- **Delete `token.json`** when done if this is someone else's machine
- **Revoke access** after extraction if it's a one-time job: [Google Account Permissions](https://myaccount.google.com/permissions)
- The OAuth scope is **read-only** (`gmail.readonly`) — the script cannot send, delete, or modify emails
