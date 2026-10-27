"""Server-rendered Saved Item UI for the closed pilot."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import hmac
import json
from math import ceil
from typing import Annotated
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import (
    ActionActor,
    ActionEvent,
    ActionEventName,
    Intent,
    ResurfacingEvent,
    ResurfacingState,
    SavedItem,
    SavedItemStatus,
    ScheduleKind,
    User,
)
from app.repositories import EventRepository, SavedItemRepository, UserRepository
from app.schemas import CaptureInput, RevisitDateInput
from app.security import (
    DismissUndoClaims,
    DismissUndoTokenManager,
    InvalidPilotTimezone,
    PilotAccessService,
    SessionClaims,
    SessionManager,
)
from app.services import (
    CaptureRejected,
    CaptureService,
    ImpressionMountStatus,
    ActionErrorCode,
    ActionRejected,
    LaterChoice,
    OpenActionStatus,
    ResurfacingActionService,
    ResurfacingEngine,
)
from app.services.clock import Clock, SystemClock


SessionFactory = Callable[[], Session]


@dataclass(frozen=True)
class SavedItemView:
    """Template-safe projection that deliberately excludes private source text."""

    id: str
    source_url: str
    title: str
    source_domain: str
    source_excerpt: str | None
    saved_at: datetime
    intent: str
    why_saved_note: str | None
    effort_band: str
    extraction_status: str
    status: str
    schedule_kind: str
    next_due_on: date | None
    updated_at: datetime


@dataclass(frozen=True)
class ResurfacingCardView:
    """A deliberately narrow projection for the public-facing card."""

    event_id: str
    item_id: str
    title: str
    source_domain: str
    context_label: str | None
    context_text: str | None
    effort_band: str
    saved_age: str
    scheduled_on: date
    due_timing: str
    plus_3_date: date
    plus_7_date: date
    custom_minimum_date: date
    actions_enabled: bool


def _item_view(item: SavedItem) -> SavedItemView:
    return SavedItemView(
        id=item.id,
        source_url=item.source_url,
        title=item.title,
        source_domain=item.source_domain,
        source_excerpt=item.source_excerpt,
        saved_at=item.saved_at,
        intent=item.intent.value,
        why_saved_note=item.why_saved_note,
        effort_band=item.effort_band.value,
        extraction_status=item.extraction_status.value,
        status=item.status.value,
        schedule_kind=item.schedule_kind.value,
        next_due_on=item.next_due_on,
        updated_at=item.updated_at,
    )


def _resurfacing_card_view(
    event: ResurfacingEvent,
    item: SavedItem,
    *,
    local_today: date,
    timezone_name: str,
    actions_enabled: bool = False,
) -> ResurfacingCardView:
    if item.why_saved_note:
        context_label = "Your note"
        context_text = item.why_saved_note
    elif item.source_excerpt:
        context_label = "Source excerpt"
        context_text = item.source_excerpt
    else:
        context_label = None
        context_text = None

    saved_local_date = item.saved_at.astimezone(ZoneInfo(timezone_name)).date()
    saved_days = max(0, (local_today - saved_local_date).days)
    saved_age = "Saved today" if saved_days == 0 else f"Saved {saved_days} days ago"
    timing = "scheduled for today" if event.scheduled_on_snapshot == local_today else "ready now"
    return ResurfacingCardView(
        event_id=event.id,
        item_id=item.id,
        title=item.title,
        source_domain=item.source_domain,
        context_label=context_label,
        context_text=context_text,
        effort_band=item.effort_band.value,
        saved_age=saved_age,
        scheduled_on=event.scheduled_on_snapshot,
        due_timing=timing,
        plus_3_date=local_today + timedelta(days=3),
        plus_7_date=local_today + timedelta(days=7),
        custom_minimum_date=local_today + timedelta(days=1),
        actions_enabled=actions_enabled,
    )


def create_web_router(
    *,
    settings: Settings,
    session_factory: SessionFactory,
    capture_service: CaptureService,
    templates: Jinja2Templates,
    clock: Clock | None = None,
    resurfacing_engine: ResurfacingEngine | None = None,
    action_service: ResurfacingActionService | None = None,
) -> APIRouter:
    router = APIRouter()
    selected_clock = clock or SystemClock()
    resurfacing = resurfacing_engine or ResurfacingEngine(clock=selected_clock)
    actions = action_service or ResurfacingActionService(clock=selected_clock)
    session_manager = SessionManager(
        settings.app_secret,
        session_max_age_seconds=settings.pilot_session_max_age_seconds,
        login_csrf_max_age_seconds=settings.pilot_login_csrf_max_age_seconds,
    )
    dismiss_undo_tokens = DismissUndoTokenManager(settings.app_secret)
    access_service = PilotAccessService(
        settings.pilot_access_codes,
        fingerprint_secret=settings.app_secret,
    )
    users = UserRepository()
    items = SavedItemRepository()
    events = EventRepository()

    def current_utc() -> datetime:
        now = selected_clock.now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now.astimezone(timezone.utc)

    def local_today(user: User) -> date:
        now = selected_clock.now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        try:
            return now.astimezone(ZoneInfo(user.timezone)).date()
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=500, detail="Pilot timezone is invalid") from exc

    def signed_in(request: Request, database: Session) -> tuple[SessionClaims, User] | None:
        claims = session_manager.read_session(
            request.cookies.get(session_manager.session_cookie_name)
        )
        if claims is None:
            return None
        user = users.get(database, claims.user_id)
        if user is None:
            return None
        return claims, user

    def login_redirect() -> RedirectResponse:
        response = RedirectResponse(url="/", status_code=303)
        response.delete_cookie(session_manager.session_cookie_name, path="/")
        response.delete_cookie(dismiss_undo_tokens.cookie_name, path="/")
        return response

    def require_csrf(claims: SessionClaims, token: str) -> None:
        if not session_manager.valid_csrf(claims, token):
            raise HTTPException(status_code=403, detail="Invalid CSRF token")

    def render_login(request: Request, *, error: str | None = None, status_code: int = 200) -> Response:
        token = session_manager.new_login_csrf()
        response = templates.TemplateResponse(
            request=request,
            name="home.html",
            context={"login_csrf": token, "error": error},
            status_code=status_code,
        )
        response.set_cookie(
            session_manager.login_csrf_cookie_name,
            token,
            max_age=settings.pilot_login_csrf_max_age_seconds,
            httponly=True,
            secure=settings.secure_cookies,
            samesite="lax",
            path="/",
        )
        return response

    def authenticated_context(claims: SessionClaims) -> dict[str, object]:
        return {"signed_in": True, "csrf_token": session_manager.csrf_token(claims)}

    @router.get("/", response_class=HTMLResponse)
    def home(request: Request) -> Response:
        with session_factory() as database:
            if signed_in(request, database) is not None:
                return RedirectResponse(url="/items", status_code=303)
        return render_login(request)

    @router.post("/session")
    def create_session(
        request: Request,
        access_code: Annotated[str, Form()],
        timezone_name: Annotated[str, Form()] = "UTC",
        csrf_token: Annotated[str, Form()] = "",
    ) -> Response:
        login_cookie = request.cookies.get(session_manager.login_csrf_cookie_name)
        if not session_manager.valid_login_csrf(csrf_token, login_cookie):
            raise HTTPException(status_code=403, detail="Invalid CSRF token")

        with session_factory() as database:
            try:
                user = access_service.authenticate_or_provision(
                    database,
                    access_code=access_code,
                    timezone_name=timezone_name,
                )
            except InvalidPilotTimezone:
                database.rollback()
                return render_login(
                    request,
                    error="We could not detect a valid timezone. Please try again.",
                    status_code=422,
                )
            if user is None:
                database.rollback()
                return render_login(
                    request,
                    error="That access code could not be used.",
                    status_code=401,
                )
            database.commit()
            session_token, _ = session_manager.create_session(user.id)

        response = RedirectResponse(url="/items", status_code=303)
        response.set_cookie(
            session_manager.session_cookie_name,
            session_token,
            max_age=settings.pilot_session_max_age_seconds,
            httponly=True,
            secure=settings.secure_cookies,
            samesite="lax",
            path="/",
        )
        response.delete_cookie(session_manager.login_csrf_cookie_name, path="/")
        response.delete_cookie(dismiss_undo_tokens.cookie_name, path="/")
        return response

    @router.post("/session/logout")
    def logout(request: Request, csrf_token: Annotated[str, Form()] = "") -> Response:
        with session_factory() as database:
            principal = signed_in(request, database)
            if principal is None:
                return login_redirect()
            claims, _ = principal
            require_csrf(claims, csrf_token)
        return login_redirect()

    @router.get("/items", response_class=HTMLResponse)
    def list_items(
        request: Request,
        notice: str | None = None,
        event_id: str | None = None,
    ) -> Response:
        with session_factory() as database:
            principal = signed_in(request, database)
            if principal is None:
                return login_redirect()
            claims, user = principal
            selection = resurfacing.select_for_today(
                database,
                user_id=claims.user_id,
            )
            resurfacing_paused = user.resurfacing_paused_at is not None
            resurfacing_card = None
            if (
                not resurfacing_paused
                and selection.event is not None
                and selection.event.state is ResurfacingState.PENDING
            ):
                selected_item = items.get(
                    database,
                    user_id=claims.user_id,
                    item_id=selection.event.saved_item_id,
                )
                if selected_item is not None:
                    resurfacing_card = _resurfacing_card_view(
                        selection.event,
                        selected_item,
                        local_today=selection.local_today,
                        timezone_name=user.timezone,
                    )
            elif not resurfacing_paused:
                # The daily limit concerns selecting a NEW card. Keep today's
                # already-shown, unresolved card actionable across navigation or
                # reload without another mount or impression event.
                retry_event = events.get_daily_event(
                    database,
                    user_id=claims.user_id,
                    local_date_slot=selection.local_today,
                )
                if (
                    retry_event is not None
                    and retry_event.state is ResurfacingState.SHOWN
                    and retry_event.local_date_slot == selection.local_today
                ):
                    selected_item = items.get(
                        database,
                        user_id=claims.user_id,
                        item_id=retry_event.saved_item_id,
                    )
                    if (
                        selected_item is not None
                        and selected_item.status is SavedItemStatus.ACTIVE
                    ):
                        resurfacing_card = _resurfacing_card_view(
                            retry_event,
                            selected_item,
                            local_today=selection.local_today,
                            timezone_name=user.timezone,
                            actions_enabled=True,
                        )

            undo_token = request.cookies.get(dismiss_undo_tokens.cookie_name)
            undo_claims = dismiss_undo_tokens.read(
                undo_token,
                now=current_utc(),
            )
            dismiss_undo_event_id = None
            clear_dismiss_undo_cookie = bool(
                undo_token
                and (
                    undo_claims is None
                    or undo_claims.user_id != claims.user_id
                )
            )
            if (
                notice == "dismissed"
                and undo_claims is not None
                and undo_claims.user_id == claims.user_id
            ):
                if actions.dismiss_undo_available(
                    database,
                    user_id=claims.user_id,
                    resurfacing_event_id=undo_claims.event_id,
                    item_id=undo_claims.item_id,
                    dismiss_action_event_id=undo_claims.dismiss_action_event_id,
                    original_session_id=undo_claims.original_session_id,
                    dismissal_local_date=undo_claims.local_date_slot,
                    undo_expires_at=undo_claims.expires_at,
                ):
                    dismiss_undo_event_id = undo_claims.event_id
                else:
                    clear_dismiss_undo_cookie = True
            item_views = [_item_view(item) for item in items.list_for_user(database, user_id=claims.user_id)]
            database.commit()
        if notice == "open-failed":
            notice_message = (
                "The source could not be reached safely. The card is still actionable."
                if resurfacing_card is not None
                else "The earlier Open attempt did not complete. Its retry did not change the item's current state."
            )
        else:
            notice_message = {
                "deleted": "The saved item and its private snapshot were permanently deleted.",
                "dismissed": "Dismissed. This item will no longer resurface.",
                "dismiss-undone": (
                    "Dismiss undone. The card is ready again."
                    if resurfacing_card is not None
                    else "Dismiss undone. The item is active again."
                ),
                "later": "The item was rescheduled to the date shown below.",
                "paused": "Resurfacing is paused. Saved dates were not changed.",
                "resumed": "Resurfacing is active again.",
            }.get(notice)

        context = authenticated_context(claims)
        context.update(
            {
                "items": item_views,
                "resurfacing_card": resurfacing_card,
                "resurfacing_paused": resurfacing_paused,
                "dismiss_undo_event_id": dismiss_undo_event_id,
                "notice": notice_message,
                "notice_is_error": notice == "open-failed",
            }
        )
        response = templates.TemplateResponse(
            request=request,
            name="items.html",
            context=context,
        )
        response.headers["Cache-Control"] = "no-store"
        if clear_dismiss_undo_cookie:
            response.delete_cookie(dismiss_undo_tokens.cookie_name, path="/")
        return response

    @router.post("/resurfacing/{event_id}/impression", status_code=204)
    def acknowledge_resurfacing_impression(
        request: Request,
        event_id: str,
        csrf_token: Annotated[str, Form()] = "",
    ) -> Response:
        with session_factory() as database:
            principal = signed_in(request, database)
            if principal is None:
                return Response(status_code=401, headers={"Cache-Control": "no-store"})
            claims, _ = principal
            require_csrf(claims, csrf_token)
            result = resurfacing.acknowledge_mount(
                database,
                user_id=claims.user_id,
                resurfacing_event_id=event_id,
                session_id=claims.session_id,
            )
            if result.status is ImpressionMountStatus.REJECTED:
                database.rollback()
                raise HTTPException(status_code=404, detail="Resurfacing card not found")
            database.commit()
        return Response(status_code=204, headers={"Cache-Control": "no-store"})

    def action_error(exc: ActionRejected) -> None:
        if exc.code is ActionErrorCode.NOT_FOUND:
            raise HTTPException(status_code=404, detail="Resurfacing card not found")
        if exc.code in {
            ActionErrorCode.INVALID_DATE,
            ActionErrorCode.INVALID_IDEMPOTENCY_KEY,
        }:
            raise HTTPException(status_code=422, detail=str(exc))
        raise HTTPException(status_code=409, detail=str(exc))

    @router.post("/resurfacing/{event_id}/later")
    def later(
        request: Request,
        event_id: str,
        choice: Annotated[str, Form()],
        idempotency_key: Annotated[str, Form()],
        custom_date: Annotated[str, Form()] = "",
        csrf_token: Annotated[str, Form()] = "",
    ) -> Response:
        with session_factory() as database:
            principal = signed_in(request, database)
            if principal is None:
                return login_redirect()
            claims, _ = principal
            require_csrf(claims, csrf_token)
            parsed_custom_date = None
            if choice == LaterChoice.CUSTOM.value:
                try:
                    parsed_custom_date = date.fromisoformat(custom_date)
                except ValueError:
                    raise HTTPException(status_code=422, detail="Choose a valid future date")
            try:
                result = actions.later(
                    database,
                    user_id=claims.user_id,
                    resurfacing_event_id=event_id,
                    session_id=claims.session_id,
                    idempotency_key=idempotency_key,
                    choice=choice,
                    custom_date=parsed_custom_date,
                )
                database.commit()
            except ActionRejected as exc:
                database.rollback()
                action_error(exc)
        return RedirectResponse(
            url="/items?notice=later",
            status_code=303,
        )

    @router.post("/resurfacing/{event_id}/dismiss")
    def dismiss(
        request: Request,
        event_id: str,
        idempotency_key: Annotated[str, Form()],
        csrf_token: Annotated[str, Form()] = "",
    ) -> Response:
        with session_factory() as database:
            principal = signed_in(request, database)
            if principal is None:
                return login_redirect()
            claims, _ = principal
            require_csrf(claims, csrf_token)
            try:
                result = actions.dismiss(
                    database,
                    user_id=claims.user_id,
                    resurfacing_event_id=event_id,
                    session_id=claims.session_id,
                    idempotency_key=idempotency_key,
                )
                database.commit()
            except ActionRejected as exc:
                database.rollback()
                action_error(exc)
        response = RedirectResponse(url="/items?notice=dismissed", status_code=303)
        seconds_remaining = ceil(
            (result.undo_expires_at - current_utc()).total_seconds()
        )
        if seconds_remaining > 0:
            response.set_cookie(
                dismiss_undo_tokens.cookie_name,
                dismiss_undo_tokens.issue(
                    DismissUndoClaims(
                        user_id=claims.user_id,
                        event_id=event_id,
                        item_id=result.item_id,
                        dismiss_action_event_id=result.dismiss_action_event_id,
                        original_session_id=result.original_session_id,
                        local_date_slot=result.local_date_slot,
                        expires_at=result.undo_expires_at,
                    )
                ),
                max_age=seconds_remaining,
                httponly=True,
                secure=settings.secure_cookies,
                samesite="lax",
                path="/",
            )
        else:
            response.delete_cookie(dismiss_undo_tokens.cookie_name, path="/")
        response.headers["Cache-Control"] = "no-store"
        return response

    def dismiss_undo_error() -> Response:
        response = Response(
            content="This Dismiss can no longer be undone.",
            status_code=409,
            media_type="text/plain",
            headers={"Cache-Control": "no-store"},
        )
        response.delete_cookie(dismiss_undo_tokens.cookie_name, path="/")
        return response

    @router.post("/resurfacing/{event_id}/dismiss/undo")
    def undo_dismiss(
        request: Request,
        event_id: str,
        idempotency_key: Annotated[str, Form()],
        csrf_token: Annotated[str, Form()] = "",
    ) -> Response:
        with session_factory() as database:
            principal = signed_in(request, database)
            if principal is None:
                return login_redirect()
            claims, _ = principal
            require_csrf(claims, csrf_token)
            undo_claims = dismiss_undo_tokens.read(
                request.cookies.get(dismiss_undo_tokens.cookie_name),
                now=current_utc(),
            )
            if (
                undo_claims is None
                or undo_claims.user_id != claims.user_id
                or undo_claims.event_id != event_id
            ):
                database.rollback()
                return dismiss_undo_error()
            try:
                actions.undo_dismiss(
                    database,
                    user_id=claims.user_id,
                    resurfacing_event_id=event_id,
                    item_id=undo_claims.item_id,
                    dismiss_action_event_id=undo_claims.dismiss_action_event_id,
                    original_session_id=undo_claims.original_session_id,
                    dismissal_local_date=undo_claims.local_date_slot,
                    undo_expires_at=undo_claims.expires_at,
                    session_id=claims.session_id,
                    idempotency_key=idempotency_key,
                )
                database.commit()
            except ActionRejected:
                database.rollback()
                return dismiss_undo_error()
        response = RedirectResponse(
            url=f"/items?notice=dismiss-undone&event_id={event_id}",
            status_code=303,
        )
        response.delete_cookie(dismiss_undo_tokens.cookie_name, path="/")
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.post("/resurfacing/pause")
    def pause_resurfacing(
        request: Request,
        idempotency_key: Annotated[str, Form()],
        csrf_token: Annotated[str, Form()] = "",
    ) -> Response:
        with session_factory() as database:
            principal = signed_in(request, database)
            if principal is None:
                return login_redirect()
            claims, _ = principal
            require_csrf(claims, csrf_token)
            try:
                actions.pause(
                    database,
                    user_id=claims.user_id,
                    session_id=claims.session_id,
                    idempotency_key=idempotency_key,
                )
                database.commit()
            except ActionRejected as exc:
                database.rollback()
                action_error(exc)
        return RedirectResponse(url="/items?notice=paused", status_code=303)

    @router.post("/resurfacing/resume")
    def resume_resurfacing(
        request: Request,
        idempotency_key: Annotated[str, Form()],
        csrf_token: Annotated[str, Form()] = "",
    ) -> Response:
        with session_factory() as database:
            principal = signed_in(request, database)
            if principal is None:
                return login_redirect()
            claims, _ = principal
            require_csrf(claims, csrf_token)
            try:
                actions.resume(
                    database,
                    user_id=claims.user_id,
                    session_id=claims.session_id,
                    idempotency_key=idempotency_key,
                )
                database.commit()
            except ActionRejected as exc:
                database.rollback()
                action_error(exc)
        return RedirectResponse(url="/items?notice=resumed", status_code=303)

    @router.post("/resurfacing/{event_id}/open")
    def open_source(
        request: Request,
        event_id: str,
        idempotency_key: Annotated[str, Form()],
        csrf_token: Annotated[str, Form()] = "",
    ) -> Response:
        with session_factory() as database:
            principal = signed_in(request, database)
            if principal is None:
                return login_redirect()
            claims, _ = principal
            require_csrf(claims, csrf_token)
        try:
            result = actions.open_source(
                session_factory,
                user_id=claims.user_id,
                resurfacing_event_id=event_id,
                session_id=claims.session_id,
                idempotency_key=idempotency_key,
            )
        except ActionRejected as exc:
            action_error(exc)
        if result.status in {
            OpenActionStatus.FAILED,
            OpenActionStatus.REPLAYED_FAILED,
        }:
            return RedirectResponse(
                url=f"/items?notice=open-failed&event_id={event_id}",
                status_code=303,
            )
        if not result.destination_url:
            raise HTTPException(status_code=409, detail="The source handoff was not available")
        response = RedirectResponse(url=result.destination_url, status_code=303)
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.get("/items/new", response_class=HTMLResponse)
    def new_item(request: Request) -> Response:
        with session_factory() as database:
            principal = signed_in(request, database)
            if principal is None:
                return login_redirect()
            claims, user = principal
            default_date = local_today(user) + timedelta(days=7)
        context = authenticated_context(claims)
        context.update(
            {
                "form": {
                    "url": "",
                    "intent": Intent.READ.value,
                    "revisit_on": default_date.isoformat(),
                    "why_saved_note": "",
                },
                "capture_operation_id": str(uuid4()),
                "error": None,
            }
        )
        return templates.TemplateResponse(request=request, name="new_item.html", context=context)

    @router.post("/items")
    def save_item(
        request: Request,
        url: Annotated[str, Form()],
        intent: Annotated[str, Form()],
        revisit_on: Annotated[str, Form()],
        why_saved_note: Annotated[str, Form()] = "",
        csrf_token: Annotated[str, Form()] = "",
        capture_operation_id: Annotated[str, Form()] = "",
    ) -> Response:
        form = {
            "url": url,
            "intent": intent,
            "revisit_on": revisit_on,
            "why_saved_note": why_saved_note,
        }
        with session_factory() as database:
            principal = signed_in(request, database)
            if principal is None:
                return login_redirect()
            claims, _ = principal
            require_csrf(claims, csrf_token)

            # One admitted form submission is one operation. Legacy clients that
            # omit the field still work, but need a supplied ID for retry replay.
            try:
                if capture_operation_id:
                    if len(capture_operation_id) != 36:
                        raise ValueError
                    operation_id = str(UUID(capture_operation_id))
                else:
                    operation_id = str(uuid4())
            except ValueError as exc:
                raise HTTPException(status_code=422, detail="Invalid capture operation") from exc
            fingerprint = hmac.new(
                settings.app_secret.encode(),
                ("capture-input-v1:" + claims.user_id + ":" + json.dumps(
                    form, sort_keys=True, ensure_ascii=False, separators=(",", ":")
                )).encode(),
                sha256,
            ).hexdigest()
            started_key = f"capture:{operation_id}:started"

            def failed_form() -> Response:
                context = authenticated_context(claims)
                context.update(
                    {
                        "form": form,
                        # An edited resubmission is a new intentional operation.
                        "capture_operation_id": str(uuid4()),
                        "error": "We could not save that article. Check the public URL and revisit date.",
                    }
                )
                return templates.TemplateResponse(
                    request=request, name="new_item.html", context=context, status_code=422
                )

            def replay(started: ActionEvent) -> Response:
                if not hmac.compare_digest(
                    str(started.properties.get("input_fingerprint", "")), fingerprint
                ):
                    raise HTTPException(status_code=409, detail="Capture operation was reused with different input")
                terminal = database.scalar(
                    select(ActionEvent).where(
                        ActionEvent.user_id == claims.user_id,
                        ActionEvent.correlation_id == operation_id,
                        ActionEvent.event_name.in_((
                            ActionEventName.CAPTURE_SUCCEEDED, ActionEventName.CAPTURE_FAILED
                        )),
                    )
                )
                if terminal is None:
                    raise HTTPException(
                        status_code=409,
                        detail="Capture is in progress or incomplete; open a new form to try again",
                    )
                if terminal.event_name == ActionEventName.CAPTURE_FAILED:
                    return failed_form()
                if terminal.item_id is None or items.get(
                    database, user_id=claims.user_id, item_id=terminal.item_id
                ) is None:
                    raise HTTPException(status_code=409, detail="Saved result is no longer available")
                notice = "saved" if terminal.properties.get("created") else "duplicate"
                return RedirectResponse(
                    url=f"/items/{terminal.item_id}?notice={notice}", status_code=303
                )

            existing = database.scalar(select(ActionEvent).where(
                ActionEvent.user_id == claims.user_id,
                ActionEvent.idempotency_key == started_key,
            ))
            if existing is not None:
                return replay(existing)
            # Commit the unique claim before network/extraction work. Concurrent
            # retries cannot perform capture twice; a crash remains started-only
            # rather than being reported as a known failure or success.
            database.add(ActionEvent(
                event_name=ActionEventName.CAPTURE_STARTED,
                user_id=claims.user_id,
                session_id=claims.session_id,
                correlation_id=operation_id,
                idempotency_key=started_key,
                actor=ActionActor.USER,
                occurred_at=current_utc(),
                properties={"input_fingerprint": fingerprint, "telemetry_version": 1},
            ))
            try:
                database.commit()
            except IntegrityError:
                database.rollback()
                claimed = database.scalar(select(ActionEvent).where(
                    ActionEvent.user_id == claims.user_id,
                    ActionEvent.idempotency_key == started_key,
                ))
                if claimed is None:
                    raise
                return replay(claimed)
            # Python's SQLite legacy transaction mode does not emit BEGIN for
            # SELECT/SAVEPOINT. Start the real outer transaction explicitly so
            # repository savepoints cannot commit an item independently of its
            # terminal outcome; rollback must remove both together.
            if database.get_bind().dialect.name == "sqlite":
                database.connection().exec_driver_sql("BEGIN")
            try:
                payload = CaptureInput(
                    url=url,
                    intent=intent,
                    revisit_on=revisit_on or None,
                    why_saved_note=why_saved_note or None,
                )
                result = capture_service.capture(database, user_id=claims.user_id, payload=payload)
                events.create_action_event(
                    database,
                    event_name=ActionEventName.CAPTURE_SUCCEEDED,
                    user_id=claims.user_id,
                    session_id=claims.session_id,
                    correlation_id=operation_id,
                    idempotency_key=f"capture:{operation_id}:succeeded",
                    actor=ActionActor.SYSTEM,
                    item_id=result.item.id,
                    occurred_at=current_utc(),
                    properties={
                        "created": result.created,
                        "extraction_status": result.item.extraction_status.value,
                    },
                )
                database.commit()
            except (ValidationError, CaptureRejected) as exc:
                database.rollback()
                events.create_action_event(
                    database,
                    event_name=ActionEventName.CAPTURE_FAILED,
                    user_id=claims.user_id,
                    session_id=claims.session_id,
                    correlation_id=operation_id,
                    idempotency_key=f"capture:{operation_id}:failed",
                    actor=ActionActor.SYSTEM,
                    occurred_at=current_utc(),
                    properties={"reason": exc.code.value if isinstance(exc, CaptureRejected) else "VALIDATION"},
                )
                database.commit()
                return failed_form()

        notice = "saved" if result.created else "duplicate"
        return RedirectResponse(url=f"/items/{result.item.id}?notice={notice}", status_code=303)

    @router.get("/items/{item_id}", response_class=HTMLResponse)
    def item_detail(request: Request, item_id: str, notice: str | None = None) -> Response:
        with session_factory() as database:
            principal = signed_in(request, database)
            if principal is None:
                return login_redirect()
            claims, user = principal
            item = items.get(database, user_id=claims.user_id, item_id=item_id)
            if item is None:
                raise HTTPException(status_code=404, detail="Saved item not found")
            item_view = _item_view(item)
            minimum_date = local_today(user).isoformat()
            active_resurfacing = events.has_active_cycle_for_item(
                database,
                user_id=claims.user_id,
                item_id=item_id,
            )
        context = authenticated_context(claims)
        context.update(
            {
                "item": item_view,
                "minimum_date": minimum_date,
                "active_resurfacing": active_resurfacing,
                "error": None,
                "notice": {
                    "saved": "Article saved.",
                    "duplicate": "This article was already saved. You can change its revisit date here.",
                    "date-updated": "Revisit date updated.",
                }.get(notice),
            }
        )
        return templates.TemplateResponse(request=request, name="item_detail.html", context=context)

    @router.post("/items/{item_id}/date")
    def update_item_date(
        request: Request,
        item_id: str,
        revisit_on: Annotated[str, Form()],
        expected_updated_at: Annotated[str, Form()] = "",
        csrf_token: Annotated[str, Form()] = "",
    ) -> Response:
        with session_factory() as database:
            principal = signed_in(request, database)
            if principal is None:
                return login_redirect()
            claims, user = principal
            require_csrf(claims, csrf_token)
            item = items.get(database, user_id=claims.user_id, item_id=item_id)
            if item is None:
                raise HTTPException(status_code=404, detail="Saved item not found")
            if item.status is not SavedItemStatus.ACTIVE:
                raise HTTPException(
                    status_code=409,
                    detail="Dismissed saved items cannot be rescheduled",
                )
            if events.has_active_cycle_for_item(
                database,
                user_id=claims.user_id,
                item_id=item_id,
            ):
                context = authenticated_context(claims)
                context.update(
                    {
                        "item": _item_view(item),
                        "minimum_date": local_today(user).isoformat(),
                        "active_resurfacing": True,
                        "notice": None,
                        "error": "The revisit date is locked while this item's resurfacing decision is active.",
                    }
                )
                return templates.TemplateResponse(
                    request=request,
                    name="item_detail.html",
                    context=context,
                    status_code=409,
                )
            try:
                payload = RevisitDateInput(revisit_on=revisit_on)
                if payload.revisit_on < local_today(user):
                    raise ValueError("past date")
            except (ValidationError, ValueError):
                item_view = _item_view(item)
                context = authenticated_context(claims)
                context.update(
                    {
                        "item": item_view,
                        "minimum_date": local_today(user).isoformat(),
                        "active_resurfacing": False,
                        "notice": None,
                        "error": "Choose today or a future date.",
                    }
                )
                return templates.TemplateResponse(
                    request=request,
                    name="item_detail.html",
                    context=context,
                    status_code=422,
                )
            try:
                expected_version = datetime.fromisoformat(expected_updated_at)
                if expected_version.tzinfo is None:
                    raise ValueError("timezone required")
                expected_version = expected_version.astimezone(timezone.utc)
            except ValueError:
                context = authenticated_context(claims)
                context.update(
                    {
                        "item": _item_view(item),
                        "minimum_date": local_today(user).isoformat(),
                        "active_resurfacing": False,
                        "notice": None,
                        "error": "This date form is no longer current. Refresh before trying again.",
                    }
                )
                return templates.TemplateResponse(
                    request=request,
                    name="item_detail.html",
                    context=context,
                    status_code=409,
                )
            updated = items.update_schedule_if_no_active_cycle(
                database,
                user_id=claims.user_id,
                item_id=item_id,
                expected_updated_at=expected_version,
                schedule_kind=ScheduleKind.STANDARD,
                next_due_on=payload.revisit_on,
                schedule_timezone=user.timezone,
            )
            if not updated:
                database.rollback()
                current_item = items.get(
                    database,
                    user_id=claims.user_id,
                    item_id=item_id,
                )
                if current_item is None:
                    raise HTTPException(status_code=404, detail="Saved item not found")
                context = authenticated_context(claims)
                context.update(
                    {
                        "item": _item_view(current_item),
                        "minimum_date": local_today(user).isoformat(),
                        "active_resurfacing": events.has_active_cycle_for_item(
                            database,
                            user_id=claims.user_id,
                            item_id=item_id,
                        ),
                        "notice": None,
                        "error": "The item changed before the date could be saved. Refresh and use the controls now available.",
                    }
                )
                return templates.TemplateResponse(
                    request=request,
                    name="item_detail.html",
                    context=context,
                    status_code=409,
                )
            database.commit()
        return RedirectResponse(url=f"/items/{item_id}?notice=date-updated", status_code=303)

    @router.post("/items/{item_id}/delete")
    def delete_item(
        request: Request,
        item_id: str,
        csrf_token: Annotated[str, Form()] = "",
    ) -> Response:
        with session_factory() as database:
            principal = signed_in(request, database)
            if principal is None:
                return login_redirect()
            claims, _ = principal
            require_csrf(claims, csrf_token)
            if not items.hard_delete(database, user_id=claims.user_id, item_id=item_id):
                raise HTTPException(status_code=404, detail="Saved item not found")
            database.commit()
        return RedirectResponse(url="/items?notice=deleted", status_code=303)

    return router
