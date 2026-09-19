"""
Unit tests for the eavesdrop system.

Covers the three rules the feature is built on:
  1. distance  — close enough to hear, far enough not to be noticed
  2. posture   — running blows your cover, crouching and cover help
  3. payoff    — every completed eavesdrop writes an intel entry

Run:
    python -m unittest test_eavesdrop -v
"""

import asyncio
import json
import os
import unittest
from pathlib import Path

from npc_dialogue import NPCManager
from npc_conversation import ConversationManager
from eavesdrop import (
    EavesdropManager,
    EavesdropSpot,
    IntelEntry,
    Posture,
    REASONS,
)

CARDS_DIR = Path("character_cards")
SPOTS_FILE = Path("eavesdrop_spots.json")


def _have_cards() -> bool:
    return CARDS_DIR.exists() and any(CARDS_DIR.glob("*.json"))


class EavesdropTestCase(unittest.IsolatedAsyncioTestCase):
    """Shared fixture: a loaded world with three NPCs and four spots."""

    async def asyncSetUp(self):
        if not _have_cards():
            self.skipTest("character_cards/ not found")

        self.npc_manager = NPCManager()
        for path in sorted(CARDS_DIR.glob("*.json")):
            self.npc_manager.load_character(str(path), player_id="test_player")

        self.conversations = ConversationManager(
            npc_manager=self.npc_manager,
            ambient_chance=1.0,  # tests should not depend on a dice roll
        )
        self.eavesdrop = EavesdropManager(self.conversations)
        if SPOTS_FILE.exists():
            self.eavesdrop.load_spots(SPOTS_FILE)

        self.player = "test_player"

    def _move(self, x, y, posture=Posture.STAND, concealed=False):
        return self.eavesdrop.set_presence(
            self.player, (x, y), posture=posture, concealed=concealed,
            location="Ironhold Village",
        )


class TestSpotLoading(EavesdropTestCase):
    async def test_spots_load_from_file(self):
        self.assertGreaterEqual(len(self.eavesdrop.spots), 4)

    async def test_every_spot_registers_its_topic(self):
        """每个偷听点的话题必须挂进话题表，否则对谈会退回随机抽话题。"""
        registry = self.conversations.engine.topic_registry
        for spot in self.eavesdrop.spots.values():
            self.assertIn(spot.topic_id, registry.topics, spot.spot_id)

    async def test_npc_positions_loaded(self):
        self.assertIn("Thorne", self.eavesdrop.npc_positions)
        self.assertIn("Elara", self.eavesdrop.npc_positions)
        self.assertIn("Zephyr", self.eavesdrop.npc_positions)


class TestAvailability(EavesdropTestCase):
    async def test_out_of_range(self):
        self._move(200, 200)
        avail = self.eavesdrop.check("spot_forge_alley", self.player)
        self.assertFalse(avail.can_eavesdrop)
        self.assertEqual(avail.blocking, "out_of_range")
        self.assertEqual(avail.reason, REASONS["out_of_range"])

    async def test_in_range_success(self):
        self._move(25, 25)
        avail = self.eavesdrop.check("spot_forge_alley", self.player)
        self.assertTrue(avail.can_eavesdrop, avail.reason)
        self.assertEqual(avail.blocking, "ok")

    async def test_running_is_blocked(self):
        """跑过去不算偷听 —— 这是 007 那套设计的核心手感。"""
        self._move(22, 26, posture=Posture.RUN)
        avail = self.eavesdrop.check("spot_forge_alley", self.player)
        self.assertFalse(avail.can_eavesdrop)
        self.assertEqual(avail.blocking, "blocked_posture")

    async def test_standing_too_close_gets_noticed(self):
        """站到 NPC 脸上就会被发现，哪怕位置在偷听点半径内。"""
        self._move(21.5, 25.0)  # 紧贴 Thorne
        avail = self.eavesdrop.check("spot_forge_alley", self.player)
        self.assertTrue(avail.in_range, "玩家应该落在偷听点半径内")
        self.assertFalse(avail.can_eavesdrop)
        self.assertEqual(avail.blocking, "detected")

    async def test_concealment_shrinks_detection_radius(self):
        """同一个位置，暴露时被发现，躲起来就能听。"""
        spot = self.eavesdrop.spots["spot_forge_alley"]
        exposed_r = self.eavesdrop._detection_radius(spot, Posture.WALK, False)
        hidden_r = self.eavesdrop._detection_radius(spot, Posture.WALK, True)
        self.assertLess(hidden_r, exposed_r)

    async def test_crouching_shrinks_detection_radius(self):
        spot = self.eavesdrop.spots["spot_forge_alley"]
        walk_r = self.eavesdrop._detection_radius(spot, Posture.WALK, False)
        crouch_r = self.eavesdrop._detection_radius(spot, Posture.CROUCH, False)
        self.assertLess(crouch_r, walk_r)

    async def test_unknown_spot(self):
        avail = self.eavesdrop.check("no_such_spot", self.player)
        self.assertFalse(avail.can_eavesdrop)
        self.assertEqual(avail.blocking, "unknown_spot")


class TestNarrativeGating(EavesdropTestCase):
    async def test_finale_is_locked_until_intel_collected(self):
        """最后一幕要点听完前三段才能解锁 —— 这是把四段对话缝成一条线的关键。"""
        self._move(53, 26, concealed=True)
        avail = self.eavesdrop.check("spot_rampart_gap", self.player)
        self.assertFalse(avail.can_eavesdrop)
        self.assertEqual(avail.blocking, "locked")

    async def test_finale_requires_concealment(self):
        for intel_id in ("intel_forge_order", "intel_three_crates", "intel_iron_remembers"):
            self.eavesdrop.intel.setdefault(self.player, []).append(
                IntelEntry(intel_id, "", "", ("", ""))
            )

        self._move(53, 26, concealed=False)
        self.assertEqual(
            self.eavesdrop.check("spot_rampart_gap", self.player).blocking,
            "not_concealed",
        )

        self._move(53, 26, concealed=True)
        self.assertTrue(
            self.eavesdrop.check("spot_rampart_gap", self.player).can_eavesdrop
        )


class TestEavesdropFlow(EavesdropTestCase):
    async def test_successful_eavesdrop_produces_transcript_and_intel(self):
        self._move(25, 25)
        result = await self.eavesdrop.eavesdrop("spot_forge_alley", self.player)
        self.assertTrue(result["ok"], result.get("reason"))
        self.assertGreater(len(result["transcript"]), 0)
        self.assertIsNotNone(result["intel"])
        self.assertEqual(result["intel"]["intel_id"], "intel_forge_order")

    async def test_transcript_alternates_speakers(self):
        self._move(25, 25)
        result = await self.eavesdrop.eavesdrop("spot_forge_alley", self.player)
        speakers = [line["speaker"] for line in result["transcript"]]
        self.assertGreaterEqual(len(speakers), 2)
        self.assertNotEqual(speakers[0], speakers[1])

    async def test_intel_is_recorded_on_player(self):
        self._move(25, 25)
        await self.eavesdrop.eavesdrop("spot_forge_alley", self.player)
        ids = [e["intel_id"] for e in self.eavesdrop.get_intel(self.player)]
        self.assertIn("intel_forge_order", ids)

    async def test_intel_brief_is_prompt_ready(self):
        self._move(25, 25)
        await self.eavesdrop.eavesdrop("spot_forge_alley", self.player)
        brief = self.eavesdrop.intel_brief(self.player)
        self.assertIn("OVERHEARD", brief)
        self.assertIn("Ironhold", brief)

    async def test_spot_is_consumed_afterwards(self):
        self._move(25, 25)
        await self.eavesdrop.eavesdrop("spot_forge_alley", self.player)
        avail = self.eavesdrop.check("spot_forge_alley", self.player)
        self.assertFalse(avail.can_eavesdrop)
        self.assertEqual(avail.blocking, "consumed")

    async def test_failed_eavesdrop_returns_reason_not_exception(self):
        self._move(200, 200)
        result = await self.eavesdrop.eavesdrop("spot_forge_alley", self.player)
        self.assertFalse(result["ok"])
        self.assertEqual(result["blocking"], "out_of_range")
        self.assertEqual(result["transcript"], [])


class TestAmbientCoexistence(EavesdropTestCase):
    async def test_ambient_filter_blocks_the_pair_player_is_listening_to(self):
        """玩家站在偷听点上时，这一对 NPC 不能再自己先聊起来。"""
        self._move(25, 25)
        blocked = self.eavesdrop._blocks_ambient("Thorne", "Elara")
        self.assertTrue(blocked)

    async def test_ambient_filter_allows_unrelated_pairs(self):
        self._move(25, 25)
        self.assertFalse(self.eavesdrop._blocks_ambient("Elara", "Zephyr"))

    async def test_ambient_filter_releases_after_consumed(self):
        self._move(25, 25)
        await self.eavesdrop.eavesdrop("spot_forge_alley", self.player)
        self.assertFalse(self.eavesdrop._blocks_ambient("Thorne", "Elara"))

    async def test_ambient_chatter_still_happens_when_player_is_elsewhere(self):
        """玩家不在场时，NPC 之间应该还能自己聊起来。"""
        self._move(200, 200)
        await self.conversations.check_proximity_conversations("Ironhold Village")
        # ambient_chance=1.0 时必定开聊
        self.assertGreater(len(self.conversations.conversation_history), 0)


class TestPrefetch(EavesdropTestCase):
    async def test_prefetch_caches_transcript(self):
        ok = await self.eavesdrop.prefetch("spot_forge_alley")
        self.assertTrue(ok)
        spot = self.eavesdrop.spots["spot_forge_alley"]
        self.assertIsNotNone(spot.cached_transcript)

    async def test_prefetch_result_is_served_instantly(self):
        await self.eavesdrop.prefetch("spot_forge_alley")
        self._move(25, 25)
        result = await self.eavesdrop.eavesdrop("spot_forge_alley", self.player)
        self.assertTrue(result["ok"])
        self.assertEqual(
            len(result["transcript"]),
            len(self.eavesdrop.spots["spot_forge_alley"].cached_transcript),
        )


class TestApiShape(EavesdropTestCase):
    async def test_list_spots_returns_ui_ready_payload(self):
        self._move(25, 25)
        spots = self.eavesdrop.list_spots(self.player)
        self.assertEqual(len(spots), len(self.eavesdrop.spots))
        first = spots[0]
        for key in ("spot_id", "name", "position", "radius", "availability"):
            self.assertIn(key, first)
        self.assertIn("reason", first["availability"])

    async def test_public_view_hides_intel_text(self):
        """地图接口不能把答案漏出去。"""
        for spot in self.eavesdrop.list_spots(self.player):
            self.assertNotIn("intel_text", spot)

    async def test_status_payload(self):
        self._move(25, 25)
        st = self.eavesdrop.status(self.player)
        self.assertEqual(st["spots_total"], len(self.eavesdrop.spots))
        self.assertEqual(st["intel_count"], 0)
        self.assertIn("presence", st)



class TestSpotGeometry(EavesdropTestCase):
    """偷听点的坐标不是随便摆的。这两条不变量决定了机制成不成立。"""

    async def test_hear_zone_has_a_dangerous_side(self):
        """可听范围必须有一部分落在察觉半径内。

        否则玩家站在这个圆圈里任何位置都安全 —— 没有取舍，也就没有玩法。
        """
        for spot in self.eavesdrop.spots.values():
            if not spot.npc_anchor:
                continue
            d = self.eavesdrop._distance(spot.position, spot.npc_anchor)
            near_edge = d - spot.radius
            self.assertLess(
                near_edge, spot.alert_radius,
                f"{spot.spot_id}: 整个可听范围都在察觉半径外，站哪儿都不会被发现",
            )

    async def test_centre_of_hear_zone_is_safe(self):
        """同时，圆圈正中心必须是安全的，否则这个点根本用不了。"""
        for spot in self.eavesdrop.spots.values():
            if not spot.npc_anchor:
                continue
            d = self.eavesdrop._distance(spot.position, spot.npc_anchor)
            detect = self.eavesdrop._detection_radius(spot, Posture.WALK, False)
            self.assertGreaterEqual(
                d, detect,
                f"{spot.spot_id}: 站在正中心就会被发现",
            )


class TestContentWiring(EavesdropTestCase):
    """内容文件和角色卡之间的接缝。

    这里测的不是偷听逻辑，而是"这套东西能不能真的跑起来"。

    对应的真实故障：api_server 启动时装好了 conversation_manager、
    eavesdrop_manager 等一切，却从没往 NPC 池里放角色卡。结果玩家能走到
    偷听点、check 也老实回答"位置合适，可以听了"，一按 Listen 却是 400
    "NPC 'Thorne' not loaded"。当时单元测试全绿 —— 因为那条路径没人走过。
    """

    async def test_every_npc_named_in_the_spots_can_be_loaded(self):
        if not _have_cards():
            self.skipTest("character_cards/ 不存在")

        manager = NPCManager(backend="ollama")  # 只加载卡，不连模型
        for card in sorted(CARDS_DIR.glob("*.json")):
            manager.load_character(str(card))

        loaded = set(manager.list_characters())
        needed = {n for spot in self.eavesdrop.spots.values() for n in spot.npc_pair}
        missing = needed - loaded

        self.assertEqual(
            missing, set(),
            f"偷听点引用了加载不出来的 NPC：{sorted(missing)}；"
            f"character_cards/ 里只有 {sorted(loaded)}",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
