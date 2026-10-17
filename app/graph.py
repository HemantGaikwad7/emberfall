"""FalkorDB access layer: the shared memory every agent reads and writes.

Every Cypher statement the product depends on lives in QUERIES so it can be
listed in the README / UI and every execution is recorded in the turn trace.
"""
import os
import threading
import time
import uuid

from falkordb import FalkorDB
from falkordb.edge import Edge
from falkordb.node import Node
from falkordb.path import Path

_db = None
_db_lock = threading.Lock()


def db():
    global _db
    with _db_lock:
        if _db is None:
            if os.getenv("FALKORDB_URL"):  # e.g. falkors://user:pass@host:port from FalkorDB Cloud
                _db = FalkorDB.from_url(os.environ["FALKORDB_URL"])
            else:
                _db = FalkorDB(
                    host=os.getenv("FALKORDB_HOST", "localhost"),
                    port=int(os.getenv("FALKORDB_PORT", "6379")),
                    username=os.getenv("FALKORDB_USERNAME") or None,
                    password=os.getenv("FALKORDB_PASSWORD") or None,
                    ssl=os.getenv("FALKORDB_SSL", "").lower() in ("1", "true", "yes"),
                )
        return _db


def new_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


QUERIES = {
    # ---------- reads: scene & world facts ----------
    "scene": """
        MATCH (p:Player {id:$pid})-[:LOCATED_AT]->(l:Location)
        OPTIONAL MATCH (n:NPC)-[:LOCATED_AT]->(l)
        OPTIONAL MATCH (l)-[:CONNECTED_TO]-(x:Location)
        OPTIONAL MATCH (other:Player)-[:LOCATED_AT]->(l) WHERE other.id <> $pid
        RETURN l, collect(DISTINCT n) AS npcs, collect(DISTINCT x) AS exits,
               collect(DISTINCT other) AS party""",
    "entity": """
        MATCH (e {id:$id})
        OPTIONAL MATCH (e)-[:LOCATED_AT]->(l:Location)
        OPTIONAL MATCH (e)-[:OWNS]->(i:Item)
        OPTIONAL MATCH (owner)-[:OWNS]->(e)
        RETURN e, labels(e)[0] AS label, l.name AS location,
               collect(DISTINCT i.name) AS owns, owner.name AS owned_by""",
    "find_by_name": """
        MATCH (e) WHERE (e:NPC OR e:Location OR e:Item OR e:Player OR e:Faction)
          AND toLower(e.name) CONTAINS toLower($name)
        RETURN e.id AS id, e.name AS name, labels(e)[0] AS label LIMIT 5""",
    # ---------- NPC semantic + procedural memory ----------
    "npc_profile": """
        MATCH (n:NPC {id:$nid})
        OPTIONAL MATCH (n)-[:MEMBER_OF]->(f:Faction)
        OPTIONAL MATCH (n)-[:LOCATED_AT]->(l:Location)
        RETURN n, f.name AS faction, f.id AS faction_id, l.name AS location""",
    "npc_relationships": """
        MATCH (n:NPC {id:$nid})
        OPTIONAL MATCH (n)-[k:KIN_OF]-(kin:NPC)
        OPTIONAL MATCH (n)-[:MEMBER_OF]->(:Faction)-[a:ALLIED_WITH|HOSTILE_TO]-(f2:Faction)
        RETURN collect(DISTINCT {name: kin.name, id: kin.id, relation: k.relation,
                                 direction: CASE WHEN startNode(k) = n THEN 'out' ELSE 'in' END}) AS kin,
               collect(DISTINCT {faction: f2.name, stance: type(a)}) AS politics""",
    "npc_rules": """
        MATCH (n:NPC {id:$nid})
        MATCH (r:Rule)-[:GOVERNS]->(t)
        WHERE t = n OR (n)-[:MEMBER_OF]->(t)
        RETURN r.id AS id, r.text AS text""",
    "world_rules": "MATCH (r:Rule {scope:'world'}) RETURN r.id AS id, r.text AS text",
    # ---------- NPC episodic memory ----------
    "npc_memories": """
        MATCH (n:NPC {id:$nid})-[m:REMEMBERS]->(e:Event)<-[:PERFORMED]-(p:Player {id:$pid})
        MATCH (e)-[:IN_SESSION]->(s:Session)
        OPTIONAL MATCH (e)-[:TARGETED]->(t)
        OPTIONAL MATCH (e)-[:AT]->(l:Location)
        RETURN e.id AS event_id, e.description AS what, e.kind AS kind,
               e.sentiment AS sentiment, s.number AS session, m.via AS learned_via,
               m.weight AS weight, t.name AS target, l.name AS place
        ORDER BY s.number, e.ts""",
    "npc_attitude": """
        MATCH (n:NPC {id:$nid})
        OPTIONAL MATCH (n)-[f:FEELS]->(p:Player {id:$pid})
        RETURN coalesce(f.score, 0.0) AS score""",
    # ---------- writes: events & memory propagation (World-keeper) ----------
    "create_event": """
        MATCH (s:Session {id:$sid})
        CREATE (e:Event {id:$eid, description:$desc, kind:$kind, sentiment:$sentiment,
                         ts:$ts, session:s.number})
        CREATE (e)-[:IN_SESSION]->(s)
        RETURN e.id""",
    "link_actor": "MATCH (p:Player {id:$pid}), (e:Event {id:$eid}) CREATE (p)-[:PERFORMED]->(e)",
    "link_target": "MATCH (t {id:$tid}), (e:Event {id:$eid}) CREATE (e)-[:TARGETED]->(t)",
    "link_place": "MATCH (l:Location {id:$lid}), (e:Event {id:$eid}) CREATE (e)-[:AT]->(l)",
    "remember_victim": """
        MATCH (e:Event {id:$eid})-[:TARGETED]->(t:NPC)
        MERGE (t)-[r:REMEMBERS]->(e) ON CREATE SET r.via = 'firsthand', r.weight = 1.0""",
    "remember_witness": """
        MATCH (e:Event {id:$eid})-[:AT]->(l:Location)<-[:LOCATED_AT]-(w:NPC)
        MERGE (w)-[r:REMEMBERS]->(e) ON CREATE SET r.via = 'witness', r.weight = 0.6""",
    "remember_kin": """
        MATCH (e:Event {id:$eid})-[:TARGETED]->(:NPC)-[:KIN_OF]-(k:NPC)
        MERGE (k)-[r:REMEMBERS]->(e) ON CREATE SET r.via = 'kin', r.weight = 0.9""",
    "remember_faction": """
        MATCH (e:Event {id:$eid})-[:TARGETED]->(:NPC)-[:MEMBER_OF]->(f:Faction)<-[:MEMBER_OF]-(m:NPC)
        MERGE (m)-[r:REMEMBERS]->(e) ON CREATE SET r.via = 'faction', r.weight = 0.4""",
    "update_attitudes": """
        MATCH (n:NPC)-[r:REMEMBERS]->(e:Event {id:$eid})<-[:PERFORMED]-(p:Player)
        MERGE (n)-[f:FEELS]->(p) ON CREATE SET f.score = 0.0
        SET f.score = f.score + e.sentiment * r.weight
        RETURN n.name AS npc, p.name AS player, f.score AS score""",
    # Shortest road route between the player and a destination (variable-length path, shortest first).
    "route": """
        MATCH (p:Player {id:$pid})-[:LOCATED_AT]->(a:Location), (b:Location {id:$lid})
        MATCH path = (a)-[:CONNECTED_TO*1..8]-(b)
        RETURN [n IN nodes(path) | n.name] AS route, length(path) AS hops ORDER BY hops ASC LIMIT 1""",
    "move_player": """
        MATCH (p:Player {id:$pid})-[old:LOCATED_AT]->(:Location), (b:Location {id:$lid})
        DELETE old CREATE (p)-[:LOCATED_AT]->(b) RETURN b.name""",
    "locations": "MATCH (l:Location) RETURN l.id AS id, l.name AS name, l.description AS description",
    "move_npc": """
        MATCH (n:NPC {id:$nid})-[old:LOCATED_AT]->(:Location), (b:Location {id:$lid})
        DELETE old CREATE (n)-[:LOCATED_AT]->(b) RETURN b.name""",
    "transfer_item": """
        MATCH (from {id:$from_id})-[o:OWNS]->(i:Item {id:$item_id}), (to {id:$to_id})
        DELETE o CREATE (to)-[:OWNS]->(i) RETURN i.name""",
    "set_alive": "MATCH (n:NPC {id:$nid}) SET n.alive = $alive RETURN n.name",
    # ---------- decisions (traceability) ----------
    "create_decision": """
        MATCH (n:NPC {id:$nid}), (p:Player {id:$pid}), (s:Session {id:$sid})
        CREATE (d:Decision {id:$did, verdict:$verdict, reasoning:$reasoning,
                            request:$request, ts:$ts, session:s.number})
        CREATE (n)-[:MADE]->(d), (d)-[:TOWARD]->(p), (d)-[:IN_SESSION]->(s)
        RETURN d.id""",
    "decision_evidence": """
        MATCH (d:Decision {id:$did}), (e:Event) WHERE e.id IN $eids
        CREATE (d)-[:BASED_ON]->(e)""",
    "decision_rules": """
        MATCH (d:Decision {id:$did}), (r:Rule) WHERE r.id IN $rids
        CREATE (d)-[:APPLIED]->(r)""",
    # The wow query: the exact chain from the player's deed to the NPC who judges them.
    "explain_decision": """
        MATCH (n:NPC)-[:MADE]->(d:Decision {id:$did})-[:TOWARD]->(p:Player)
        MATCH (d)-[:BASED_ON]->(e:Event)
        MATCH path = (p)-[:PERFORMED]->(e)-[:TARGETED]->(t)-[:KIN_OF|MEMBER_OF*0..2]-(n)
        RETURN path, e.id AS event_id
        ORDER BY length(path) ASC""",
    "decision_meta": """
        MATCH (n:NPC)-[:MADE]->(d:Decision {id:$did})-[:TOWARD]->(p:Player)
        OPTIONAL MATCH (d)-[:APPLIED]->(r:Rule)
        OPTIONAL MATCH (d)-[:BASED_ON]->(e:Event)
        OPTIONAL MATCH (n)-[m:REMEMBERS]->(e)
        RETURN d, n.name AS npc, n.id AS npc_id, p.name AS player, p.id AS player_id,
               collect(DISTINCT r.text) AS rules,
               collect(DISTINCT {id: e.id, what: e.description, session: e.session, via: m.via}) AS events""",
    # ---------- sessions & persistent conversation history ----------
    "active_session": "MATCH (s:Session {status:'active'}) RETURN s.id AS id, s.number AS number",
    "sessions": "MATCH (s:Session) RETURN s.id AS id, s.number AS number, s.status AS status, s.summary AS summary ORDER BY s.number",
    "close_session": """
        MATCH (s:Session {id:$sid}) SET s.status = 'closed', s.summary = $summary, s.ended = $ts
        CREATE (n:Session {id:$nid, number: s.number + 1, status:'active', started:$ts})
        CREATE (s)-[:NEXT]->(n) RETURN n.number""",
    "add_message": """
        MATCH (s:Session {id:$sid})
        CREATE (m:Message {id:$mid, agent:$agent, speaker:$speaker, text:$text, ts:$ts})
        CREATE (m)-[:IN_SESSION]->(s)""",
    "recent_messages": """
        MATCH (m:Message)-[:IN_SESSION]->(s:Session {id:$sid})
        RETURN m.agent AS agent, m.speaker AS speaker, m.text AS text ORDER BY m.ts DESC LIMIT $limit""",
    "session_events": """
        MATCH (e:Event)-[:IN_SESSION]->(s:Session {id:$sid})
        OPTIONAL MATCH (p:Player)-[:PERFORMED]->(e)
        RETURN e.description AS what, e.sentiment AS sentiment, collect(p.name) AS by, e.ts AS ts ORDER BY ts""",
}


def _plain(v):
    """Convert FalkorDB result values into JSON-friendly structures."""
    if isinstance(v, Node):
        return {"_node": v.id, "labels": v.labels, **v.properties}
    if isinstance(v, Edge):
        return {"_edge": v.id, "type": v.relation, "src": v.src_node, "dst": v.dest_node, **v.properties}
    if isinstance(v, Path):
        return {"nodes": [_plain(n) for n in v.nodes()], "edges": [_plain(e) for e in v.edges()]}
    if isinstance(v, list):
        return [_plain(x) for x in v]
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    return v


class World:
    """One campaign == one FalkorDB graph (tenant isolation: memory never leaks between tables)."""

    def __init__(self, campaign: str):
        self.campaign = campaign
        self.graph = db().select_graph(f"dnd_{campaign}")
        self.trace = None  # list set per request so every Cypher call shows up in the UI

    def run(self, name, params=None, readonly=False, **fmt):
        cypher = QUERIES[name]
        params = params or {}
        t0 = time.perf_counter()
        res = (self.graph.ro_query if readonly else self.graph.query)(cypher, params)
        if self.trace is not None:
            self.trace.append({"type": "cypher", "name": name, "params": params,
                               "ms": round((time.perf_counter() - t0) * 1000, 1), "rows": len(res.result_set)})
        cols = [h[1] if isinstance(h, (list, tuple)) else h for h in res.header]
        return [dict(zip(cols, [_plain(v) for v in row])) for row in res.result_set]

    def raw(self, cypher, params=None):
        return self.graph.query(cypher, params or {}).result_set

    # ---- convenience helpers used by agents ----
    def active_session(self):
        rows = self.run("active_session", readonly=True)
        return rows[0] if rows else None

    def record_message(self, agent, speaker, text):
        s = self.active_session()
        self.run("add_message", {"sid": s["id"], "mid": new_id("msg"), "agent": agent,
                                 "speaker": speaker, "text": text, "ts": time.time()})

    def commit_event(self, description, kind, sentiment, actor_ids, target_id=None, location_id=None):
        """Write an event and propagate memory of it through the social graph."""
        s = self.active_session()
        eid = new_id("evt")
        self.run("create_event", {"sid": s["id"], "eid": eid, "desc": description, "kind": kind,
                                  "sentiment": float(sentiment), "ts": time.time()})
        for pid in actor_ids:
            self.run("link_actor", {"pid": pid, "eid": eid})
        if target_id:
            self.run("link_target", {"tid": target_id, "eid": eid})
        if location_id:
            self.run("link_place", {"lid": location_id, "eid": eid})
        for q in ("remember_victim", "remember_witness", "remember_kin", "remember_faction"):
            self.run(q, {"eid": eid})
        attitudes = self.run("update_attitudes", {"eid": eid})
        return {"event_id": eid, "session": s["number"], "attitude_changes": attitudes}

    def graph_dump(self, include_messages=False):
        where = "" if include_messages else "WHERE NOT n:Message"
        nodes = self.raw(f"MATCH (n) {where} RETURN n")
        edge_where = "" if include_messages else "WHERE NOT a:Message AND NOT b:Message"
        edges = self.raw(f"MATCH (a)-[r]->(b) {edge_where} RETURN r")
        return {"nodes": [_plain(r[0]) for r in nodes], "edges": [_plain(r[0]) for r in edges]}

    def ensure_indexes(self):
        for label in ("Player", "NPC", "Location", "Item", "Faction", "Event", "Session",
                      "Decision", "Rule", "Message"):
            try:
                self.graph.query(f"CREATE INDEX FOR (n:{label}) ON (n.id)")
            except Exception:
                pass  # index already exists
