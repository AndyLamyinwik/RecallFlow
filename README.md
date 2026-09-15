# RecallFlow

RecallFlow is a small, non-AI web app that brings one saved article back on a transparent future date. The user can then choose **Open**, **Later**, or **Dismiss** without being shown an overdue queue or pressured to clear a backlog.

This repository is a closed-pilot V1. Its product rules and extraction thresholds are provisional; it does not claim validated retention, learning, or AI results.

## What the current V1 does

- Uses an invitation code and signed session for closed-pilot access.
- Saves public article URLs after SSRF-aware URL and network checks.
- Extracts a title and article text when possible, estimates reading effort with a deterministic `v1` rule, and falls back gracefully when extraction is unavailable.
- Shows a private saved-item list and detail page, with revisit-date editing and permanent deletion.
- Selects at most one due article per user-local day using a deterministic ordering rule. An impression is recorded only after the browser confirms that the card mounted.
- Supports **Open**, **Later**, and **Dismiss** with idempotent state transitions.
- Offers **Undo** for 30 seconds after Dismiss. A valid same-user Undo restores the item and its retained schedule; when the original card context is still current, it restores that card without creating a second impression.
- Supports a user-wide **Pause resurfacing** control. Pause hides/closes any active card but preserves every saved item and revisit date. Resume clears the pause without rewriting dates or creating a duplicate card for an already-used daily slot.
- Records the non-AI product events needed to audit the primary flow.

`source_opened` has a deliberately narrow meaning: RecallFlow completed a fresh safe-reachability check and issued an external navigation handoff. It does **not** mean the browser rendered the article or that the user read it.

## Not implemented

The current V1 does **not** include:

- AI Value Line or any model call
- Recall generation or a Recall interaction
- deployment configuration or a hosted release
- claims that the product hypotheses or `v1` thresholds have been validated

`OPENAI_API_KEY` and `LLM_MODEL` remain reserved configuration fields and are not required by the current non-AI app. The architecture documents describe both implemented V1 behavior and possible later phases; planned AI/Recall sections are not current product functionality.

## Local setup

Create and activate a virtual environment, then install the dependencies:

```bash
python -m venv .venv
python -m pip install -r requirements.txt
```

Copy `.env.example` to `.env`, then configure:

- `APP_SECRET` — a long random secret; use at least 32 characters outside development.
- `ACCESS_CODES` — one or more comma-separated, high-entropy pilot invitation codes (16–256 characters each).
- `DATABASE_URL` — defaults to the local SQLite file `data/recallflow.db`.
- `DISMISS_UNDO_WINDOW_SECONDS` — keep the V1 value at `30`.

An invitation code is converted to a salted scrypt hash when it is first used. The raw code is never stored in the database or session cookie. Never commit `.env`, invitation codes, secrets, or the local SQLite database; the supplied `.gitignore` excludes them.

## Run locally

```bash
uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000`. The health endpoint is `http://127.0.0.1:8000/health`.

Authenticated pages:

- `/items` — pause/resume control, the single due card when available, and saved items
- `/items/new` — public article capture
- `/items/{id}` — private item details, revisit-date control, and permanent deletion

## Run tests

```bash
python -m pytest
```

## Security and privacy boundaries

- Capture accepts public HTTP(S) article targets only and applies address, redirect, response-type, size, and timeout limits.
- A card's Open action performs a fresh bounded reachability check, including public-address, redirect-hop, connected-peer, and cross-user cookie-isolation safeguards, before navigation handoff.
- Adequate, untruncated extracted article text may be stored privately up to 50,000 characters. Longer or inadequate extractions retain only the limited fallback data defined by the V1 extractor. Stored source text is not rendered publicly or committed to this repository.
- Permanent item deletion removes its URL, private source snapshot, note, and item-linked records.
- State-changing browser requests use CSRF protection and ownership checks; resurfacing decisions also use idempotency keys. The 30-second Dismiss Undo reference is signed, same-user scoped, and kept in a short-lived HttpOnly cookie rather than a URL.
- Production/staging mode enables secure-cookie settings, but this repository does not yet provide deployment configuration.

## Current limitations

- The default HTTP transport validates the connected peer after connection rather than pinning the connection to the prevalidated DNS address. Strict DNS-rebinding prevention remains deployment hardening work.
- Closed-pilot invitation codes are intentionally a narrow access boundary, not a broad-release authentication system.
- SQLite is intentionally limited to a single application instance.
- There is no AI, Recall, evaluation export, deployment setup, or hosted environment in this release.
- Product hypotheses remain unvalidated until a later evaluation phase.
