"""HTTP API + static UI for the Emberfall game-master swarm."""
import re
import threading
from collections import defaultdict
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import agents
from .graph import QUERIES, World, db
from .seed import seed

app = FastAPI(title="Emberfall: a game-master swarm on FalkorDB")
STATIC = Path(__file__).resolve().parent.parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")

# Turns within one campaign are serialized so the story stays linear; different
# campaigns (tenants, i.e. separate graphs) run fully in parallel.
_locks = defaultdict(threading.Lock)


def _world(campaign: str) -> World:
    if not re.fullmatch(r"[a-z0-9_-]{1,40}", campaign):
        raise HTTPException(400, "campaign must be lowercase letters, digits, - or _")
    return World(campaign)


class Turn(BaseModel):
    player_id: str
    text: str


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/campaigns")
def campaigns():
    return sorted(g[4:] for g in db().list_graphs() if g.startswith("dnd_"))


@app.post("/api/campaigns/{campaign}/seed")
def seed_campaign(campaign: str, mode: str = "demo"):
    _world(campaign)
    with _locks[campaign]:
        return seed(campaign, mode if mode in ("demo", "fresh") else "demo")


@app.get("/api/campaigns/{campaign}/state")
def state(campaign: str):
    w = _world(campaign)
    try:
        sessions = w.run("sessions", readonly=True)
    except Exception:
        raise HTTPException(404, "campaign not seeded")
    if not sessions:
        raise HTTPException(404, "campaign not seeded")
    players = []
    for (pid,) in w.raw("MATCH (p:Player) RETURN p.id ORDER BY p.id"):
        e = w.run("entity", {"id": pid}, readonly=True)[0]
        players.append({"id": pid, "name": e["e"]["name"], "location": e["location"], "owns": e["owns"]})
    active = next(s for s in sessions if s["status"] == "active")
    history = list(reversed(w.run("recent_messages", {"sid": active["id"], "limit": 60}, readonly=True)))
    return {"mode": "llm" if agents.llm_enabled() else "offline", "model": agents.MODEL,
            "sessions": sessions, "players": players, "history": history}


@app.post("/api/campaigns/{campaign}/turn")
def turn(campaign: str, t: Turn):
    w = _world(campaign)
    with _locks[campaign]:
        return agents.play_turn(w, t.player_id, t.text.strip()[:500])


@app.post("/api/campaigns/{campaign}/end-session")
def end_session(campaign: str):
    w = _world(campaign)
    with _locks[campaign]:
        return agents.end_session(w)


@app.get("/api/campaigns/{campaign}/graph")
def graph(campaign: str, messages: bool = False):
    return _world(campaign).graph_dump(include_messages=messages)


@app.get("/api/campaigns/{campaign}/explain/{decision_id}")
def explain(campaign: str, decision_id: str):
    r = agents.explain(_world(campaign), decision_id)
    if not r:
        raise HTTPException(404, "no such decision")
    return r


@app.get("/api/campaigns/{campaign}/decisions")
def decisions(campaign: str):
    rows = _world(campaign).raw(
        "MATCH (n:NPC)-[:MADE]->(d:Decision)-[:TOWARD]->(p:Player) "
        "RETURN d.id, n.name, p.name, d.verdict, d.request, d.session ORDER BY d.ts DESC LIMIT 30")
    return [dict(zip(["id", "npc", "player", "verdict", "request", "session"], r)) for r in rows]


@app.get("/api/campaigns/{campaign}/memory/{npc_id}/{player_id}")
def memory(campaign: str, npc_id: str, player_id: str):
    w = _world(campaign)
    prof, rel, rules, mems, att = agents.npc_context(w, npc_id, player_id)
    return {"npc": prof["n"]["name"], "attitude": att, "memories": mems, "rules": rules, "relationships": rel}


@app.get("/api/queries")
def queries():
    return {k: " ".join(v.split()) for k, v in QUERIES.items()}
