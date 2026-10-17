"""Seed the town of Emberfall.

mode="demo"  -> sessions 1 & 2 already played (Kael insulted Borin in session 1), session 3 active.
mode="fresh" -> same world, session 1 active, no history: play the grudge in live.
"""
import time

from .graph import World

WORLD = """
CREATE
 (square:Location {id:'loc_square', name:'Emberfall Square', description:'A cobbled square around a soot-blackened fountain. Notices for the Watch flutter on a post.'}),
 (anvil:Location {id:'loc_anvil', name:'The Rusty Anvil', description:'Grimma Ironroot\\'s forge. Heat, sparks and the smell of quenched steel.'}),
 (tavern:Location {id:'loc_tavern', name:'The Gilded Goose', description:'A loud, low-beamed tavern where every rumour in Emberfall is born.'}),
 (road:Location {id:'loc_road', name:'Old Mill Road', description:'A muddy road past the burnt-out mill, heading toward the hills.'}),
 (mine:Location {id:'loc_mine', name:'Ironroot Mine', description:'The dwarven mine. Something has been killing miners in the deep shafts.'}),
 (square)-[:CONNECTED_TO]->(anvil), (square)-[:CONNECTED_TO]->(tavern),
 (square)-[:CONNECTED_TO]->(road), (road)-[:CONNECTED_TO]->(mine),

 (clan:Faction {id:'fac_ironroot', name:'Ironroot Clan', description:'Dwarven miners and smiths bound by blood-oath.'}),
 (watch:Faction {id:'fac_watch', name:'Emberfall Watch', description:'The town guard.'}),
 (hand:Faction {id:'fac_hand', name:'Crimson Hand', description:'A thieves\\' guild operating out of the Gilded Goose.'}),
 (clan)-[:ALLIED_WITH]->(watch), (hand)-[:HOSTILE_TO]->(watch),

 (grimma:NPC {id:'npc_grimma', name:'Grimma Ironroot', pronouns:'she/her', role:'master blacksmith', alive:true,
   persona:'Gruff, proud, fiercely loyal to family. Only she can forge moonsteel. Speaks in short, hard sentences.'}),
 (borin:NPC {id:'npc_borin', name:'Borin Ironroot', pronouns:'he/him', role:'miner', alive:true,
   persona:'Grimma\\'s younger brother. Good-natured but thin-skinned about his soot-stained face.'}),
 (aldric:NPC {id:'npc_aldric', name:'Captain Aldric Vane', pronouns:'he/him', role:'captain of the Watch', alive:true,
   persona:'Tired, honest, by-the-book. Rewards people who keep the peace.'}),
 (mira:NPC {id:'npc_mira', name:'Mira Quickfingers', pronouns:'she/her', role:'fence and informant', alive:true,
   persona:'Charming, transactional, always knows who owes whom.'}),
 (tess:NPC {id:'npc_tess', name:'Old Tess', pronouns:'she/her', role:'innkeeper of the Gilded Goose', alive:true,
   persona:'Warm grandmother figure who never forgets a kindness or a broken mug.'}),

 (borin)-[:KIN_OF {relation:'brother'}]->(grimma),
 (grimma)-[:MEMBER_OF]->(clan), (borin)-[:MEMBER_OF]->(clan),
 (aldric)-[:MEMBER_OF]->(watch), (mira)-[:MEMBER_OF]->(hand),

 (grimma)-[:LOCATED_AT]->(anvil), (borin)-[:LOCATED_AT]->(tavern),
 (aldric)-[:LOCATED_AT]->(square), (mira)-[:LOCATED_AT]->(tavern), (tess)-[:LOCATED_AT]->(tavern),

 (ore:Item {id:'itm_ore', name:'Moonsteel Ore', description:'Glows faintly. Only an Ironroot smith can work it.'}),
 (hammer:Item {id:'itm_hammer', name:'Masterwork Warhammer'}),
 (badge:Item {id:'itm_badge', name:'Watch Badge'}),
 (ledger:Item {id:'itm_ledger', name:'Stolen Ledger'}),
 (ale:Item {id:'itm_ale', name:'Cask of Dwarven Ale'}),
 (borin)-[:OWNS]->(ore), (grimma)-[:OWNS]->(hammer), (aldric)-[:OWNS]->(badge),
 (mira)-[:OWNS]->(ledger), (tess)-[:OWNS]->(ale),

 (kael:Player {id:'pc_kael', name:'Kael the Bard', pronouns:'he/him', description:'A silver-tongued half-elf bard with a cruel streak of wit.'}),
 (lyra:Player {id:'pc_lyra', name:'Lyra Dawnshield', pronouns:'she/her', description:'An earnest human paladin.'}),
 (kael)-[:LOCATED_AT]->(tavern), (lyra)-[:LOCATED_AT]->(tavern),
 (gold:Item {id:'itm_gold', name:'Pouch of 50 Gold'}), (lute:Item {id:'itm_lute', name:'Silver Lute'}),
 (potion:Item {id:'itm_potion', name:'Healing Potion'}),
 (kael)-[:OWNS]->(gold), (kael)-[:OWNS]->(lute), (lyra)-[:OWNS]->(potion),

 (r1:Rule {id:'rule_kin_oath', scope:'faction', text:'Kin-oath: an insult to one Ironroot is an insult to every Ironroot. Clan members refuse service to anyone who has wronged their kin until amends are made (a sincere apology AND a gift to the wronged dwarf).'}),
 (r2:Rule {id:'rule_watch', scope:'faction', text:'Watch code: help anyone with no record of crimes in Emberfall; arrest thieves on sight.'}),
 (r3:Rule {id:'rule_hand', scope:'faction', text:'Thieves\\' cant: trust is bought. Anyone who has done the Hand a favour is owed one back.'}),
 (r4:Rule {id:'rule_tess', scope:'npc', text:'Tess feeds anyone who has ever shown her kindness, free of charge, forever.'}),
 (r1)-[:GOVERNS]->(clan), (r2)-[:GOVERNS]->(watch), (r3)-[:GOVERNS]->(hand), (r4)-[:GOVERNS]->(tess),

 (:Rule {id:'rule_w1', scope:'world', text:'Dead NPCs cannot speak, trade or act.'}),
 (:Rule {id:'rule_w2', scope:'world', text:'Every item has exactly one owner; items only change hands via an explicit transfer.'}),
 (:Rule {id:'rule_w3', scope:'world', text:'Characters can only speak with NPCs at their own location.'}),
 (:Rule {id:'rule_w4', scope:'world', text:'Travel happens only along CONNECTED_TO roads; travellers take the shortest route.'}),
 (:Rule {id:'rule_w5', scope:'world', text:'Only an Ironroot smith can forge Moonsteel Ore, and she needs the ore in hand.'})
"""


def _say(w, agent, speaker, text):
    w.record_message(agent, speaker, text)
    time.sleep(0.002)  # keep message timestamps strictly ordered


def _close(w, summary):
    s = w.active_session()
    w.run("close_session", {"sid": s["id"], "summary": summary, "nid": f"ses_{s['number'] + 1}", "ts": time.time()})


def seed(campaign: str, mode: str = "demo"):
    w = World(campaign)
    try:
        w.graph.delete()
    except Exception:
        pass
    w = World(campaign)
    w.raw(WORLD)
    w.raw("CREATE (:Session {id:'ses_1', number:1, status:'active', started:$ts})", {"ts": time.time()})
    w.ensure_indexes()
    if mode == "fresh":
        return {"campaign": campaign, "mode": mode}

    # ---------------- Session 1 ----------------
    _say(w, "narrator", "Narrator", "Rain hammers the shutters of the Gilded Goose. Kael and Lyra shake off their cloaks as a soot-streaked dwarf laughs too loudly at the bar.")
    _say(w, "player", "Kael the Bard", "I stand on a table and sing about Borin: 'a soot-faced tunnel rat who smells of his own mine'.")
    _say(w, "npc", "Borin Ironroot", "You'll regret that song, elf. Ironroots don't forget.")
    w.commit_event("Kael publicly mocked Borin Ironroot as a 'soot-faced tunnel rat' in song", "insult", -3,
                   ["pc_kael"], "npc_borin", "loc_tavern")
    _say(w, "player", "Lyra Dawnshield", "I grab the pickpocket reaching into Old Tess's apron.")
    _say(w, "npc", "Old Tess", "Bless you, child. You'll never pay for a meal under my roof.")
    w.commit_event("Lyra caught a pickpocket robbing Old Tess", "help", 2, ["pc_lyra"], "npc_tess", "loc_tavern")
    _close(w, "The party arrived in Emberfall. At the Gilded Goose Kael humiliated Borin Ironroot with a mocking song; "
              "Borin swore the Ironroots would not forget. Lyra saved Old Tess from a pickpocket.")

    # Between sessions Borin goes back to the mine; the party sleeps at the tavern then heads out.
    w.run("move_npc", {"nid": "npc_borin", "lid": "loc_mine"})

    # ---------------- Session 2 ----------------
    _say(w, "narrator", "Narrator", "Captain Vane posts a bounty: giant rats are attacking carts on the Old Mill Road.")
    w.raw("MATCH (p:Player)-[r:LOCATED_AT]->() DELETE r")
    w.raw("MATCH (p:Player), (l:Location {id:'loc_square'}) CREATE (p)-[:LOCATED_AT]->(l)")
    w.commit_event("Kael and Lyra cleared the giant rats off the Old Mill Road for Captain Vane", "help", 2,
                   ["pc_kael", "pc_lyra"], "npc_aldric", "loc_square")
    _say(w, "npc", "Captain Aldric Vane", "Good work. Emberfall owes you both.")
    w.commit_event("Kael returned a coin purse Mira Quickfingers had dropped, no questions asked", "favor", 1,
                   ["pc_kael"], "npc_mira", None)
    _close(w, "The party cleared the Old Mill Road of giant rats for Captain Vane and earned the Watch's thanks. "
              "Kael quietly returned Mira Quickfingers' lost purse.")

    # ---------------- Session 3 (active) ----------------
    _say(w, "narrator", "Narrator", "Session 3. Miners are dying in the deep shafts of Ironroot Mine. Captain Vane says only a moonsteel blade "
                                    "can cut the thing down there, and only Grimma Ironroot at the Rusty Anvil can forge one.")
    return {"campaign": campaign, "mode": mode}
