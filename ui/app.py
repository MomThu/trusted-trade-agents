"""One process for the demo: MCP endpoint + REST mirror + human-only UI backend + UI.

    uvicorn ui.app:app --port 8000

  /mcp            MCP (streamable HTTP) for buyer agents         Bearer <customer key>
  /v1/...         Same agent tools as REST/OpenAPI (see /docs)   Bearer <customer key>
  /api/ui/...     Human-only: approve, reject, cancel, mandates  X-Staff-Key
  /               Approval UI (Valio + buyer view)
"""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent import buyer_agent, voice
from data.vocab import ALLERGENS
from order_service import config, db, orders, valio_client
from order_service.models import Need, OrderLine
from order_service.offers import find_offers
from mcp_server.server import mcp

STAFF_KEY = os.getenv("STAFF_KEY", "demo-staff")
STATIC = Path(__file__).resolve().parent / "static"

mcp_app = mcp.streamable_http_app()


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.con()
    async with mcp.session_manager.run():
        yield


app = FastAPI(
    title="Valio Aimo Trusted Trade Agents",
    version="0.1.0",
    description="Valio-side interface for external AI purchasing agents. Agents: MCP at /mcp or REST at /v1. "
                "Humans: /api/ui (approve, reject, cancel, mandates). Rules decide; no AI decides.",
    lifespan=lifespan,
)


@app.middleware("http")
async def no_stale_ui(request: Request, call_next):
    """Browsers must revalidate the UI files, so a reload always shows the latest version."""
    response = await call_next(request)
    if request.url.path.startswith("/ui"):
        response.headers["Cache-Control"] = "no-cache"
    return response


# ------------------------------------------------------------------ auth

def agent_buyer(authorization: str | None = Header(None), x_api_key: str | None = Header(None)) -> dict:
    key = authorization[7:].strip() if authorization and authorization.lower().startswith("bearer ") else x_api_key
    buyer = config.buyer_for_key(key)
    if not buyer:
        raise HTTPException(401, "Unknown or missing buyer API key")
    return buyer


def staff(x_staff_key: str | None = Header(None)) -> str:
    if x_staff_key != STAFF_KEY:
        raise HTTPException(401, "Staff key required (human-only endpoint)")
    return "staff"


# ------------------------------------------------------------------ REST mirror of the agent tools

class PlaceOrderBody(BaseModel):
    lines: list[OrderLine]
    need: Need


@app.post("/v1/find_offers", tags=["agent"])
async def v1_find_offers(need: Need, buyer: dict = Depends(agent_buyer)):
    return await find_offers(buyer, need)


@app.post("/v1/place_order", tags=["agent"])
async def v1_place_order(body: PlaceOrderBody, buyer: dict = Depends(agent_buyer)):
    return await orders.place_order(buyer, body.lines, body.need)


@app.get("/v1/orders/{order_id}", tags=["agent"])
def v1_order(order_id: str, buyer: dict = Depends(agent_buyer)):
    o = orders.get_order(order_id, buyer["buyer_id"])
    if not o:
        raise HTTPException(404, "order not found")
    return o


@app.get("/v1/audit", tags=["agent"])
def v1_audit(order_id: str | None = None, buyer: dict = Depends(agent_buyer)):
    return orders.audit_log(buyer["buyer_id"], order_id)


# ------------------------------------------------------------------ human-only UI backend

class Decision(BaseModel):
    approver: str = "Purchasing manager"
    note: str | None = None
    remove_lines: list[int] | None = None


def _transition(fn, *args, **kw):
    try:
        return fn(*args, **kw)
    except orders.TransitionError as e:
        raise HTTPException(409, str(e))


@app.get("/api/ui/orders", tags=["human"])
def ui_orders(status: str | None = None, buyer_id: str | None = None, _: str = Depends(staff)):
    return orders.list_orders(buyer_id, status)


@app.get("/api/ui/orders/{order_id}", tags=["human"])
def ui_order(order_id: str, _: str = Depends(staff)):
    o = orders.get_order(order_id)
    if not o:
        raise HTTPException(404, "order not found")
    o["audit"] = orders.audit_log(order_id=order_id)
    return o


@app.post("/api/ui/orders/{order_id}/approve", tags=["human"])
def ui_approve(order_id: str, d: Decision, _: str = Depends(staff)):
    return _transition(orders.approve, order_id, d.approver, d.remove_lines, d.note)


@app.post("/api/ui/orders/{order_id}/reject", tags=["human"])
def ui_reject(order_id: str, d: Decision, _: str = Depends(staff)):
    return _transition(orders.reject, order_id, d.approver, d.note)


@app.post("/api/ui/orders/{order_id}/cancel", tags=["human"])
def ui_cancel(order_id: str, d: Decision, _: str = Depends(staff)):
    return _transition(orders.cancel, order_id, d.approver, d.note)


@app.get("/api/ui/audit", tags=["human"])
def ui_audit(buyer_id: str | None = None, order_id: str | None = None, _: str = Depends(staff)):
    return orders.audit_log(buyer_id, order_id, 500)


@app.get("/api/ui/buyers", tags=["human"])
def ui_buyers(_: str = Depends(staff)):
    return [{"buyer_id": b["buyer_id"], "name": b["name"], "api_key_hint": b["api_key"][:9] + "…",
             "mandate": b["mandate"]} for b in config.load_buyers().values()]


class MandateUpdate(BaseModel):
    spend_limit_eur: float | None = None
    allergen_policy_exclude: list[str] | None = None
    substitutions_need_approval: bool | None = None
    min_delivery_reliability: float | None = None
    unusual_quantity_factor: float | None = None
    price_increase_tolerance_pct: float | None = None


@app.get("/api/ui/vocab", tags=["human"])
def ui_vocab(_: str = Depends(staff)):
    return {"allergen_groups": sorted(ALLERGENS)}


@app.put("/api/ui/buyers/{buyer_id}/mandate", tags=["human"])
def ui_mandate(buyer_id: str, m: MandateUpdate, _: str = Depends(staff)):
    changes = m.model_dump(exclude_none=True)
    if "allergen_policy_exclude" in changes:
        groups = sorted(set(changes.pop("allergen_policy_exclude")))
        bad = [g for g in groups if g not in ALLERGENS]
        if bad:
            raise HTTPException(422, f"unknown allergen group(s): {bad}")
        changes["allergen_policy"] = {"exclude": groups, "confirmed_by": "Valio admin (on behalf of customer)",
                                      "confirmed_at": db.now_iso()}
    try:
        b = config.save_mandate(buyer_id, changes)
    except KeyError:
        raise HTTPException(404, "buyer not found")
    db.audit("mandate_changed", f"Mandate updated: {changes}", buyer_id=buyer_id, actor="human:Valio admin", data=changes)
    return b["mandate"]


class ScenarioBody(BaseModel):
    name: str | None = None


@app.get("/api/ui/scenario", tags=["demo"])
def ui_scenario_get(_: str = Depends(staff)):
    return {"active": valio_client.active_scenario(),
            "available": {k: v.get("description") for k, v in config.load_scenarios().items()}}


@app.post("/api/ui/scenario", tags=["demo"])
def ui_scenario_set(body: ScenarioBody, _: str = Depends(staff)):
    try:
        valio_client.set_scenario(body.name)
    except KeyError:
        raise HTTPException(404, "unknown scenario")
    db.audit("demo_scenario", f"Demo scenario overlay set to {body.name or 'off'}", buyer_id=None, actor="human:demo operator")
    return {"active": valio_client.active_scenario()}


@app.post("/api/ui/reset", tags=["demo"])
def ui_reset(_: str = Depends(staff)):
    db.reset(seed=True)
    valio_client.set_scenario(None)
    return {"ok": True}


@app.get("/api/ui/presets", tags=["demo"])
def ui_presets(_: str = Depends(staff)):
    return [{"id": p["id"], "title": p["title"], "text": p["text"], "buyer_key": p["buyer_key"],
             "buyer_id": (config.buyer_for_key(p["buyer_key"]) or {}).get("buyer_id")} for p in buyer_agent.load_presets().values()]


class AgentRun(BaseModel):
    preset: str | None = None
    text: str | None = None
    buyer_id: str = "hotel-aurora"


_agent_lock = asyncio.Lock()


@app.post("/api/ui/agent/run", tags=["demo"])
async def ui_agent_run(body: AgentRun, request: Request, _: str = Depends(staff)):
    """Run the reference buyer agent (an EXTERNAL client that talks to /mcp like any vendor's agent)."""
    url = str(request.base_url).rstrip("/") + "/mcp"
    events: list[dict] = []

    def on_event(e: dict):
        e = dict(e)
        if e.get("kind") == "tool_result":
            e.pop("result", None)
        events.append(e)

    async with _agent_lock:
        prev = valio_client.active_scenario()
        try:
            if body.preset:
                p = buyer_agent.load_presets()[body.preset]
                valio_client.set_scenario(p.get("scenario"))
                after = p.get("scenario_after_find")
                result = await buyer_agent.run_scripted(p, p["buyer_key"], url, on_event,
                                                        before_order=(lambda: valio_client.set_scenario(after)) if after else None)
                mode = "scripted"
            elif body.text:
                b = config.load_buyers().get(body.buyer_id)
                if not b:
                    raise HTTPException(404, "buyer not found")
                if not config.GEMINI_API_KEY:
                    raise HTTPException(400, "GEMINI_API_KEY not set: use a preset (scripted agent) or add the key to .env")
                result = await buyer_agent.run_llm(body.text, b["api_key"], b["name"], url, on_event)
                mode = "gemini"
            else:
                raise HTTPException(400, "preset or text required")
        except KeyError:
            raise HTTPException(404, "unknown preset")
        except HTTPException:
            raise
        except Exception as e:  # surface agent/LLM failures in the console instead of a 500
            while isinstance(e, BaseExceptionGroup) and e.exceptions:   # unwrap TaskGroup errors
                e = e.exceptions[0]
            events.append({"kind": "error", "text": f"{type(e).__name__}: {e}"})
            result = {"outcome": "agent_error"}
        finally:
            valio_client.set_scenario(prev)
    order = result.get("order") or {}
    return {"mode": locals().get("mode"), "outcome": result.get("outcome"), "order_id": order.get("order_id"), "events": events}


# ------------------------------------------------------------------ buyer agent voice (ElevenLabs)
# Voice belongs to the BUYER's agent: the chef talks to their agent, the agent still talks to Valio in JSON.

@app.post("/api/ui/agent/voice/stt", tags=["demo"])
async def ui_voice_stt(audio: UploadFile = File(...), _: str = Depends(staff)):
    data = await audio.read()
    if not data:
        raise HTTPException(400, "empty recording")
    try:
        return await voice.speech_to_text(data, audio.filename or "speech.webm", audio.content_type or "audio/webm")
    except voice.VoiceError as e:
        raise HTTPException(502, str(e))


@app.post("/api/ui/agent/voice/realtime-token", tags=["demo"])
async def ui_voice_realtime_token(_: str = Depends(staff)):
    """Short-lived token for live (streaming) transcription straight from the browser to ElevenLabs."""
    try:
        return {"token": await voice.realtime_token(),
                "model_id": os.getenv("ELEVENLABS_REALTIME_MODEL", "scribe_v2_realtime")}
    except voice.VoiceError as e:
        raise HTTPException(502, str(e))


class SpeakBody(BaseModel):
    text: str


@app.post("/api/ui/agent/voice/tts", tags=["demo"])
async def ui_voice_tts(body: SpeakBody, _: str = Depends(staff)):
    if not body.text.strip():
        raise HTTPException(400, "no text")
    try:
        audio = await voice.text_to_speech(body.text)
    except voice.VoiceError as e:
        raise HTTPException(502, str(e))
    return Response(audio, media_type="audio/mpeg")


# ------------------------------------------------------------------ UI + MCP mount

@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/ui/")


@app.get("/health", include_in_schema=False)
def health():
    return {"ok": True, "valio_api_key": bool(config.VALIO_API_KEY), "gemini": bool(config.GEMINI_API_KEY),
            "elevenlabs": voice.enabled(),
            "scenario": valio_client.active_scenario()}


app.mount("/ui", StaticFiles(directory=STATIC, html=True), name="ui")
app.mount("/", mcp_app)   # serves /mcp; must be last
