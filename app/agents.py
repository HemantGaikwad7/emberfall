"""The game-master swarm: Narrator, NPC and World-keeper agents.

They never pass world state to each other in prompts. Each agent reads what it
needs from the shared FalkorDB graph and writes its results back to it.
Handoffs carry only an intent; the graph is the source of truth.
"""
import contextvars
import json
import os
import re
import time

import groq
from dotenv import load_dotenv

from .graph import QUERIES, World, new_id

load_dotenv()

try:  # trust the OS certificate store (needed behind corporate TLS-inspecting proxies)
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
EFFORT = os.getenv("GROQ_REASONING_EFFORT", "low")  # only sent to reasoning models (gpt-oss)
# Each Groq model has its own free-tier quota, so when one is rate-limited we move down this list.
MODELS = [MODEL] + [m.strip() for m in os.getenv(
    "GROQ_FALLBACK_MODELS", "openai/gpt-oss-20b,qwen/qwen3.8-27b").split(",")
    if m.strip() and m.strip() != MODEL]

_client = None
_exhausted = {}  # model -> unix time until which we skip it
last_model = None


class LLMUnavailable(Exception):
    """Every configured model is rate-limited; callers fall back to the offline engine."""


_force_offline = contextvars.ContextVar("force_offline", default=False)


def llm_enabled():
    if os.getenv("DND_OFFLINE") == "1" or _force_offline.get():
        return False
    return bool(os.getenv("GROQ_API_KEY"))


def client():
    global _client
    if _client is None:
        _client = groq.Groq(max_retries=1)  # fail fast on 429 so we can switch models
    return _client


def _step(world, **kw):
    if world.trace is not None:
        world.trace.append(kw)


def _fn(tool):
    return {"type": "function", "function": {"name": tool["name"], "description": tool["description"],
                                             "parameters": tool["input_schema"]}}


def chat(messages, tools=None, max_tokens=4096):
    """One Groq chat completion. Retries malformed tool calls; switches model when one is rate-limited."""
    global last_model
    for model in MODELS:
        if _exhausted.get(model, 0) > time.time():
            continue
        kw = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": 0.7}
        if tools:
            kw.update(tools=[_fn(t) for t in tools], tool_choice="auto")
        if "gpt-oss" in model:
            kw["reasoning_effort"] = EFFORT
        elif "qwen" in model:
            kw["reasoning_format"] = "hidden"  # keep <think> text out of the narration
        for attempt in range(3):
            try:
                msg = client().chat.completions.create(**kw).choices[0].message
                last_model = model
                return msg
            except groq.RateLimitError:
                _exhausted[model] = time.time() + 600
                break
            except (groq.NotFoundError, groq.PermissionDeniedError):
                _exhausted[model] = time.time() + 86400  # this key cannot use the model
                break
            except groq.BadRequestError as ex:
                if "tool_use_failed" not in str(ex) or attempt == 2:
                    raise
    raise LLMUnavailable("all Groq models are rate-limited right now")


def run_agent(world, agent, system, user, tools, handlers, stop_tools=(), max_turns=6):
    """Manual tool-use loop. Returns (final_text, stopped_by_tool_output)."""
    _step(world, type="agent", agent=agent, text="thinking...")
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    stopped = None
    text = ""
    for _ in range(max_turns):
        msg = chat(messages, tools)
        text = (msg.content or "").strip()
        calls = msg.tool_calls or []
        if not calls:
            return text, stopped
        messages.append({"role": "assistant", "content": msg.content or "",
                         "tool_calls": [{"id": c.id, "type": "function",
                                         "function": {"name": c.function.name, "arguments": c.function.arguments}}
                                        for c in calls]})
        for call in calls:
            name = call.function.name
            try:
                args = json.loads(call.function.arguments or "{}")
                out = handlers[name](**args)
                is_error = isinstance(out, dict) and "error" in out
            except Exception as ex:  # surface errors to the model instead of crashing the turn
                args, out, is_error = call.function.arguments, {"error": f"{type(ex).__name__}: {ex}"}, True
            _step(world, type="tool", agent=agent, name=name, input=args, output=out)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps(out, default=str)})
            if name in stop_tools and not is_error:
                stopped = out
        if stopped is not None:
            return text, stopped
    return text, stopped


# --------------------------------------------------------------------------- NPC agent

NPC_DECIDE_TOOL = {
    "name": "decide",
    "description": "Record your in-character decision about the player's request. Call exactly once.",
    "input_schema": {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["help", "refuse", "negotiate", "neutral"],
                        "description": "help = grant the request; refuse = deny it; negotiate = conditions; neutral = no request was made"},
            "dialogue": {"type": "string", "description": "What you say aloud, in character (1-4 sentences)."},
            "reasoning": {"type": "string", "description": "Out-of-character one-sentence explanation of why."},
            "evidence_event_ids": {"type": "array", "items": {"type": "string"},
                                   "description": "IDs of the remembered events that drove this decision. Only IDs from your memory list."},
            "rule_ids": {"type": "array", "items": {"type": "string"},
                         "description": "IDs of the codes/rules that shaped this decision."},
        },
        "required": ["verdict", "dialogue", "reasoning", "evidence_event_ids", "rule_ids"],
    },
}


def npc_context(world, npc_id, player_id):
    prof = world.run("npc_profile", {"nid": npc_id}, readonly=True)[0]
    rel = world.run("npc_relationships", {"nid": npc_id}, readonly=True)[0]
    rules = world.run("npc_rules", {"nid": npc_id}, readonly=True)
    mems = world.run("npc_memories", {"nid": npc_id, "pid": player_id}, readonly=True)
    att = world.run("npc_attitude", {"nid": npc_id, "pid": player_id}, readonly=True)[0]["score"]
    return prof, rel, rules, mems, att


def npc_agent(world, npc_id, player_id, player_says, request):
    """The NPC reasons only from its own memories in the graph, then commits a Decision node."""
    prof, rel, rules, mems, att = npc_context(world, npc_id, player_id)
    npc = prof["n"]
    player = world.run("entity", {"id": player_id}, readonly=True)[0]["e"]
    kin = [k for k in rel["kin"] if k.get("name")]
    kin_txt = "; ".join(
        f"{k['name']} is your {k['relation']}" if k["direction"] == "in" else f"you are {k['name']}'s {k['relation']}"
        for k in kin) or "none"
    mem_txt = "\n".join(
        f"- [{m['event_id']}] session {m['session']}: {m['what']} (target: {m['target']}, sentiment {m['sentiment']:+.0f}, you know because: {m['learned_via']})"
        for m in mems) or "- (you have no memories of this person)"
    rules_txt = "\n".join(f"- [{r['id']}] {r['text']}" for r in rules) or "- (none)"

    system = (
        f"You are {npc['name']} ({npc.get('pronouns', 'they/them')}), {npc['role']} in the town of Emberfall, in a tabletop RPG run by a team of AI game masters.\n"
        f"Personality: {npc['persona']}\n"
        f"Faction: {prof['faction'] or 'none'}. Family: {kin_txt}.\n\n"
        f"Codes you live by:\n{rules_txt}\n\n"
        "You decide only from the memories you are given: they are the shared world graph's record of what this "
        "person has actually done, possibly in earlier sessions. Never invent past events. If a memory matters, "
        "let it show in what you say (name the deed and the session). Respond by calling the `decide` tool exactly once."
    )
    user = (
        f"{player['name']} ({player.get('description', '')}) stands before you.\n"
        f"Your memories of {player['name']}:\n{mem_txt}\n"
        f"Your overall feeling toward them (graph-computed, negative = hostile): {att:+.1f}\n\n"
        f"They say: \"{player_says}\"\n"
        f"What they want from you: {request or 'nothing specific'}"
    )
    valid_events = {m["event_id"] for m in mems}
    valid_rules = {r["id"] for r in rules}
    decision = {}

    def decide(verdict, dialogue, reasoning, evidence_event_ids, rule_ids):
        decision.update(verdict=verdict, dialogue=dialogue, reasoning=reasoning,
                        evidence=[e for e in evidence_event_ids if e in valid_events],
                        rules=[r for r in rule_ids if r in valid_rules])
        return {"ok": True}

    if llm_enabled():
        _step(world, type="handoff", frm="narrator", to=f"npc:{npc['name']}", text=request or player_says)
        text, _ = run_agent(world, f"npc:{npc['name']}", system, user, [NPC_DECIDE_TOOL],
                            {"decide": decide}, stop_tools=("decide",), max_turns=2)
        if not decision:
            decision.update(verdict="neutral", dialogue=text or "...", reasoning="no explicit decision",
                            evidence=[], rules=[])
    else:
        decision.update(_offline_npc(npc, mems, att, rules, request, player))

    return commit_decision(world, npc, player, request or player_says, decision)


def commit_decision(world, npc, player, request, d):
    s = world.active_session()
    did = new_id("dec")
    world.run("create_decision", {"nid": npc["id"], "pid": player["id"], "sid": s["id"], "did": did,
                                  "verdict": d["verdict"], "reasoning": d["reasoning"],
                                  "request": request, "ts": time.time()})
    if d["evidence"]:
        world.run("decision_evidence", {"did": did, "eids": d["evidence"]})
    if d["rules"]:
        world.run("decision_rules", {"did": did, "rids": d["rules"]})
    world.record_message("npc", npc["name"], d["dialogue"])
    out = {"decision_id": did, "npc": npc["name"], "npc_id": npc["id"], "verdict": d["verdict"],
           "dialogue": d["dialogue"], "reasoning": d["reasoning"], "evidence": d["evidence"],
           "explainable": bool(d["evidence"])}
    _step(world, type="decision", agent=f"npc:{npc['name']}", **out)
    return out


# --------------------------------------------------------------------------- World-keeper agent

WK_TOOLS = [
    {"name": "commit_event",
     "description": "Write a significant deed into world memory. Memory of it propagates automatically to the "
                    "target, witnesses at the location, the target's kin and faction.",
     "input_schema": {"type": "object", "properties": {
         "description": {"type": "string", "description": "Third-person past tense, naming actor and target."},
         "kind": {"type": "string", "enum": ["insult", "harm", "theft", "help", "gift", "apology", "favor", "deal", "other"]},
         "sentiment": {"type": "integer", "minimum": -3, "maximum": 3,
                       "description": "How the target would feel: -3 grave offense ... +3 deep gratitude."},
         "actor_ids": {"type": "array", "items": {"type": "string"}},
         "target_id": {"type": "string", "description": "ID of the NPC affected (or empty)."},
         "location_id": {"type": "string"}},
         "required": ["description", "kind", "sentiment", "actor_ids", "target_id", "location_id"]}},
    {"name": "move_player",
     "description": "Move a player to any location reachable by road; they travel the shortest route. "
                    "Fails if no road connects them.",
     "input_schema": {"type": "object", "properties": {"player_id": {"type": "string"}, "location_id": {"type": "string"}},
                      "required": ["player_id", "location_id"]}},
    {"name": "transfer_item",
     "description": "Move an item between owners. Fails if from_id does not own it.",
     "input_schema": {"type": "object", "properties": {"item_id": {"type": "string"}, "from_id": {"type": "string"},
                                                       "to_id": {"type": "string"}},
                      "required": ["item_id", "from_id", "to_id"]}},
    {"name": "reject_change",
     "description": "Refuse a proposed change that would break world consistency.",
     "input_schema": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}},
]


def _wk_handlers(world, ledger):
    def commit_event(description, kind, sentiment, actor_ids, target_id="", location_id=""):
        r = world.commit_event(description, kind, sentiment, actor_ids, target_id or None, location_id or None)
        ledger.append({"op": "event", "description": description, **r})
        return r

    def move_player(player_id, location_id):
        route = world.run("route", {"pid": player_id, "lid": location_id}, readonly=True)
        if not route:
            return {"error": f"no road leads from {player_id}'s current location to {location_id} (or already there)"}
        rows = world.run("move_player", {"pid": player_id, "lid": location_id})
        path = " → ".join(route[0]["route"])
        ledger.append({"op": "move", "player_id": player_id, "to": f"{rows[0]['b.name']} via {path}"})
        return {"ok": True, "now_at": rows[0]["b.name"], "route": route[0]["route"]}

    def transfer_item(item_id, from_id, to_id):
        rows = world.run("transfer_item", {"item_id": item_id, "from_id": from_id, "to_id": to_id})
        if not rows:
            return {"error": f"{from_id} does not own {item_id}"}
        ledger.append({"op": "transfer", "item": rows[0]["i.name"], "from": from_id, "to": to_id})
        return {"ok": True}

    def reject_change(reason):
        ledger.append({"op": "rejected", "reason": reason})
        return {"ok": True}

    return {"commit_event": commit_event, "move_player": move_player,
            "transfer_item": transfer_item, "reject_change": reject_change}


def world_keeper(world, player_id, proposal):
    """Validates proposed changes against world rules and graph facts, then commits them."""
    rules = world.run("world_rules", readonly=True)
    scene = world.run("scene", {"pid": player_id}, readonly=True)[0]
    party = [player_id] + [p["id"] for p in scene["party"]]
    facts = [world.run("entity", {"id": pid}, readonly=True)[0] for pid in party]
    fact_txt = "\n".join(
        f"- {f['e']['name']} [{f['e']['id']}] at {f['location']}, owns: {', '.join(f['owns']) or 'nothing'}" for f in facts)
    items = world.raw("MATCH (o)-[:OWNS]->(i:Item) RETURN i.id, i.name, o.id, o.name")
    item_txt = "\n".join(f"- {r[1]} [{r[0]}] owned by {r[3]} [{r[2]}]" for r in items)
    npcs_here = "\n".join(f"- {n['name']} [{n['id']}] alive={n.get('alive')}" for n in scene["npcs"]) or "- none"
    exits = ", ".join(f"{x['name']} [{x['id']}]" for x in scene["exits"])
    places = ", ".join(f"{x['name']} [{x['id']}]" for x in world.run("locations", readonly=True))
    loc = scene["l"]

    system = (
        "You are the World-keeper of a tabletop RPG run by AI game masters. You own world consistency. "
        "The Narrator proposes what just happened; you check it against the world rules and current graph facts, "
        "commit what is valid with your tools, and reject what is not. Commit an event for any socially significant "
        "deed (insults, threats, help, gifts, apologies, thefts, violence) so NPCs remember it in future sessions. "
        "Do not commit idle chatter. Be terse."
    )
    user = (
        "World rules:\n" + "\n".join(f"- {r['text']}" for r in rules) +
        f"\n\nCurrent location: {loc['name']} [{loc['id']}]. Exits: {exits}\n"
        f"All places (reachable by road): {places}\nNPCs here:\n{npcs_here}\n"
        f"Party:\n{fact_txt}\nItem ownership:\n{item_txt}\n\nNarrator's proposal:\n{json.dumps(proposal)}"
    )
    ledger = []
    _step(world, type="handoff", frm="narrator", to="world-keeper", text=proposal.get("summary", ""))
    if llm_enabled():
        run_agent(world, "world-keeper", system, user, WK_TOOLS, _wk_handlers(world, ledger), max_turns=4)
    else:
        _offline_world_keeper(world, proposal, _wk_handlers(world, ledger))
    _step(world, type="ledger", agent="world-keeper", ledger=ledger)
    return {"committed": ledger}


# --------------------------------------------------------------------------- Narrator agent

NARRATOR_TOOLS = [
    {"name": "talk_to_npc",
     "description": "Hand the conversation to an NPC agent. The NPC decides from its own memories in the world graph. "
                    "Use whenever the player addresses, asks, threatens or bargains with an NPC.",
     "input_schema": {"type": "object", "properties": {
         "npc_id": {"type": "string"},
         "player_says": {"type": "string", "description": "The player's words/action toward the NPC."},
         "request": {"type": "string", "description": "What the player wants from the NPC, if anything."}},
         "required": ["npc_id", "player_says", "request"]}},
    {"name": "travel",
     "description": "The active player travels. Pass the FINAL destination the player named, even if it is several "
                    "roads away; the World-keeper routes them along the shortest road path and returns the route.",
     "input_schema": {"type": "object", "properties": {
         "destination_id": {"type": "string", "description": "Location id of where the player wants to end up."}},
         "required": ["destination_id"]}},
    {"name": "propose_world_change",
     "description": "Hand off to the World-keeper to validate and persist what happened: significant deeds (as events) "
                    "and items changing hands. Call once per turn after the action resolves, if anything changed. "
                    "Not for travel; use the travel tool.",
     "input_schema": {"type": "object", "properties": {
         "summary": {"type": "string", "description": "One sentence: what happened this turn."},
         "events": {"type": "array", "items": {"type": "object", "properties": {
             "description": {"type": "string"}, "kind": {"type": "string"},
             "sentiment": {"type": "integer"}, "actor_ids": {"type": "array", "items": {"type": "string"}},
             "target_id": {"type": "string"}}}},
         "transfers": {"type": "array", "items": {"type": "object", "properties": {
             "item_id": {"type": "string"}, "from_id": {"type": "string"}, "to_id": {"type": "string"}}}}},
         "required": ["summary"]}},
    {"name": "lookup",
     "description": "Look up any character, place, item or faction in the world graph by name.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
]


def _recall_story(world):
    sessions = world.run("sessions", readonly=True)
    s = world.active_session()
    recent = list(reversed(world.run("recent_messages", {"sid": s["id"], "limit": 12}, readonly=True)))
    past = "\n".join(f"Session {x['number']}: {x['summary']}" for x in sessions if x["status"] == "closed")
    now = "\n".join(f"{m['speaker']}: {m['text']}" for m in recent)
    return s, past, now


def narrator_turn(world, player_id, text):
    scene = world.run("scene", {"pid": player_id}, readonly=True)[0]
    player = world.run("entity", {"id": player_id}, readonly=True)[0]
    s, past, now = _recall_story(world)
    loc = scene["l"]
    npcs = "\n".join(f"- {n['name']} [{n['id']}], {n['role']}; pronouns: {n.get('pronouns', 'they/them')}"
                     for n in scene["npcs"]) or "- nobody"
    exits = ", ".join(f"{x['name']} [{x['id']}]" for x in scene["exits"])
    places = ", ".join(f"{x['name']} [{x['id']}]" for x in world.run("locations", readonly=True))
    party = ", ".join(f"{p['name']} ({p.get('pronouns', 'they/them')})" for p in scene["party"]) or "nobody else (the rest of the party is elsewhere)"

    system = (
        "You are the Narrator, lead game master of a fantasy tabletop RPG run by a swarm of AI agents that share one "
        "world graph. You describe scenes and resolve actions. You do NOT voice NPCs yourself. When the player engages "
        "an NPC, call talk_to_npc and weave the NPC's returned dialogue (verbatim, in quotes) into your narration. "
        "When the player goes somewhere, call travel with their final destination and narrate the journey along the "
        "returned route, ending at the destination. When a meaningful deed toward an NPC happens or an item is handed "
        "over, call propose_world_change so the World-keeper can validate and record it. If a tool returns an error or "
        "the World-keeper rejects a change, it did NOT happen: narrate accordingly. "
        "Only characters listed under 'NPCs present' and 'Party here' are in the scene; never place anyone else there. "
        "Use each character's listed pronouns. "
        "Write 2-5 vivid sentences of second-person narration, then stop. No menus, no meta commentary."
    )
    user = (
        f"Session {s['number']}. Story so far:\n{past or '(this is the first session)'}\n\n"
        f"Recent table talk:\n{now}\n\n"
        f"Scene: {loc['name']} [{loc['id']}]: {loc['description']}\nNPCs present:\n{npcs}\nExits: {exits}\n"
        f"All places (a player can travel to any of them; the World-keeper routes them by road): {places}\n"
        f"Active player: {player['e']['name']} [{player_id}], carrying: {', '.join(player['owns']) or 'nothing'}. "
        f"Party here: {party}.\n\n"
        f"{player['e']['name']} says/does: \"{text}\""
    )
    decisions, commits = [], []

    def talk_to_npc(npc_id, player_says, request=""):
        here = {n["id"]: n for n in scene["npcs"]}
        if npc_id not in here:
            info = world.run("entity", {"id": npc_id}, readonly=True)
            where = info[0]["location"] if info else "unknown"
            _step(world, type="blocked", agent="world-keeper", text=f"{npc_id} is at {where}, not here (rule: speak only to NPCs at your location)")
            return {"error": f"That NPC is not here; they are at {where}."}
        if not here[npc_id].get("alive", True):
            return {"error": "That NPC is dead and cannot speak."}
        d = npc_agent(world, npc_id, player_id, player_says, request)
        decisions.append(d)
        pr = here[npc_id].get("pronouns", "they/them")
        return {"npc": d["npc"], "pronouns": pr, "verdict": d["verdict"], "dialogue": d["dialogue"],
                "note": f"Refer to {d['npc']} as {pr} in your narration."}

    def propose_world_change(summary, events=(), moves=(), transfers=()):
        r = world_keeper(world, player_id, {"summary": summary, "events": list(events),
                                            "moves": list(moves), "transfers": list(transfers)})
        commits.extend(r["committed"])
        return r

    def travel(destination_id):
        _step(world, type="handoff", frm="narrator", to="world-keeper", text=f"travel to {destination_id}")
        ledger = []
        out = _wk_handlers(world, ledger)["move_player"](player_id, destination_id)
        if "error" in out:
            _step(world, type="blocked", agent="world-keeper", text=out["error"])
        _step(world, type="ledger", agent="world-keeper", ledger=ledger)
        commits.extend(ledger)
        return out

    def lookup(name):
        hits = world.run("find_by_name", {"name": name}, readonly=True)
        return [world.run("entity", {"id": h["id"]}, readonly=True)[0] | {"label": h["label"]} for h in hits][:3]

    narration, _ = run_agent(world, "narrator", system, user, NARRATOR_TOOLS,
                             {"talk_to_npc": talk_to_npc, "propose_world_change": propose_world_change,
                              "travel": travel, "lookup": lookup}, max_turns=6)
    return {"narration": narration, "decisions": decisions, "commits": commits}


# --------------------------------------------------------------------------- end of session

def end_session(world):
    """World-keeper consolidates the session's episodic memory into a semantic summary."""
    s = world.active_session()
    events = world.run("session_events", {"sid": s["id"]}, readonly=True)
    msgs = list(reversed(world.run("recent_messages", {"sid": s["id"], "limit": 40}, readonly=True)))
    summary = ""
    if llm_enabled() and (events or msgs):
        transcript = "\n".join(f"{m['speaker']}: {m['text']}" for m in msgs)
        deeds = "\n".join(f"- {e['what']} (by {', '.join(e['by'])}, {e['sentiment']:+.0f})" for e in events)
        try:
            msg = chat([
                {"role": "system", "content": "You are the World-keeper. Summarise a tabletop session in 2-3 sentences "
                                              "for future game masters. Name who did what to whom; keep grudges and "
                                              "debts explicit."},
                {"role": "user", "content": f"Recorded deeds:\n{deeds}\n\nTranscript:\n{transcript}"}])
            summary = (msg.content or "").strip()
        except LLMUnavailable:
            pass
    if not summary:
        summary = " ".join(e["what"] + "." for e in events) or "A quiet session; nothing of note happened."
    world.run("close_session", {"sid": s["id"], "summary": summary, "nid": new_id("ses"), "ts": time.time()})
    return {"closed": s["number"], "summary": summary, "now": s["number"] + 1}


# --------------------------------------------------------------------------- offline fallback
# Deterministic stand-ins so the demo runs without an API key. Same graph reads/writes, no LLM.

INSULT = re.compile(r"\b(insult|mock|rat|idiot|fool|spit|stupid|ugly|coward|smell|stink)\w*", re.I)
APOLOGY = re.compile(r"\b(sorry|apolog|amends|forgive)\w*", re.I)
FRIENDLY = re.compile(r"\b(thank|help|save|heal|protect)\w*", re.I)
MOVE = re.compile(r"\b(go|walk|travel|head|move)\b", re.I)
GIVE = re.compile(r"\b(give|hand|offer|gift|take this|accept)\b", re.I)


def _offline_npc(npc, mems, att, rules, request, player):
    grudges = sorted([m for m in mems if m["sentiment"] < 0], key=lambda m: m["sentiment"])
    kind = [m for m in mems if m["sentiment"] > 0]
    if att < -1 and grudges:
        g = grudges[0]
        about = "my brother " + g["target"].split()[0] if g["learned_via"] == "kin" else g["target"]
        return dict(verdict="refuse",
                    dialogue=f"You've got some nerve. Session {g['session']}, {g['place']}: you made a mockery of {about}. "
                             f"Ironroots don't forget. Get out of my sight.",
                    reasoning=f"Attitude {att:+.1f}; remembers: {g['what']} (via {g['learned_via']}).",
                    evidence=[g["event_id"]], rules=[r["id"] for r in rules])
    if kind:
        k = kind[-1]
        return dict(verdict="help", dialogue=f"Word reached me, session {k['session']}: {k['what']}. That counts for something. Aye, I'll help.",
                    reasoning=f"Attitude {att:+.1f}; remembers kindness.", evidence=[m["event_id"] for m in kind], rules=[])
    return dict(verdict="help" if request else "neutral", dialogue="I've no quarrel with you, stranger. Aye, we can deal." if request else "Hm. What do you want, stranger?",
                reasoning="No memories of this person.", evidence=[], rules=[])


def _offline_world_keeper(world, proposal, h):
    for m in proposal.get("moves", []):
        h["move_player"](**m)
    for t in proposal.get("transfers", []):
        h["transfer_item"](**t)
    for e in proposal.get("events", []):
        h["commit_event"](**e)


def offline_turn(world, player_id, text):
    scene = world.run("scene", {"pid": player_id}, readonly=True)[0]
    player = world.run("entity", {"id": player_id}, readonly=True)[0]
    pname = player["e"]["name"]
    loc = scene["l"]
    low = text.lower()
    decisions, commits = [], []
    _step(world, type="agent", agent="narrator", text="offline rules engine (set GROQ_API_KEY in .env for the LLM swarm)")

    if MOVE.search(text):
        for x in world.run("locations", readonly=True):
            if x["id"] != loc["id"] and (x["name"].lower().split()[-1] in low or x["name"].lower() in low):
                r = world_keeper(world, player_id, {"summary": f"{pname} travels to {x['name']}",
                                                    "moves": [{"player_id": player_id, "location_id": x["id"]}]})
                commits += r["committed"]
                via = next((c["to"] for c in commits if c["op"] == "move"), x["name"])
                return {"narration": f"You make your way to {via}. {x['description']}", "decisions": [], "commits": commits}
        return {"narration": "You can't get there from here. Exits: " + ", ".join(x["name"] for x in scene["exits"]),
                "decisions": [], "commits": []}

    target = next((n for n in scene["npcs"] if n["name"].split()[0].lower() in low or n["name"].lower() in low), None)
    if not target:
        info = world.run("find_by_name", {"name": next((w for w in re.findall(r"[A-Z][a-z]+", text)), "zzz")}, readonly=True)
        if info and info[0]["label"] == "NPC":
            where = world.run("entity", {"id": info[0]["id"]}, readonly=True)[0]["location"]
            _step(world, type="blocked", agent="world-keeper", text=f"{info[0]['name']} is at {where}, not here")
            return {"narration": f"{info[0]['name']} isn't here; word is they're at {where}.", "decisions": [], "commits": []}
        return {"narration": f"{loc['description']} Present: " + (", ".join(n["name"] for n in scene["npcs"]) or "nobody") + ".",
                "decisions": [], "commits": []}

    events, transfers = [], []
    if GIVE.search(text):
        for it in player["owns"]:
            if any(w in low for w in it.lower().split() if len(w) > 3):
                iid = world.raw("MATCH (i:Item {name:$n}) RETURN i.id", {"n": it})[0][0]
                transfers.append({"item_id": iid, "from_id": player_id, "to_id": target["id"]})
                events.append({"description": f"{pname} gave {it} to {target['name']}", "kind": "gift", "sentiment": 1,
                               "actor_ids": [player_id], "target_id": target["id"], "location_id": loc["id"]})
    if APOLOGY.search(text):
        events.append({"description": f"{pname} sincerely apologised to {target['name']}", "kind": "apology", "sentiment": 2,
                       "actor_ids": [player_id], "target_id": target["id"], "location_id": loc["id"]})
    elif INSULT.search(text):
        events.append({"description": f"{pname} insulted {target['name']}: \"{text[:80]}\"", "kind": "insult", "sentiment": -2,
                       "actor_ids": [player_id], "target_id": target["id"], "location_id": loc["id"]})
    elif FRIENDLY.search(text) and not re.search(r"\b(can|could|will|would)\b.*\?", low) and "please" not in low:
        events.append({"description": f"{pname} helped {target['name']}", "kind": "help", "sentiment": 1,
                       "actor_ids": [player_id], "target_id": target["id"], "location_id": loc["id"]})

    if events or transfers:
        r = world_keeper(world, player_id, {"summary": text, "events": events, "transfers": transfers})
        commits += r["committed"]
    _step(world, type="handoff", frm="narrator", to=f"npc:{target['name']}", text=text)
    d = npc_agent(world, target["id"], player_id, text, text)
    decisions.append(d)
    return {"narration": f"{target['name']} looks up at you. \"{d['dialogue']}\"", "decisions": decisions, "commits": commits}


# --------------------------------------------------------------------------- entry point

def play_turn(world: World, player_id: str, text: str):
    world.trace = []
    player = world.run("entity", {"id": player_id}, readonly=True)[0]["e"]
    world.record_message("player", player["name"], text)
    mode = "offline"
    if llm_enabled():
        try:
            result, mode = narrator_turn(world, player_id, text), last_model
        except LLMUnavailable as ex:
            _step(world, type="blocked", agent="narrator", text=f"{ex}; this turn uses the offline rules engine")
            token = _force_offline.set(True)
            try:
                result = offline_turn(world, player_id, text)
            finally:
                _force_offline.reset(token)
    else:
        result = offline_turn(world, player_id, text)
    world.record_message("narrator", "Narrator", result["narration"])
    result["trace"], world.trace = world.trace, None
    result["mode"] = mode
    return result


def explain(world: World, decision_id: str):
    meta = world.run("decision_meta", {"did": decision_id}, readonly=True)
    if not meta:
        return None
    paths = world.run("explain_decision", {"did": decision_id}, readonly=True)
    m = meta[0]
    d = m["d"]
    return {"decision_id": decision_id, "npc": m["npc"], "player": m["player"], "verdict": d["verdict"],
            "reasoning": d["reasoning"], "request": d["request"], "session": d["session"],
            "rules": m["rules"], "events": [e for e in m["events"] if e.get("id")],
            "paths": [p["path"] for p in paths], "cypher": QUERIES["explain_decision"].strip()}
