# Insurance Tracker — Future Roadmap

## 1. Document Upload Mode

**Problem:** Currently the app requires Gmail read access just to get started. Many users won't want to grant email permissions upfront — they just want to manually add a policy.

**Solution:** Two modes of adding policies:

### Mode A: Manual Upload
- User signs in with Google (basic profile scope only — email + name)
- Upload policy PDFs directly from their device
- Same AI extraction pipeline runs on the uploaded PDF
- No Gmail permissions needed

### Mode B: Gmail Scan (current flow)
- User explicitly opts in to Gmail scanning
- Triggers a **second OAuth flow** with the additional `gmail.readonly` scope
- Works exactly as it does today

### Implementation Notes
- **Progressive scopes:** Initial login requests only `openid` + `userinfo.email`. Gmail scope requested separately only when user clicks "Scan Gmail".
- **Upload endpoint:** `POST /api/policies/upload` accepting multipart PDF files
- **Reuse extraction pipeline:** `PipelineService.extract()` already handles PDF text → Grok → structured policy. Upload mode just skips the Gmail fetch and triage stages.
- **UI:** Add an "Upload Policy" button alongside "Refresh from Gmail" in the header. Drop zone or file picker in a modal.
- **Storage:** Uploaded PDFs could be stored encrypted in Turso (as blobs) or just processed and discarded like Gmail PDFs.


## 2. Multi-Email Scanning

**Problem:** Users may have insurance emails spread across multiple Gmail accounts (personal + spouse + work).

**Solution:** Allow linking additional email accounts, all mapping to the same user profile.

### How It Works
- "Add another email" button in settings/header
- Triggers OAuth for the new email (with Gmail scope)
- Stores a separate token per linked email
- All linked emails' policies merge into the same dashboard
- Each policy tagged with which email it came from

### Implementation Notes
- **DB change:** New `linked_emails` table: `user_id`, `email`, `token_path`, `added_at`
- **Pipeline change:** Refresh iterates over all linked emails, merges results before finalize/dedup
- **UI:** Show which email each policy was found in (subtle badge on card or in modal details)
- **Token management:** Each linked email gets its own `tokens/{email}.json` file (already works this way)


## 3. Expiry Alerts

**Problem:** The whole point of tracking policies is to not miss renewals. Currently the app shows "days left" but doesn't proactively notify.

**Solution:** Customizable alerts that notify users before a policy expires.

### Alert Options
- User picks per-policy or global default: **1 month**, **2 months**, **4 months** before expiry
- Multiple alerts per policy (e.g., "4 months + 1 month")
- Choice of notification channel (start with email, later add push/WhatsApp)

### Implementation Notes
- **DB tables:**
  - `alert_preferences`: `user_id`, `policy_id` (nullable for global default), `months_before`, `channel`
  - `sent_alerts`: `user_id`, `policy_id`, `alert_type`, `sent_at` (prevent duplicate sends)
- **Alert engine:** Background job (cron or scheduled task) that:
  1. Queries all policies with expiry dates
  2. Checks if any fall within the user's alert window
  3. Checks `sent_alerts` to avoid re-sending
  4. Sends notification (email via Gmail API or a transactional email service)
- **UI:** Alert settings modal per policy (bell icon on card) + global defaults in a settings page
- **Email content:** "Your [plan_name] policy expires in [X] days on [date]. Policy number: [number]."

### Future Extensions
- WhatsApp alerts (via Twilio or Meta Business API)
- Push notifications (if we add a PWA service worker)
- Calendar integration (add renewal dates to Google Calendar)


## 4. Other Ideas (Lower Priority)

- **Policy comparison:** Side-by-side comparison of health plans (coverage, premium, sum insured)
- **Renewal tracking:** Track premium payment history across years
- **Family view:** Group policies by family member
- **Export:** Download all policy data as PDF summary or CSV
- **PWA:** Make the app installable on mobile with offline support
- **Claim tracker:** Track claim submissions and status alongside policies
