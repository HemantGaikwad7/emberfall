---
title: "She Refused Because I Insulted Her Brother Two Sessions Ago: Building AI NPCs With Graph Memory"
tags: falkordb, ai, agents, graphdatabase
---

# She Refused Because I Insulted Her Brother Two Sessions Ago

*How I built a swarm of AI game masters that share one memory graph, and why the graph, not the LLM, ended up being the interesting part.*

> Built for **Graph Hacks: Context for AI Agents** (WeMakeDevs × FalkorDB), Track 02: Agent Memory & Coordination.
> 🎮 Live demo: (https://emberfall-d55v.onrender.com/) · 💻 Code: https://github.com/HemantGaikwad7/emberfall · 🎬 Video: https://youtu.be/kyxHE9RDE-0?si=IZ3vXQ2UOuJQIFqb

---

## The moment

My bard, Kael, walks into a forge and asks Grimma Ironroot, the only smith in town who can work moonsteel, to forge a blade.

She refuses:

> *"No blade for you, Kael. Apologise to Borin and bring a gift, then we'll talk."*

Grimma was never in the tavern where, **two sessions earlier**, Kael sang a mocking song about a dwarf called Borin. But Borin is her brother. Word travelled.

Then I click **"Why?"** and the app draws the exact path through the database:

```
Kael ─PERFORMED→ [S1: mocked Borin in song] ─TARGETED→ Borin ─KIN_OF(brother)→ Grimma
```

This isn't the LLM explaining itself after the fact. It's a Cypher query over the facts that drove her decision. That moment is the whole project in one image: **agents that remember, coordinate, and can show their work.**

<img width="470" height="371" alt="image" src="https://github.com/user-attachments/assets/b1b3be05-b9ec-4b80-b3bd-ac5aff7917c9" />


## The problem with LLM memory

LLM-based characters usually "remember" one of two ways:

1. **Stuff the transcript into the prompt.** It works until the context fills up, and nothing connects what happened to *whom*.
2. **Vector search over past chats.** Good at finding *similar text*, bad at answering *"who would care about this, and why?"*

Grimma's grudge isn't similar text. It's a **relationship chain**: Kael did something to Borin, Borin is Grimma's brother, and Grimma's clan has an oath about kin. That's a graph problem, so I put the world in a graph.

## The architecture: three agents, one graph

Emberfall runs a tabletop RPG with three kinds of agent:

- **Narrator.** Reads the scene and story from the graph, narrates, and decides who should handle the player's action.
- **NPC agents.** One per character. Each decides **only from its own memories** in the graph.
- **World-keeper.** Owns consistency: validates proposed changes against world rules, then commits them.

The key design rule: **handoffs carry intent, never state.** When the Narrator hands off to Grimma, it passes only *"Kael asks you to forge a blade."* Grimma's agent then queries FalkorDB for what *she* knows. Every agent reads and writes the same graph, so there's no state to drift out of sync between agents.

```
Player ─▶ Narrator ──talk_to_npc──▶ NPC agent ──▶ writes Decision
                   ──travel / propose_world_change──▶ World-keeper ──▶ writes Events, moves, transfers
                              all of them ◀──▶ FalkorDB
```

## The graph model

```
(:Player)-[:PERFORMED]->(:Event {sentiment})-[:TARGETED]->(:NPC)
(:NPC)-[:KIN_OF {relation}]->(:NPC)          (:NPC)-[:MEMBER_OF]->(:Faction)
(:NPC)-[:REMEMBERS {via, weight}]->(:Event)  (:NPC)-[:FEELS {score}]->(:Player)
(:Rule)-[:GOVERNS]->(:Faction|:NPC)
(:NPC)-[:MADE]->(:Decision)-[:BASED_ON]->(:Event)   (:Decision)-[:APPLIED]->(:Rule)
(:Event|:Message)-[:IN_SESSION]->(:Session)-[:NEXT]->(:Session)
```

This covers the three classic memory types without any separate stores:

| Memory type | In the graph |
|---|---|
| **Episodic** (what happened) | `Event` nodes and `REMEMBERS` edges, which record *who* knows and *how* |
| **Semantic** (what it adds up to) | `FEELS` attitude scores and LLM-written session summaries |
| **Procedural** (how to behave) | `Rule` nodes such as the Ironroot kin-oath, linked to the factions they govern |

Conversation history is `Message` nodes chained to sessions, so a page refresh, or a new session, rebuilds everything from the graph.

## Memory that spreads like gossip

When the World-keeper commits an event, four small Cypher queries decide who learns about it:

```cypher
// the target remembers first-hand
MATCH (e:Event {id:$eid})-[:TARGETED]->(t:NPC)
MERGE (t)-[r:REMEMBERS]->(e) ON CREATE SET r.via = 'firsthand', r.weight = 1.0;

// family hears about it
MATCH (e:Event {id:$eid})-[:TARGETED]->(:NPC)-[:KIN_OF]-(k:NPC)
MERGE (k)-[r:REMEMBERS]->(e) ON CREATE SET r.via = 'kin', r.weight = 0.9;
```

Witnesses at the location (0.6) and faction-mates (0.4) follow the same pattern. Then every NPC who remembers it updates how they feel:

```cypher
MATCH (n:NPC)-[r:REMEMBERS]->(e:Event {id:$eid})<-[:PERFORMED]-(p:Player)
MERGE (n)-[f:FEELS]->(p) ON CREATE SET f.score = 0.0
SET f.score = f.score + e.sentiment * r.weight
```

A −3 insult to Borin becomes −2.7 for Grimma: she's angry, and the graph can say exactly why.

## Decisions you can trace

The NPC agent has a single tool, `decide`, which takes a verdict, dialogue, reasoning, and the **IDs of the memories and rules** that drove it. The code throws away any ID the NPC doesn't actually have, so a model that invents a grudge gets its evidence silently dropped.

The decision is saved as a node linked to its evidence. The "Why?" button runs:

```cypher
MATCH (n:NPC)-[:MADE]->(d:Decision {id:$did})-[:TOWARD]->(p:Player)
MATCH (d)-[:BASED_ON]->(e:Event)
MATCH path = (p)-[:PERFORMED]->(e)-[:TARGETED]->(t)-[:KIN_OF|MEMBER_OF*0..2]-(n)
RETURN path ORDER BY length(path) ASC
```

That variable-length match `*0..2` is the whole trick. It finds whether the NPC was hurt directly (0 hops), through family (1 hop), or through their clan (2 hops), and returns the actual nodes and edges for the UI to highlight.

## Redemption also travels through the graph

Here's the part I like most. Kael travels to the mine (the World-keeper finds the shortest road route with a path query), apologises to Borin and hands over a pouch of gold. The World-keeper checks that Kael *owns* the gold before transferring it, then records the apology.

The **same `KIN_OF` edge** that carried the insult to Grimma now carries the apology. Back at the forge, her memories hold both events, the kin-oath is satisfied, and she agrees to forge the blade. The "Why?" path for her new decision runs through the Session 3 apology.

No special-case code. Just memory flowing through relationships.

<img width="646" height="408" alt="image" src="https://github.com/user-attachments/assets/14cbcc00-069f-4d54-afc9-ba7684aa604c" />


## Keeping the world honest

LLMs love to improvise, so the World-keeper enforces rules stored as graph nodes:

- You can only talk to NPCs at your location. "Grimma isn't here; she's at the Rusty Anvil."
- Items have exactly one owner. You can't gift what you don't have.
- Travel follows `CONNECTED_TO` roads.

In one test run, the narrator tried to record *"Grimma forged the blade."* The World-keeper rejected it, because Grimma doesn't hold the Moonsteel Ore; her brother does. The graph caught a plot hole the LLM didn't.

## Multi-tenant by design

Each campaign is its own FalkorDB graph (`dnd_<campaign>`), so memory never leaks between tables of players. Turns within one campaign are serialised to keep the story linear, while campaigns run in parallel. Inside a campaign, memory is per player: Grimma refuses Kael but happily deals with Lyra, who never insulted anyone.

## The stack (all free)

- **FalkorDB** (Docker locally, FalkorDB Cloud free tier when deployed) holds all the state.
- **Groq** runs `openai/gpt-oss-120b`, with automatic fallback to other free models when one hits its daily limit.
- **FastAPI** plus one HTML page using vis-network for the live graph.
- An **offline rules engine** runs the same graph flow without an LLM, so the demo never breaks.

## What I learned

1. **Put relationships in the database, not the prompt.** The LLM is good at voice and judgement. The graph is good at *who knows what and why*. Splitting it that way made the agents simpler and more reliable.
2. **Handoffs should carry intent, not state.** Once every agent reads the graph itself, coordination bugs mostly disappear.
3. **Make the model cite IDs, then validate them.** Cheap, and it turns "explainable AI" from a claim into a query.
4. **Small FalkorDB lessons:** `shortestPath` doesn't support undirected traversals yet, so I used an undirected variable-length match ordered by `length(path)`. When ordering by path length while also returning `nodes(path)`, project the length to an alias first.
5. **Free tiers have limits.** I hit Groq's daily token cap during testing, so I added model fallback and an offline mode. Your demo should never depend on one quota.

## Try it

- 🎮 Play: https://emberfall-d55v.onrender.com/. Click **Load demo**, then use the suggestion buttons.
- 💻 Code and full Cypher: https://github.com/HemantGaikwad7/emberfall
- 🎬 2-minute demo: https://youtu.be/kyxHE9RDE-0?si=IZ3vXQ2UOuJQIFqb

Insult someone's brother. See how long they remember.

