# Insurance Tracker — Future Roadmap

## 1. Document Upload Mode ✅

**Status:** Done.

- [x] Upload PDF button in header alongside "Refresh from Gmail"
- [x] `POST /api/policies/upload` endpoint with multipart PDF support
- [x] Password-protected PDF handling (prompts for password, retries)
- [x] Reuses extraction pipeline (`_grok_extract` + `finalize` for dedup)
- [x] Progressive OAuth scopes — login requests only basic profile, Gmail scope is opt-in
- [x] "Connect Gmail" button when Gmail scope not granted, "Refresh from Gmail" when granted
- [x] Vault key modal with helper text (replaces hardcoded default, warns about data loss)
- [x] User-friendly progress messages (no technical jargon like "Groq", "triage", etc.)
- [x] Documents auto-deleted after processing


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


## 4. Google OAuth Verification

**Status:** Partially done — needs manual GCP steps before resubmitting.

### Issues from previous verification attempt:
1. **Domain ownership not verified** — `insurance-hut.fly.dev` is not registered to us.
2. **Home page behind login** — The home page must show app information without requiring login.
3. **App name mismatch** — OAuth consent screen says "insurance-hut" but the home page doesn't match.

### Done:
- [x] Custom domain: `policies.life` with Fly.io certs, `BASE_URL` secret set
- [x] Public landing page with hero, "How it works", "Built on trust" sections — no login required
- [x] App rebranded to "Policies.life" across all pages (index, privacy, terms, how-it-works)

### Still needed (manual GCP steps):
- [ ] Verify `policies.life` domain ownership in Google Search Console
- [ ] Update OAuth consent screen app name to "Policies.life"
- [ ] Resubmit for verification


## 5. Export to PDF ✅

**Status:** Done.

- [x] "Export PDF" button below summary bar (right-aligned, subtle ghost style)
- [x] Client-side PDF via jsPDF + autoTable (CDN loaded, no build system)
- [x] Styled PDF: header with branding + date, summary stats, per-policy sections
- [x] All fields: policy number, type, period, sum insured, premium, members, vehicle, nominee, coverages, notes
- [x] Sorted by status (Active → Expiring Soon → Expired)
- [x] Locked policies show minimal info (provider, policy number, "Password Protected")
- [x] Hidden policies excluded
- [x] Page numbers on every page


## 6. SEO & Social Meta Tags

- [x] Add `<meta name="description">` with app summary
- [x] Add Open Graph tags (`og:title`, `og:description`, `og:image`, `og:url`) for link previews on Facebook/LinkedIn/WhatsApp
- [x] Add Twitter Card tags (`twitter:card`, `twitter:title`, `twitter:description`, `twitter:image`)
- [ ] Add a social preview image (1200x630) — clean branded card with "Policies.life" + tagline → drop as `/static/og-image.png`
- [x] Add `<link rel="canonical" href="https://policies.life/">`
- [x] Add favicon / apple-touch-icon (inline SVG favicon, apple-touch-icon path ready)
- [x] Add structured data (JSON-LD) for SoftwareApplication schema


## 7. Encrypted Cache (Cross-Device Persistence) ✅

**Status:** Done.

- [x] DB fallback: `GET /api/policies` falls back to Turso when file cache misses
- [x] Vault key prompt on load: If DB has encrypted data but no vault key in session, prompts user
- [x] Wrong vault key handling: Clears sessionStorage, re-prompts, gives helpful error after 2 failures
- [x] Cache warming: After successful DB load, warms the file cache for fast subsequent loads
- [x] Save flow: Refresh, upload, and unlock all save to both file cache and Turso DB


## 8. Mobile UX Overhaul ✅ (partial)

**Status:** Core mobile layout done. Some items remain.

### Done:
- [x] Header: overflow ⋯ menu with Upload PDF, Refresh from Gmail, Logout
- [x] Modals → bottom sheets with slide-up animation + swipe-to-dismiss
- [x] Filter bar: horizontal scroll
- [x] Touch targets: 44px minimum, active scale transforms
- [x] Safe areas: `env(safe-area-inset-bottom)` on footer, modals, toast
- [x] iOS zoom prevention: 16px font on all inputs
- [x] Sub-pages (how-it-works, privacy, terms) mobile-responsive

### Remaining:
- [ ] **Empty state on mobile:** Show "Fetch from Gmail" or "Upload Documents" buttons on the empty state screen so mobile users have a clear CTA (currently only accessible via ⋯ menu)
- [ ] **Screen-off / connection loss (mobile only):** When phone screen turns off during refresh (~60s auto-lock), SSE connection drops and UI shows "Connection lost." Server keeps running and saves to DB, but client never gets the `done` event.
  - **Wake Lock API:** Request `navigator.wakeLock.request('screen')` during refresh to prevent screen-off (Chrome Android, Safari iOS 16.4+, fails silently elsewhere)
  - **Auto-recover:** On `es.onerror` (mobile only — detect via `'ontouchstart' in window` or screen width), poll `GET /api/policies` every 3s up to 10 times to check if server finished
  - **Visibility recovery:** `visibilitychange` listener — when page becomes visible while `isRefreshing`, check if EventSource closed and trigger poll
  - **UX:** Show "Reconnecting..." during poll, then render results or show "Try reloading in a minute"


## 9. Performance / Cost Optimizations

- [ ] **Limit PDF page extraction to 10 pages max:** Insurance policy docs can be 50+ pages but all key info (policy number, dates, sum insured, members) is in the first few pages. Cap PyMuPDF extraction at 10 pages in `modal_app.py` `_do_fetch_and_extract()` to reduce LLM input tokens and speed up extraction.


## 10. Other Ideas (Lower Priority)

- **Policy comparison:** Side-by-side comparison of health plans (coverage, premium, sum insured)
- **Renewal tracking:** Track premium payment history across years
- **Family view:** Group policies by family member
- **PWA:** Make the app installable on mobile with offline support
- **Claim tracker:** Track claim submissions and status alongside policies
