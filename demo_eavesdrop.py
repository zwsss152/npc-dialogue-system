"""
Eavesdrop System Demo
=====================

Walks through every rule of the eavesdrop mechanic in five scenarios:

    1. Too far          — you can see their mouths move, you can't hear a word
    2. Too loud         — running into the circle kills the conversation
    3. Too close        — standing in their face gets you noticed
    4. Cover works      — same position, crouched and behind cover, it works
    5. The payoff       — listen, record intel, and unlock the finale

Runs with no LLM. If Ollama is not up, the conversation engine falls back to
template responses, which is enough to demonstrate the whole flow.

Usage:
    python demo_eavesdrop.py
"""

import asyncio
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from npc_dialogue import NPCManager
from npc_conversation import ConversationManager
from eavesdrop import EavesdropManager, Posture

PLAYER = "demo_player"
CARDS = Path("character_cards")
SPOTS = Path("eavesdrop_spots.json")


def rule(title: str) -> None:
    print()
    print("=" * 68)
    print(f"  {title}")
    print("=" * 68)


def show_check(mgr: EavesdropManager, spot_id: str) -> bool:
    avail = mgr.check(spot_id, PLAYER)
    mark = "OK  " if avail.can_eavesdrop else "NO  "
    print(f"  [{mark}] {avail.name:<28} dist={avail.distance:>6.1f}")
    print(f"         -> {avail.reason}")
    return avail.can_eavesdrop


def build() -> tuple[EavesdropManager, ConversationManager]:
    if not CARDS.exists() or not any(CARDS.glob("*.json")):
        raise SystemExit("character_cards/ not found — run this from the project root")

    manager = NPCManager()
    for path in sorted(CARDS.glob("*.json")):
        manager.load_character(str(path), player_id=PLAYER)

    conversations = ConversationManager(npc_manager=manager)
    eavesdrop = EavesdropManager(conversations)
    if SPOTS.exists():
        eavesdrop.load_spots(SPOTS)
    else:
        raise SystemExit("eavesdrop_spots.json not found")

    return eavesdrop, conversations


async def main() -> int:
    eavesdrop, conversations = build()

    print()
    print("NPCs loaded:", ", ".join(conversations.npc_manager.npcs.keys()))
    print("Spots loaded:", len(eavesdrop.spots))
    print(f"Ambient chatter chance per location tick: {conversations.ambient_chance:.0%}")

    target = "spot_forge_alley"

    # ---------------------------------------------------------------- 1
    rule("1. Too far away")
    eavesdrop.set_presence(PLAYER, (200, 200), posture=Posture.WALK, location="Ironhold Village")
    show_check(eavesdrop, target)
    print("\n  The spot exists, but you are nowhere near it. Nothing to hear.")

    # ---------------------------------------------------------------- 2
    rule("2. In range, but running")
    eavesdrop.set_presence(PLAYER, (25, 25), posture=Posture.RUN, location="Ironhold Village")
    show_check(eavesdrop, target)
    print("\n  You are standing in the right place. You are also sprinting,")
    print("  which multiplies every NPC's notice radius by "
          f"{Posture.NOISE[Posture.RUN]:.1f}x. They stop talking.")

    # ---------------------------------------------------------------- 3
    rule("3. Walk in, but stand right next to them")
    eavesdrop.set_presence(PLAYER, (21.5, 25.0), posture=Posture.WALK, location="Ironhold Village")
    show_check(eavesdrop, target)
    anchor = eavesdrop.spots[target].npc_anchor
    print(f"\n  Thorne and Elara are standing at {anchor}.")
    print("  Stood in the middle of the spot you are 6 units away — invisible.")
    print("  Two steps toward them and you cross the notice radius. The whole")
    print("  mechanic is that gap: close enough to hear, far enough to be ignored.")

    # ---------------------------------------------------------------- 4
    rule("4. Correct position, walking")
    eavesdrop.set_presence(PLAYER, (25, 25), posture=Posture.WALK, location="Ironhold Village")
    show_check(eavesdrop, target)

    print()
    print("  Notice radius by posture, at this spot:")
    spot = eavesdrop.spots[target]
    for posture in Posture.ALL:
        r = eavesdrop._detection_radius(spot, posture, False)
        r_cov = eavesdrop._detection_radius(spot, posture, True)
        print(f"    {posture:<8} exposed {r:>5.2f}   in cover {r_cov:>5.2f}")

    # ---------------------------------------------------------------- 5
    rule("5. Press the button — and hear something worth hearing")
    result = await eavesdrop.eavesdrop(target, PLAYER)
    if not result["ok"]:
        print("  Failed:", result["reason"])
        return 1

    print(f"  Spot   : {result['spot_name']}")
    print(f"  Topic  : {result['topic']}")
    print(f"  Pair   : {' & '.join(result['npc_pair'])}")
    print()
    for line in result["transcript"]:
        print(f"    {line['speaker']}: {line['message']}")
        print()
    if result["intel"]:
        print("  ── Recorded ──")
        print(f"  {result['intel']['text']}")

    # ---------------------------------------------------------------- 6
    rule("6. It is spent — you cannot farm the same corner twice")
    show_check(eavesdrop, target)

    # ---------------------------------------------------------------- 7
    rule("7. Gathering the rest of the thread")
    for spot_id in ("spot_market_ledger", "spot_tower_landing"):
        eavesdrop.set_presence(PLAYER, eavesdrop.spots[spot_id].position,
                               posture=Posture.WALK, location="Ironhold Village")
        res = await eavesdrop.eavesdrop(spot_id, PLAYER)
        print(f"\n  {res.get('spot_name', spot_id)}")
        if res["ok"]:
            for line in res["transcript"][:2]:
                print(f"    {line['speaker']}: {line['message']}")
            if res["intel"]:
                print(f"    -> recorded: {res['intel']['intel_id']}")
        else:
            print(f"    blocked: {res['reason']}")

    # ---------------------------------------------------------------- 8
    rule("8. The finale was locked the whole time")
    finale = eavesdrop.spots["spot_rampart_gap"]
    eavesdrop.set_presence(PLAYER, finale.position, posture=Posture.WALK,
                           concealed=False, location="Ironhold Village")
    show_check(eavesdrop, "spot_rampart_gap")
    print("\n  All three pieces are in hand now, but you are standing in the open.")

    eavesdrop.set_presence(PLAYER, finale.position, posture=Posture.CROUCH,
                           concealed=True, location="Ironhold Village")
    show_check(eavesdrop, "spot_rampart_gap")

    res = await eavesdrop.eavesdrop("spot_rampart_gap", PLAYER)
    if res["ok"]:
        print()
        for line in res["transcript"]:
            print(f"    {line['speaker']}: {line['message']}")
            print()
        if res["intel"]:
            print("  ── Recorded ──")
            print(f"  {res['intel']['text']}")

    # ---------------------------------------------------------------- 9
    rule("What the rest of the game gets out of this")
    st = eavesdrop.status(PLAYER)
    print(f"  Spots heard : {len(st['spots_consumed'])} / {st['spots_total']}")
    print(f"  Intel count : {st['intel_count']}")
    print()
    print("  This brief is ready to be pasted straight into an NPC's system prompt,")
    print("  which is how overheard information becomes new dialogue options:")
    print()
    for line in eavesdrop.intel_brief(PLAYER).strip().splitlines():
        print("   ", line)

    print()
    print("=" * 68)
    print("  Done. Serve the map UI with:")
    print("      python -m uvicorn api_server:app --port 8000")
    print("      open http://localhost:8000/eavesdrop")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
