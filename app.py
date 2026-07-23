import os
import json
import asyncio
import logging
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
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

BASIC_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
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


def _redirect_uri():
    base = os.getenv("BASE_URL", "http://localhost:8080")
    return f"{base}/auth/callback"


def create_oauth_flow(scopes=None) -> Flow:
    client_config = {
        "web": {
            "client_id": os.getenv("GOOGLE_CLIENT_ID"),
            "client_secret": os.getenv("GOOGLE_CLIENT_SECRET"),
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [_redirect_uri()],
        }
    }
    flow = Flow.from_client_config(client_config, scopes=scopes or BASIC_SCOPES)
    flow.redirect_uri = _redirect_uri()
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
    flow = create_oauth_flow(BASIC_SCOPES)
    authorization_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    request.session["oauth_state"] = state
    request.session["code_verifier"] = flow.code_verifier
    request.session["oauth_scopes"] = "basic"
    return RedirectResponse(authorization_url)


@app.get("/auth/callback")
async def callback(request: Request):
    import os as _os
    _os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"
    # Recreate flow with the same scopes used for the request
    requested_scopes = BASIC_SCOPES
    flow = create_oauth_flow(requested_scopes)
    # Restore PKCE code_verifier from session
    flow.code_verifier = request.session.get("code_verifier")
    # Fly proxy terminates TLS, so request.url is http:// but redirect_uri is https://
    callback_url = str(request.url)
    if callback_url.startswith("http://") and _redirect_uri().startswith("https://"):
        callback_url = callback_url.replace("http://", "https://", 1)
    flow.fetch_token(authorization_response=callback_url)
    credentials = flow.credentials

    # Get user info
    oauth2 = google_build("oauth2", "v2", credentials=credentials)
    user_info = oauth2.userinfo().get().execute()
    user_email = user_info["email"]
    user_name = user_info.get("name", user_email)

    # Save token per user (local file + DB for persistence across restarts)
    token_json = credentials.to_json()
    token_path = TOKENS_DIR / f"{user_email}.json"
    with open(token_path, "w") as f:
        f.write(token_json)

    # Persist to Turso DB so token survives machine restarts
    if turso_db._client is not None:
        try:
            await db_service.get_or_create_user(user_email, user_name)
            await db_service.save_google_token(user_email, token_json)
        except Exception as e:
            logger.warning(f"Failed to save token to DB: {e}")

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
        "has_gmail": False,
    }


class PolicyRequest(BaseModel):
    vault_key: str = ""

@app.post("/api/policies")
async def get_policies(request: Request, body: PolicyRequest):
    vault_key = body.vault_key
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

    # Fall back to Turso DB (persists across machine restarts)
    if turso_db._client is not None:
        try:
            user_id = await db_service.get_or_create_user(email)
            # Check if user has stored data but didn't provide vault key
            user = await db_service.get_user(user_id)
            if user and user["vault_hash"] and not vault_key:
                return JSONResponse({"need_vault_key": True}, status_code=200)
            # Don't verify (and accidentally set) vault key if it's empty
            if not vault_key:
                return JSONResponse({"policies": [], "fetched_at": None, "from_cache": True}, status_code=200)
            vault_key_derived = await db_service.verify_vault_key(user_id, vault_key)
            policies = await db_service.load_final_policies(user_id, vault_key_derived)
            if policies:
                cache.set(email, policies)  # warm up in-memory cache
                return {
                    "policies": policies,
                    "fetched_at": None,
                    "from_cache": True,
                }
        except ValueError as e:
            if "Wrong vault key" in str(e):
                return JSONResponse({"error": "Wrong vault key", "wrong_key": True}, status_code=200)
            logger.warning(f"Failed to load policies from DB: {e}")
        except Exception as e:
            logger.warning(f"Failed to load policies from DB: {e}")

    return JSONResponse({"error": "No cached data"}, status_code=404)


@app.post("/api/policies/refresh")
async def refresh_policies(request: Request):
    # Note: Retained for future use. Currently frontend uses direct PDF uploads.
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
    data["ts"] = datetime.now().strftime("%H:%M:%S")
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


def sse_keepalive() -> str:
    return ": keepalive\n\n"


@app.get("/api/policies/refresh-stream")
async def refresh_stream(request: Request, vault_key: str = "", force: bool = False):
    # Note: Retained for future use. Currently frontend uses direct PDF uploads.
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

                    # Force refresh: clear ALL triage + extraction data so everything re-runs
                    if force:
                        await turso_db.execute(
                            "DELETE FROM processed_emails WHERE user_id = ?",
                            [user_id],
                        )
                        logger.info(f"Force refresh: cleared all triage and extraction data for {email}")
                        known_msg_ids = set()
                        cached_extractions = []
                        extracted_msg_ids = set()
                    else:
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

            # Restore token file from DB if missing (e.g. after machine restart)
            token_path = TOKENS_DIR / f"{email}.json"
            if not token_path.exists() and turso_db._client is not None:
                try:
                    token_json = await db_service.get_google_token(email)
                    if token_json:
                        with open(token_path, "w") as f:
                            f.write(token_json)
                        logger.info(f"Restored token from DB for {email}")
                    else:
                        yield sse_event("error_event", {
                            "message": "No credentials found. Please re-authenticate.",
                        })
                        return
                except Exception as e:
                    logger.warning(f"Failed to restore token from DB: {e}")

            # Phase 0: Gmail metadata fetch
            import time as _time
            pipeline_start = _time.time()

            yield sse_event("progress", {
                "stage": "gmail", "pct": 0,
                "message": "Scanning your inbox...",
            })

            t0 = _time.time()
            gmail = GmailService(email)
            metadata = await asyncio.to_thread(gmail.fetch_email_metadata)
            gmail_elapsed = _time.time() - t0
            logger.info(f"[Timing] GMAIL FETCH: {gmail_elapsed:.2f}s — {len(metadata)} emails")

            yield sse_event("stage_complete", {
                "stage": "gmail", "total": len(metadata),
                "message": f"Found {len(metadata)} emails to review",
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
            # Use Modal workers if available, fall back to local
            raw_policies = []
            use_modal = False
            try:
                import modal  # noqa: F401
                use_modal = True
            except ImportError:
                pass

            if use_modal:
                # Read token JSON for Modal
                token_path = TOKENS_DIR / f"{email}.json"
                token_json_str = token_path.read_text()
                async for event in pipeline.extract_modal(
                    token_json_str,
                    email,
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
            else:
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
                "message": "Organizing your policies...",
            })

            # cached_extractions already loaded during DB setup phase above
            if not cached_extractions:
                logger.info("No cached extractions available")

            # Also load previously saved final policies (includes unlocked PDFs)
            # so that unlocked data isn't lost when re-encountering locked PDFs
            prev_final = []
            if vault_key_derived is not None and user_id is not None:
                try:
                    prev_final = await db_service.load_final_policies(user_id, vault_key_derived)
                    if prev_final:
                        logger.info(f"Loaded {len(prev_final)} previous final policies for merge")
                except Exception as e:
                    logger.warning(f"Failed to load previous final policies: {e}")

            all_existing = list(cached_extractions) + list(prev_final or [])
            logger.info(f"Finalizing {len(raw_policies)} new + {len(all_existing)} existing")
            final_policies = await pipeline.finalize(
                raw_policies, existing_policies=all_existing
            )
            logger.info(f"Finalized: {len(final_policies)} policies")

            # Save final policies to DB (encrypted)
            if vault_key_derived is not None and user_id is not None:
                try:
                    t0 = _time.time()
                    await db_service.save_final_policies(
                        user_id, final_policies, vault_key_derived
                    )
                    logger.info(f"[Timing] DB save final policies: {_time.time() - t0:.2f}s")
                except Exception as e:
                    logger.warning(f"Failed to save final policies to DB: {e}")

            cache.set(email, final_policies)
            total_elapsed = _time.time() - pipeline_start
            logger.info(f"[Timing] PIPELINE TOTAL: {total_elapsed:.2f}s — {len(final_policies)} policies")
            yield sse_event("done", {
                "policies": final_policies,
                "fetched_at": datetime.now().isoformat(),
                "elapsed": round(total_elapsed, 1),
            })

        except Exception as e:
            logger.error(f"Pipeline failed: {e}", exc_info=True)
            yield sse_event("error_event", {"message": str(e)})

    async def with_keepalive(gen):
        """Wrap an SSE generator with periodic keepalive comments to prevent proxy timeouts."""
        queue = asyncio.Queue()
        done = False

        async def producer():
            nonlocal done
            try:
                async for item in gen:
                    await queue.put(item)
            finally:
                done = True
                await queue.put(None)  # sentinel

        task = asyncio.create_task(producer())
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=10)
                    if item is None:
                        break
                    yield item
                except asyncio.TimeoutError:
                    yield sse_keepalive()
        finally:
            task.cancel()

    return StreamingResponse(
        with_keepalive(event_generator()),
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
    vault_key = body.get("vault_key", "")
    msg_id = body.get("msg_id", "")

    if not password:
        return JSONResponse({"error": "Missing password"}, status_code=400)

    import fitz  # PyMuPDF
    attachments_dir = str(BASE_DIR / "attachments")

    # If PDF file doesn't exist locally, re-download from Gmail using msg_id
    if (not pdf_path or not Path(pdf_path).exists()) and msg_id:
        try:
            gmail = GmailService(email)
            downloaded = gmail.redownload_attachment(msg_id)
            if downloaded:
                pdf_path = downloaded[0]  # Use the first PDF
                logger.info(f"Re-downloaded PDF for unlock: {pdf_path}")
            else:
                return JSONResponse({"error": "Could not re-download PDF from Gmail. Try refreshing from Gmail first."}, status_code=404)
        except Exception as e:
            logger.warning(f"Failed to re-download PDF for unlock: {e}")
            return JSONResponse({"error": "Could not re-download PDF from Gmail. Try refreshing from Gmail first."}, status_code=404)

    if not pdf_path:
        return JSONResponse({"error": "PDF file not found"}, status_code=404)

    # Security: ensure the path is within the attachments directory
    resolved = str(Path(pdf_path).resolve())
    if not resolved.startswith(attachments_dir):
        return JSONResponse({"error": "Invalid path"}, status_code=403)

    if not Path(pdf_path).exists():
        return JSONResponse({"error": "PDF file not found"}, status_code=404)

    # Try opening with password
    try:
        text = ""
        doc = fitz.open(pdf_path)
        if doc.is_encrypted:
            if not doc.authenticate(password):
                doc.close()
                return JSONResponse({"error": "Wrong password. Please try again."}, status_code=401)
        for page in doc:
            page_text = page.get_text()
            if page_text:
                text += page_text + "\n"
        doc.close()

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

        # Sanitize dates
        for d_field in ["policy_start", "policy_end"]:
            d_val = result.get(d_field)
            if d_val and isinstance(d_val, str):
                d_val = d_val.strip()
                result[d_field] = d_val  # update with stripped
                from datetime import datetime as dt
                try:
                    dt.strptime(d_val, "%Y-%m-%d")
                except (ValueError, TypeError):
                    for fmt in ("%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%m-%d-%Y", "%d %b %Y", "%d %B %Y"):
                        try:
                            parsed = dt.strptime(d_val, fmt)
                            result[d_field] = parsed.strftime("%Y-%m-%d")
                            break
                        except ValueError:
                            pass

        # Calculate end date if term is provided but end date is missing
        if not result.get("policy_end") and result.get("policy_start") and result.get("policy_term"):
            try:
                from datetime import datetime as dt
                s_date = dt.strptime(result["policy_start"], "%Y-%m-%d").date()
                term_years = int(result["policy_term"])
                try:
                    e_date = s_date.replace(year=s_date.year + term_years)
                except ValueError:
                    e_date = s_date.replace(year=s_date.year + term_years, month=2, day=28)
                result["policy_end"] = e_date.strftime("%Y-%m-%d")
            except Exception:
                pass

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


@app.post("/api/policies/upload")
async def upload_pdf(
    request: Request,
    file: UploadFile = File(...),
    password: str = Form(""),
    vault_key: str = Form(""),
):
    """Upload a PDF policy document manually."""
    email = request.session.get("user_email")
    if not email:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    if not file.filename or not file.filename.lower().endswith(".pdf"):
        return JSONResponse({"error": "Only PDF files are accepted"}, status_code=400)

    import fitz  # PyMuPDF

    # Save uploaded file
    upload_dir = BASE_DIR / "attachments" / email / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)

    safe_name = "".join(c if c.isalnum() or c in "._- " else "_" for c in file.filename)
    save_path = upload_dir / safe_name
    # Deconflict filename
    counter = 1
    while save_path.exists():
        stem = Path(safe_name).stem
        suffix = Path(safe_name).suffix
        save_path = upload_dir / f"{stem}_{counter}{suffix}"
        counter += 1

    content = await file.read()
    save_path.write_bytes(content)
    logger.info(f"Uploaded PDF saved: {save_path} ({len(content)} bytes)")

    try:
        doc = fitz.open(str(save_path))

        if doc.is_encrypted:
            if not password:
                doc.close()
                return {"needs_password": True, "filename": save_path.name}
            if not doc.authenticate(password):
                doc.close()
                return JSONResponse({"error": "Wrong password. Please try again."}, status_code=401)

        text = ""
        for page in doc:
            page_text = page.get_text()
            if page_text:
                text += page_text + "\n"
        doc.close()

        if not text or len(text.strip()) < 50:
            return JSONResponse({"error": "No readable text found in PDF"}, status_code=422)

        # Extract policy via LLM
        pipeline = PipelineService()
        extract_doc = {
            "pdf_filename": file.filename,
            "email_subject": "Manual upload",
            "pdf_text": text,
        }
        result = await pipeline._grok_extract(extract_doc)

        if not result:
            return JSONResponse({"error": "Could not extract policy details from this PDF"}, status_code=422)

        # Sanitize dates
        for d_field in ["policy_start", "policy_end"]:
            d_val = result.get(d_field)
            if d_val and isinstance(d_val, str):
                d_val = d_val.strip()
                result[d_field] = d_val
                try:
                    datetime.strptime(d_val, "%Y-%m-%d")
                except (ValueError, TypeError):
                    for fmt in ("%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%m-%d-%Y", "%d %b %Y", "%d %B %Y"):
                        try:
                            parsed = datetime.strptime(d_val, fmt)
                            result[d_field] = parsed.strftime("%Y-%m-%d")
                            break
                        except ValueError:
                            pass

        # Calculate end date if term is provided but end date is missing
        if not result.get("policy_end") and result.get("policy_start") and result.get("policy_term"):
            try:
                from datetime import datetime as dt
                s_date = dt.strptime(result["policy_start"], "%Y-%m-%d").date()
                term_years = int(result["policy_term"])
                try:
                    e_date = s_date.replace(year=s_date.year + term_years)
                except ValueError:
                    e_date = s_date.replace(year=s_date.year + term_years, month=2, day=28)
                result["policy_end"] = e_date.strftime("%Y-%m-%d")
            except Exception:
                pass

        # Fix status based on today's date
        end = result.get("policy_end")
        if end:
            try:
                end_date = datetime.strptime(end, "%Y-%m-%d").date()
                result["status"] = "ACTIVE" if end_date >= datetime.now().date() else "EXPIRED"
            except (ValueError, TypeError):
                pass
                
        if password:
            result["pdf_password"] = password

        # Dedup with existing policies
        cached = cache.get(email)
        existing = cached["policies"] if cached else []
        merged = await pipeline.finalize([result], existing)
        cache.set(email, merged)

        # Update DB
        user_name = request.session.get("user_name", email)
        if turso_db._client is not None:
            try:
                user_id = await db_service.get_or_create_user(email, user_name)
                vault_key_derived = await db_service.verify_vault_key(user_id, vault_key)
                await db_service.save_final_policies(user_id, merged, vault_key_derived)
            except Exception as e:
                logger.warning(f"Failed to update DB after upload: {e}")

        return {"policy": result, "policies": merged}

    except Exception as e:
        err_repr = repr(e).lower()
        if "password" in err_repr:
            return JSONResponse({"error": "Wrong password. Please try again."}, status_code=401)
        logger.error(f"Upload processing failed: {e}", exc_info=True)
        return JSONResponse({"error": f"Failed to process PDF: {str(e)}"}, status_code=500)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
