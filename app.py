import os
import json
import asyncio
import logging
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build as google_build

from services.gmail_service import GmailService
from services.grok_service import GrokService
from services.cache_service import CacheService
from services.pipeline_service import PipelineService
from services.db_service import db as turso_db
from services import db_service

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# Allow OAuth over HTTP for localhost
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
TOKENS_DIR = DATA_DIR / "tokens"
TOKENS_DIR.mkdir(parents=True, exist_ok=True)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
]

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SESSION_SECRET", "dev-fallback"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

cache = CacheService()
grok = GrokService()


@app.on_event("startup")
async def startup():
    try:
        await turso_db.connect()
        await turso_db.init_schema()
        logger.info("Turso DB connected and schema initialized")
    except Exception as e:
        logger.warning(f"Turso DB init failed (refresh will still work without caching): {e}")


@app.on_event("shutdown")
async def shutdown():
    await turso_db.close()


def create_oauth_flow() -> Flow:
    client_config = {
        "web": {
            "client_id": os.getenv("GOOGLE_CLIENT_ID"),
            "client_secret": os.getenv("GOOGLE_CLIENT_SECRET"),
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost:8080/auth/callback"],
        }
    }
    flow = Flow.from_client_config(client_config, scopes=SCOPES)
    flow.redirect_uri = "http://localhost:8080/auth/callback"
    return flow


# ── Routes ──────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = BASE_DIR / "static" / "index.html"
    return HTMLResponse(html_path.read_text())


@app.get("/how-it-works", response_class=HTMLResponse)
async def how_it_works():
    html_path = BASE_DIR / "static" / "how-it-works.html"
    return HTMLResponse(html_path.read_text())


@app.get("/privacy", response_class=HTMLResponse)
async def privacy():
    html_path = BASE_DIR / "static" / "privacy.html"
    return HTMLResponse(html_path.read_text())


@app.get("/terms", response_class=HTMLResponse)
async def terms():
    html_path = BASE_DIR / "static" / "terms.html"
    return HTMLResponse(html_path.read_text())


@app.get("/auth/login")
async def login(request: Request):
    flow = create_oauth_flow()
    authorization_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    request.session["oauth_state"] = state
    return RedirectResponse(authorization_url)


@app.get("/auth/callback")
async def callback(request: Request):
    import os as _os
    _os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"
    flow = create_oauth_flow()
    flow.fetch_token(authorization_response=str(request.url))
    credentials = flow.credentials

    # Get user info
    oauth2 = google_build("oauth2", "v2", credentials=credentials)
    user_info = oauth2.userinfo().get().execute()
    user_email = user_info["email"]
    user_name = user_info.get("name", user_email)

    # Save token per user
    token_path = TOKENS_DIR / f"{user_email}.json"
    with open(token_path, "w") as f:
        f.write(credentials.to_json())

    # Set session
    request.session["user_email"] = user_email
    request.session["user_name"] = user_name

    return RedirectResponse("/")


@app.get("/auth/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/")


@app.get("/api/me")
async def get_me(request: Request):
    email = request.session.get("user_email")
    if not email:
        return JSONResponse({"authenticated": False}, status_code=401)
    return {
        "authenticated": True,
        "email": email,
        "name": request.session.get("user_name", email),
    }


@app.get("/api/policies")
async def get_policies(request: Request):
    email = request.session.get("user_email")
    if not email:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    cached = cache.get(email)
    if cached:
        return {
            "policies": cached["policies"],
            "fetched_at": cached["fetched_at"],
            "from_cache": True,
        }
    return JSONResponse({"error": "No cached data"}, status_code=404)


@app.post("/api/policies/refresh")
async def refresh_policies(request: Request):
    email = request.session.get("user_email")
    if not email:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    try:
        # Step 1: Fetch from Gmail
        logger.info(f"Starting refresh for {email}")
        gmail = GmailService(email)
        pdf_texts = gmail.fetch_insurance_emails()
        logger.info(f"Found {len(pdf_texts)} documents with text")

        if not pdf_texts:
            cache.set(email, [])
            return {"policies": [], "fetched_at": datetime.now().isoformat(), "from_cache": False}

        # Step 2: Extract with Grok
        logger.info("Sending to Grok for extraction...")
        policies = grok.extract_policies(pdf_texts)
        logger.info(f"Extracted {len(policies)} policies")

        # Step 3: Cache
        cache.set(email, policies)

        return {
            "policies": policies,
            "fetched_at": datetime.now().isoformat(),
            "from_cache": False,
        }

    except Exception as e:
        logger.error(f"Refresh failed: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


def sse_event(event_type: str, data: dict) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


@app.get("/api/policies/refresh-stream")
async def refresh_stream(request: Request, vault_key: str = "Ashish"):
    email = request.session.get("user_email")
    if not email:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    user_name = request.session.get("user_name", email)

    async def event_generator():
        # DB state — may be None if Turso is unavailable
        user_id = None
        vault_key_derived = None
        known_msg_ids = set()
        extracted_msg_ids = set()
        cached_extractions = []

        try:
            # Try to set up DB context (non-fatal if Turso unavailable)
            if turso_db._client is not None:
                try:
                    user_id = await db_service.get_or_create_user(email, user_name)
                    vault_key_derived = await db_service.verify_vault_key(user_id, vault_key)
                    known_msg_ids = await db_service.get_processed_msg_ids(user_id)
                    # Load cached extractions early so we know which ones actually decrypt
                    cached_extractions, failed_decrypt_ids = await db_service.get_cached_extractions(
                        user_id, vault_key_derived
                    )
                    # Extracted = relevant emails that already have extraction_json
                    rows = await turso_db.query(
                        """SELECT msg_id FROM processed_emails
                           WHERE user_id = ? AND is_relevant = 1 AND extraction_json IS NOT NULL""",
                        [user_id],
                    )
                    extracted_msg_ids = {r["msg_id"] for r in rows} - failed_decrypt_ids
                    if failed_decrypt_ids:
                        logger.warning(f"{len(failed_decrypt_ids)} extractions failed to decrypt, will re-extract")
                    logger.info(f"DB: {len(known_msg_ids)} known, {len(extracted_msg_ids)} extracted, {len(cached_extractions)} cached for {email}")
                except ValueError as e:
                    yield sse_event("error_event", {"message": str(e)})
                    return
                except Exception as e:
                    logger.warning(f"DB lookup failed, proceeding without cache: {e}")
                    user_id = None
                    vault_key_derived = None

            # Phase 0: Gmail metadata fetch
            yield sse_event("progress", {
                "stage": "gmail", "pct": 0,
                "message": "Searching Gmail for insurance emails...",
            })

            gmail = GmailService(email)
            metadata = await asyncio.to_thread(gmail.fetch_email_metadata)

            yield sse_event("stage_complete", {
                "stage": "gmail", "total": len(metadata),
                "message": f"Found {len(metadata)} emails",
            })

            if not metadata:
                cache.set(email, [])
                yield sse_event("done", {
                    "policies": [], "fetched_at": datetime.now().isoformat(),
                })
                return

            # Stage 1: Triage (skip already-triaged emails)
            pipeline = PipelineService()
            relevant_emails = []
            async for event in pipeline.triage(
                metadata, skip_msg_ids=known_msg_ids, user_id=user_id
            ):
                if event["type"] == "progress":
                    yield sse_event("progress", event)
                elif event["type"] == "stage_complete":
                    relevant_emails = event["relevant_emails"]
                    yield sse_event("stage_complete", {
                        k: v for k, v in event.items() if k != "relevant_emails"
                    })

            # Also include previously-triaged relevant emails (from DB)
            if known_msg_ids and user_id is not None:
                db_relevant_rows = await turso_db.query(
                    """SELECT msg_id FROM processed_emails
                       WHERE user_id = ? AND is_relevant = 1""",
                    [user_id],
                )
                db_relevant_ids = {r["msg_id"] for r in db_relevant_rows}
                # Build metadata for DB-known relevant emails not already in relevant_emails
                new_relevant_ids = {m["msg_id"] for m in relevant_emails}
                for meta in metadata:
                    if meta["msg_id"] in db_relevant_ids and meta["msg_id"] not in new_relevant_ids:
                        relevant_emails.append(meta)

            if not relevant_emails:
                cache.set(email, [])
                yield sse_event("done", {
                    "policies": [], "fetched_at": datetime.now().isoformat(),
                })
                return

            # Stage 2: Extract (skip already-extracted emails)
            raw_policies = []
            async for event in pipeline.extract(
                gmail,
                relevant_emails,
                skip_msg_ids=extracted_msg_ids,
                user_id=user_id,
                vault_key_derived=vault_key_derived,
            ):
                if event["type"] == "progress":
                    yield sse_event("progress", event)
                elif event["type"] == "stage_complete":
                    raw_policies = event["raw_policies"]
                    yield sse_event("stage_complete", {
                        k: v for k, v in event.items() if k != "raw_policies"
                    })

            # Stage 3: Finalize — merge cached extractions with new
            yield sse_event("progress", {
                "stage": "finalize", "pct": 88,
                "message": "Deduplicating and finalizing...",
            })

            # cached_extractions already loaded during DB setup phase above
            if not cached_extractions:
                logger.info("No cached extractions available")

            logger.info(f"Finalizing {len(raw_policies)} new + {len(cached_extractions)} cached")
            final_policies = await pipeline.finalize(
                raw_policies, existing_policies=cached_extractions
            )
            logger.info(f"Finalized: {len(final_policies)} policies")

            # Save final policies to DB (encrypted)
            if vault_key_derived is not None and user_id is not None:
                try:
                    await db_service.save_final_policies(
                        user_id, final_policies, vault_key_derived
                    )
                    logger.info("Saved final policies to DB")
                except Exception as e:
                    logger.warning(f"Failed to save final policies to DB: {e}")

            cache.set(email, final_policies)
            logger.info(f"Sending done event with {len(final_policies)} policies")
            yield sse_event("done", {
                "policies": final_policies,
                "fetched_at": datetime.now().isoformat(),
            })

        except Exception as e:
            logger.error(f"Pipeline failed: {e}", exc_info=True)
            yield sse_event("error_event", {"message": str(e)})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/policies/unlock")
async def unlock_pdf(request: Request):
    """Try to open a password-protected PDF with the user-provided password."""
    email = request.session.get("user_email")
    if not email:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    body = await request.json()
    pdf_path = body.get("pdf_path", "")
    password = body.get("password", "")
    vault_key = body.get("vault_key", "Ashish")

    if not pdf_path or not password:
        return JSONResponse({"error": "Missing pdf_path or password"}, status_code=400)

    # Security: ensure the path is within the attachments directory
    import pdfplumber
    attachments_dir = str(BASE_DIR / "attachments")
    resolved = str(Path(pdf_path).resolve())
    if not resolved.startswith(attachments_dir):
        return JSONResponse({"error": "Invalid path"}, status_code=403)

    if not Path(pdf_path).exists():
        return JSONResponse({"error": "PDF file not found"}, status_code=404)

    # Try opening with password
    try:
        text = ""
        with pdfplumber.open(pdf_path, password=password) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text += page_text + "\n"

        if not text or len(text.strip()) < 50:
            return JSONResponse({"error": "Password accepted but no text could be extracted"}, status_code=422)

        # Send to Grok for extraction
        pipeline = PipelineService()
        doc = {
            "pdf_filename": Path(pdf_path).name,
            "email_subject": body.get("email_subject", ""),
            "pdf_text": text,
        }
        result = await pipeline._grok_extract(doc)

        if not result:
            return JSONResponse({"error": "Could not extract policy details from PDF"}, status_code=422)

        # Remove the locked flag
        result.pop("password_protected", None)
        result.pop("locked_pdf_path", None)
        result.pop("password_hint", None)

        # Fix status based on today's date
        end = result.get("policy_end")
        if end:
            try:
                from datetime import datetime as dt
                end_date = dt.strptime(end, "%Y-%m-%d").date()
                result["status"] = "ACTIVE" if end_date >= dt.now().date() else "EXPIRED"
            except (ValueError, TypeError):
                pass

        # Update cache — replace the locked policy, remove duplicates
        cached = cache.get(email)
        if cached:
            policies = cached["policies"]
            result_pn = result.get("policy_number", "")

            # Remove any locked entries that match this policy number
            policies = [
                p for p in policies
                if not (p.get("password_protected") and p.get("policy_number") == result_pn)
            ]

            # Also remove any existing non-locked duplicate (from email body extraction)
            if result_pn:
                policies = [
                    p for p in policies
                    if p.get("policy_number") != result_pn
                ]

            # Add the freshly unlocked policy
            policies.append(result)
            cache.set(email, policies)

        # Also update DB if available
        user_name = request.session.get("user_name", email)
        if turso_db._client is not None:
            try:
                user_id = await db_service.get_or_create_user(email, user_name)
                vault_key_derived = await db_service.verify_vault_key(user_id, vault_key)
                await db_service.save_final_policies(user_id, cache.get(email)["policies"], vault_key_derived)
            except Exception as e:
                logger.warning(f"Failed to update DB after unlock: {e}")

        return {"policy": result, "message": "PDF unlocked successfully"}

    except Exception as e:
        err_repr = repr(e).lower()
        if "password" in err_repr:
            return JSONResponse({"error": "Wrong password. Please try again."}, status_code=401)
        logger.error(f"Unlock failed: {e}", exc_info=True)
        return JSONResponse({"error": f"Failed to read PDF: {str(e)}"}, status_code=500)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
