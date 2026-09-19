# Eavesdrop System

> Stand in the right place. Don't get noticed. Then listen.

A deliberate-listening mechanic for NPC-to-NPC conversations. It turns ambient
chatter into something the player has to work for, and turns every overheard
conversation into a permanent piece of knowledge they can use later.

The reference point is the Spycraft pillar in *007: First Light* — eavesdropping
there is not a text box that plays when you walk past. It is a position you have
to find, a posture you have to hold, and information that changes what you can
say afterwards.

---

## 1. Why it is built this way

The project already had NPC-to-NPC conversations (`npc_conversation.py`), but
they were pure ambience: two NPCs in the same room rolled a dice every 30 seconds
and occasionally chatted about the weather. Nothing the player did changed it,
and nothing came out of it.

Eavesdropping fixes both ends:

| | Before | After |
|---|---|---|
| **Trigger** | 10% dice roll, random topic | Player walks to a designed spot, topic is authored |
| **Tension** | none | You must be close enough to hear and far enough not to be seen |
| **Payoff** | nothing | An intel entry that unlocks later dialogue and gates later spots |

Ambient chatter is still there — NPCs still talk on their own when the player is
elsewhere. It just stops being the only thing the system can do.

---

## 2. The three rules

### Rule 1 — Close enough to hear, far enough to be ignored

Every spot has two radii, and the gameplay lives in the gap between them:

```
        spot.radius  ──  you can hear anything inside this
        alert_radius ──  an NPC notices you inside this
```

Stand inside `radius` but outside `alert_radius` and you get the conversation.
Step forward and they stop talking.

### Rule 2 — What you are doing matters as much as where you are

The notice radius is scaled by posture, and by whether you are in cover:

| Posture | Multiplier |
|---|---|
| `crouch` | 0.5× |
| `stand` / `walk` | 1.0× |
| `run` | 2.5× |

Being `concealed` applies a further 0.4× on top. So crouching behind cover gives
you roughly an eighth of the detection footprint of sprinting in the open —
which is what makes a spot like *The Gap in the Rampart* (requires cover) a real
positioning problem rather than a waypoint.

Running is additionally listed in `blocked_postures`, so it hard-fails rather
than just making detection more likely. The reason string the player sees is
`"You were too loud — the conversation stopped."`

### Rule 3 — Listening has to leave a mark

Each spot carries an `intel_id` and `intel_text`. Finishing a conversation writes
an entry into the player's intel ledger, and `EavesdropManager.intel_brief()`
renders the whole ledger as a prompt fragment you can paste straight into an
NPC's system prompt:

```
THE PLAYER HAS OVERHEARD THE FOLLOWING (they may bring it up):
- Someone in Ironhold is buying up old weapons in bulk and having them melted down...
- Three crates entered Ironhold without passing any border station...
```

Spots can also require intel (`requires_intel`) or world flags (`requires_flags`),
which is how four separate conversations become one mystery the player assembles
in whatever order they like.

---

## 3. Running it

```bash
# everything offline, no model needed
python demo_eavesdrop.py

# interactive map UI
python -m uvicorn api_server:app --port 8000
# then open http://localhost:8000/eavesdrop
```

In the map UI: **WASD / arrow keys** to move, posture buttons to change stance,
**In cover** to toggle concealment, click a spot, press **Listen**.

---

## 4. API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/eavesdrop` | The map interface |
| `GET` | `/api/eavesdrop/map` | All spots + NPC positions + your current eligibility |
| `POST` | `/api/eavesdrop/presence` | Report player position / posture / cover |
| `GET` | `/api/eavesdrop/check/{spot_id}` | Can I listen right now, and if not, why |
| `POST` | `/api/eavesdrop/listen` | The button |
| `GET` | `/api/eavesdrop/session/{player_id}` | Last overheard transcript |
| `GET` | `/api/eavesdrop/status/{player_id}` | Presence, consumed spots, intel count |
| `GET` | `/api/eavesdrop/intel/{player_id}` | Intel list + prompt-ready brief |
| `POST` | `/api/eavesdrop/prefetch` | Pre-generate every transcript |

A failed `listen` is **not** an HTTP error. It returns `ok: false` plus the same
human-readable `reason` the check endpoint gives, because the UI already needs to
display that string.

---

## 5. Latency — read this before shipping it

A four-turn conversation on a local 1B model takes 10–20 seconds. Generating it
*after* the player presses the button means a 15-second freeze at the worst
possible moment.

Call `POST /api/eavesdrop/prefetch` (or `await manager.prefetch_all()`) when the
level loads. Transcripts are cached on the spot and served instantly afterwards.
Trade-off: prefetched dialogue reflects the world state at load time, not at
listen time. For a scene where the player is a bystander rather than a
participant, that is the right trade.

---

## 6. Adding your own spot

Everything is data. Edit `eavesdrop_spots.json`:

```json
{
  "spot_id": "spot_warehouse_door",
  "name": "Behind the Warehouse Door",
  "location": "Ironhold Village",
  "position": [38.0, 18.0],
  "radius": 4.0,
  "alert_radius": 3.0,
  "npc_pair": ["Elara", "Thorne"],
  "max_turns": 4,

  "topic_id": "topic_warehouse",
  "topic_name": "What Was in the Crates",
  "topic_description": "Elara wants the crates moved before dawn. Thorne wants to know what is in them first.",

  "intel_id": "intel_warehouse",
  "intel_text": "The crates were never opened. Elara is being paid not to open them.",

  "require_concealed": false,
  "blocked_postures": ["run"],
  "once_only": true,
  "requires_intel": ["intel_three_crates"]
}
```

Field notes:

- `topic_id` / `topic_name` / `topic_description` are **required and fixed**. The
  topic is registered into `ConversationTopicRegistry` at load time and forced
  onto the conversation, so the random topic selector never runs. Random topics
  are what make eavesdropping feel worthless — nine out of ten would be small talk.
- `position` is the player's spot, not the NPCs'. NPC coordinates live under
  `npc_positions` in the same file.
- `once_only: true` marks the spot spent after one listen. Set it to `false` for
  repeatable ambience you still want to be deliberate.
- `requires_flags` reads from the world state object passed to `EavesdropManager`.

---

## 7. Ambient chatter still works

`ConversationManager` gained two small hooks so the two systems coexist:

```python
ConversationManager(..., ambient_chance=0.1)   # how often NPCs chat on their own
manager.ambient_filter = fn(npc1, npc2) -> bool  # True = do not start this pair
```

`EavesdropManager` registers itself as the filter and suppresses any pair the
player is currently standing next to waiting to listen. Without it, the two NPCs
would often start chatting on their own just as the player arrives, and pressing
Listen would fail with a busy error. With it, arriving at a spot reliably hands
the conversation to the player.

---

## 8. Tests

```bash
python -m unittest test_eavesdrop -v
```

27 tests covering range, posture, cover, detection, narrative gating, intel
recording, spot consumption, prefetch caching, ambient coexistence, and the
shape of the API payloads (including that `/map` never leaks `intel_text`).
