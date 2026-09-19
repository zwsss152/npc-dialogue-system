"""
Eavesdrop System — 偷听点机制
=============================

让玩家"站对位置、别被发现、按一下"就能听到一段有信息量的 NPC 对谈。

设计来源是 IO Interactive 在《007 初露锋芒》里的 Spycraft 玩法：
偷听不是"走进范围自动播字幕"，而是一件需要**主动争取**的事——
站到正确的位置、放轻脚步、别被看见，然后才会听到有用的东西。

三条核心规则
------------
1. **距离要够近，但不能太近。**
   偷听点有自己的半径；同时每对 NPC 有"察觉半径"。站进去能听见，
   再往前一步他们就不说了。这个"能听见但不被发现"的夹缝就是玩法空间。

2. **动作会暴露你。**
   跑进察觉范围会被立刻发现；蹲下/进入掩体会显著缩小对方的察觉半径。
   所以偷听点天然会被设计在"需要绕路"或"需要先制造干扰"的位置。

3. **听完要留下东西。**
   每段对话产出一条情报（intel），写进玩家的情报簿。
   情报不只是收藏品——它可以直接注入该 NPC 的 system prompt，
   让玩家"知道"的内容变成后续对话里的新选项。

与其他模块的关系
----------------
本模块**不生成对话**。对话交给 `npc_conversation.ConversationManager`，
本模块只负责：判定能不能听、决定听什么话题、把结果记下来。
环境闲聊（NPC 之间自己聊）仍然由 `ConversationManager` 随机触发，
但会通过 `ambient_filter` 回调避开玩家正在偷听的 NPC 对。
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from npc_conversation import (
    ConversationManager,
    ConversationTopic,
    ConversationTrigger,
)

Position = Tuple[float, float]

# ---------------------------------------------------------------------------
# 玩家姿态与存在状态
# ---------------------------------------------------------------------------


class Posture:
    """玩家当前的移动姿态。影响被发现的难度。"""

    STAND = "stand"
    WALK = "walk"      # 正常移动，察觉半径 ×1.0
    RUN = "run"        # 冲刺，察觉半径 ×2.5
    CROUCH = "crouch"  # 潜行，察觉半径 ×0.5

    ALL = (STAND, WALK, RUN, CROUCH)

    #: 各姿态的"显眼系数"，直接乘在 NPC 的察觉半径上
    NOISE = {
        STAND: 1.0,
        WALK: 1.0,
        RUN: 2.5,
        CROUCH: 0.5,
    }


@dataclass
class PlayerPresence:
    """玩家在世界里的位置与状态。由前端（或 Unity 客户端）持续上报。"""

    player_id: str
    position: Position = (0.0, 0.0)
    posture: str = Posture.STAND
    concealed: bool = False          # 是否处于掩体/门后/人群中
    location: str = ""               # 粗粒度区域名，与 npc_locations 对齐
    updated_at: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# 偷听点
# ---------------------------------------------------------------------------


@dataclass
class EavesdropSpot:
    """
    一个设计好的偷听点。

    注意 `topic_id` / `topic_name` / `topic_description` 是**写死**的，
    不参与随机抽取。这是刻意的：007 里的偷听是有情报价值的，
    随机抽话题会让十次偷听里九次是废话。
    """

    spot_id: str
    name: str
    position: Position
    npc_pair: Tuple[str, str]

    # 话题（注册进 ConversationTopicRegistry）
    topic_id: str
    topic_name: str
    topic_description: str = ""

    # 情报奖励
    intel_id: str = ""
    intel_text: str = ""

    # 空间规则
    radius: float = 6.0            # 玩家必须落在这个半径内
    alert_radius: float = 3.0      # NPC 的察觉半径（进入即被发现）
    npc_anchor: Optional[Position] = None
    #: 这段对话发生时，两个人**实际站在哪里**。
    #: 一个 NPC 会出现在多个偷听点里，所以不能用全局坐标判定察觉，
    #: 必须由每个点自己声明这对人此刻的位置。为空时才退回全局坐标。
    require_concealed: bool = False  # 是否必须处于隐蔽状态
    blocked_postures: Tuple[str, ...] = (Posture.RUN,)  # 这些姿态直接失去资格

    # 叙事规则
    max_turns: int = 4
    once_only: bool = True
    requires_flags: Tuple[str, ...] = ()   # 需要玩家已解锁的世界标记
    requires_intel: Tuple[str, ...] = ()   # 需要玩家已获得的其它情报
    location: str = ""

    # 运行时缓存（预生成的对白）
    cached_transcript: Optional[List[Dict[str, Any]]] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "EavesdropSpot":
        pos = data.get("position", [0.0, 0.0])
        pair = data.get("npc_pair", ["", ""])
        return cls(
            spot_id=data["spot_id"],
            name=data.get("name", data["spot_id"]),
            position=(float(pos[0]), float(pos[1])),
            npc_pair=(pair[0], pair[1]),
            topic_id=data.get("topic_id", data["spot_id"]),
            topic_name=data.get("topic_name", data.get("name", data["spot_id"])),
            topic_description=data.get("topic_description", ""),
            intel_id=data.get("intel_id", f"intel_{data['spot_id']}"),
            intel_text=data.get("intel_text", ""),
            radius=float(data.get("radius", 6.0)),
            alert_radius=float(data.get("alert_radius", 3.0)),
            npc_anchor=(
                (float(data["npc_anchor"][0]), float(data["npc_anchor"][1]))
                if data.get("npc_anchor") else None
            ),
            require_concealed=bool(data.get("require_concealed", False)),
            blocked_postures=tuple(data.get("blocked_postures", ["run"])),
            max_turns=int(data.get("max_turns", 4)),
            once_only=bool(data.get("once_only", True)),
            requires_flags=tuple(data.get("requires_flags", [])),
            requires_intel=tuple(data.get("requires_intel", [])),
            location=data.get("location", ""),
        )

    def to_public_dict(self) -> Dict[str, Any]:
        """给地图 UI 用的安全视图（不含情报答案）。"""
        return {
            "spot_id": self.spot_id,
            "name": self.name,
            "position": list(self.position),
            "radius": self.radius,
            "alert_radius": self.alert_radius,
            "npc_anchor": list(self.npc_anchor) if self.npc_anchor else None,
            "npc_pair": list(self.npc_pair),
            "location": self.location,
            "require_concealed": self.require_concealed,
            "max_turns": self.max_turns,
        }


# ---------------------------------------------------------------------------
# 判定结果
# ---------------------------------------------------------------------------

#: blocking code -> 给玩家看的文案
REASONS: Dict[str, str] = {
    "ok": "位置合适，可以听了。",
    "out_of_range": "离得太远，你只看见他们的嘴在动。",
    "detected": "他们注意到你了——话头断了。",
    "blocked_posture": "你动静太大，谈话停住了。",
    "not_concealed": "这里没有遮挡，他们看得见你。",
    "consumed": "这些话你已经听过了。",
    "locked": "你还没到能听懂的时候。",
    "busy": "他们正在说别的事。",
    "unknown_spot": "这里没什么可听的。",
}


@dataclass
class SpotAvailability:
    spot_id: str
    name: str
    position: Position
    radius: float
    distance: float
    in_range: bool
    can_eavesdrop: bool
    blocking: str
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# 情报
# ---------------------------------------------------------------------------


@dataclass
class IntelEntry:
    intel_id: str
    text: str
    spot_id: str
    source_npcs: Tuple[str, str]
    learned_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# 管理器
# ---------------------------------------------------------------------------


class EavesdropManager:
    """
    偷听点的注册、判定与执行。

    Args:
        conversation_manager: 复用现成的 NPC 对谈系统，本模块不生成台词
        world_state: 可选，用于读取 `requires_flags` 需要世界标记
        default_turn_delay: 实时生成时每回合间隔。设 0 表示不额外等待
    """

    def __init__(
        self,
        conversation_manager: ConversationManager,
        world_state: Optional[Any] = None,
        default_turn_delay: float = 0.0,
    ) -> None:
        self.conversations = conversation_manager
        self.world_state = world_state
        self.default_turn_delay = default_turn_delay

        self.spots: Dict[str, EavesdropSpot] = {}
        self.npc_positions: Dict[str, Position] = {}
        self.presence: Dict[str, PlayerPresence] = {}

        # player_id -> {spot_id}   已偷听过的点
        self.consumed: Dict[str, set] = {}
        # player_id -> [IntelEntry]
        self.intel: Dict[str, List[IntelEntry]] = {}
        # 最近一次偷听的完整对白，供前端回看
        self.last_session: Dict[str, Dict[str, Any]] = {}

        # 让环境闲聊避开玩家正在偷听的 NPC 对
        self.conversations.ambient_filter = self._blocks_ambient

    # -- 注册 ---------------------------------------------------------------

    def register_spot(self, spot: EavesdropSpot) -> None:
        """注册偷听点，并把它的专属话题挂进话题表。"""
        self.spots[spot.spot_id] = spot
        self.conversations.engine.topic_registry.register_topic(
            ConversationTopic(
                topic_id=spot.topic_id,
                name=spot.topic_name,
                description=spot.topic_description,
                keywords=[],
                min_relationship=-100.0,
                max_uses=999,          # 由本模块的 once_only 控制，不走话题冷却
                cooldown_minutes=0,
                priority=50,           # 只在被显式指定时使用
            )
        )

    def register_npc_position(
        self, npc_name: str, position: Position, location: str = ""
    ) -> None:
        self.npc_positions[npc_name] = position
        if location:
            self.conversations.update_npc_location(npc_name, location)

    def load_spots(self, path: str | Path) -> int:
        """从 JSON 文件批量加载偷听点。文件格式见 eavesdrop_spots.json。"""
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        count = 0
        for item in raw.get("spots", []):
            self.register_spot(EavesdropSpot.from_dict(item))
            count += 1
        for npc in raw.get("npc_positions", []):
            self.register_npc_position(
                npc["name"], tuple(npc["position"]), npc.get("location", "")
            )
        return count

    # -- 玩家状态 -----------------------------------------------------------

    def set_presence(
        self,
        player_id: str,
        position: Position,
        posture: str = Posture.STAND,
        concealed: bool = False,
        location: str = "",
    ) -> PlayerPresence:
        if posture not in Posture.ALL:
            posture = Posture.STAND
        p = PlayerPresence(
            player_id=player_id,
            position=(float(position[0]), float(position[1])),
            posture=posture,
            concealed=concealed,
            location=location,
        )
        self.presence[player_id] = p
        return p

    def get_presence(self, player_id: str) -> PlayerPresence:
        if player_id not in self.presence:
            self.presence[player_id] = PlayerPresence(player_id=player_id)
        return self.presence[player_id]

    # -- 判定 ---------------------------------------------------------------

    @staticmethod
    def _distance(a: Position, b: Position) -> float:
        return math.hypot(a[0] - b[0], a[1] - b[1])

    def _detection_radius(self, spot: EavesdropSpot, posture: str,
                          concealed: bool) -> float:
        """当前姿态下的实际察觉半径。

        跑动放大、潜行缩小、有掩体再缩一档。这条公式是整个玩法的手感来源，
        调它比调别的参数都敏感。
        """
        radius = spot.alert_radius * Posture.NOISE.get(posture, 1.0)
        if concealed:
            radius *= 0.4
        return radius

    def _flags(self) -> set:
        if self.world_state is None:
            return set()
        flags = getattr(self.world_state, "flags", None)
        return set(flags) if flags else set()

    def _intel_ids(self, player_id: str) -> set:
        return {e.intel_id for e in self.intel.get(player_id, [])}

    def _blocks_ambient(self, npc1: str, npc2: str) -> bool:
        """环境闲聊抑制：玩家正站在某个偷听点上等待时，不要让这两人先聊起来。

        否则玩家点下按钮会撞上 "already in a conversation"，体验直接断掉。
        """
        pair = {npc1, npc2}
        for presence in self.presence.values():
            for spot in self.spots.values():
                if set(spot.npc_pair) != pair:
                    continue
                if spot.spot_id in self.consumed.get(presence.player_id, set()):
                    continue
                if self._distance(presence.position, spot.position) <= spot.radius:
                    return True
        return False

    def check(self, spot_id: str, player_id: str) -> SpotAvailability:
        """判定某个偷听点当前能不能用，并给出人类可读的原因。"""
        spot = self.spots.get(spot_id)
        if spot is None:
            return SpotAvailability(
                spot_id=spot_id, name=spot_id, position=(0.0, 0.0), radius=0.0,
                distance=-1.0, in_range=False, can_eavesdrop=False,
                blocking="unknown_spot", reason=REASONS["unknown_spot"],
            )

        presence = self.get_presence(player_id)
        dist_to_spot = self._distance(presence.position, spot.position)
        in_range = dist_to_spot <= spot.radius

        def result(can: bool, blocking: str) -> SpotAvailability:
            return SpotAvailability(
                spot_id=spot.spot_id, name=spot.name, position=spot.position,
                radius=spot.radius, distance=round(dist_to_spot, 2),
                in_range=in_range, can_eavesdrop=can, blocking=blocking,
                reason=REASONS[blocking],
            )

        if not in_range:
            return result(False, "out_of_range")

        if spot.once_only and spot.spot_id in self.consumed.get(player_id, set()):
            return result(False, "consumed")

        missing = [f for f in spot.requires_flags if f not in self._flags()]
        missing += [i for i in spot.requires_intel if i not in self._intel_ids(player_id)]
        if missing:
            return result(False, "locked")

        if presence.posture in spot.blocked_postures:
            return result(False, "blocked_posture")

        if spot.require_concealed and not presence.concealed:
            return result(False, "not_concealed")

        # 距离 NPC 太近 —— 这里才是"偷听"和"被发现"的分界线
        detect_r = self._detection_radius(spot, presence.posture, presence.concealed)
        if spot.npc_anchor is not None:
            anchors = [spot.npc_anchor]
        else:
            anchors = [self.npc_positions[n] for n in spot.npc_pair
                       if n in self.npc_positions]
        for anchor in anchors:
            if self._distance(presence.position, anchor) < detect_r:
                return result(False, "detected")

        if any(self.conversations.is_npc_in_conversation(n) for n in spot.npc_pair):
            return result(False, "busy")

        return result(True, "ok")

    def list_spots(self, player_id: str) -> List[Dict[str, Any]]:
        """给地图 UI 的完整视图：每个点 + 当前位置的可听与否。"""
        out = []
        for spot in self.spots.values():
            view = spot.to_public_dict()
            view["availability"] = self.check(spot.spot_id, player_id).to_dict()
            out.append(view)
        return out

    # -- 执行 ---------------------------------------------------------------

    async def eavesdrop(
        self, spot_id: str, player_id: str, force_live: bool = False
    ) -> Dict[str, Any]:
        """按下偷听按钮后的完整流程。

        返回结构里同时带上 transcript 和 intel，前端可以一次性渲染，
        也可以用 transcript 做逐条揭示的演出。
        """
        avail = self.check(spot_id, player_id)
        if not avail.can_eavesdrop:
            return {
                "ok": False,
                "spot_id": spot_id,
                "blocking": avail.blocking,
                "reason": avail.reason,
                "transcript": [],
                "intel": None,
            }

        spot = self.spots[spot_id]

        if spot.cached_transcript and not force_live:
            transcript = [dict(x) for x in spot.cached_transcript]
        else:
            conversation = await self.conversations.run_full_conversation(
                npc1_name=spot.npc_pair[0],
                npc2_name=spot.npc_pair[1],
                trigger=ConversationTrigger.EAVESDROP,
                max_turns=spot.max_turns,
                turn_delay=self.default_turn_delay,
                location=spot.location,
                context={"spot_id": spot.spot_id, "eavesdrop": True},
                topic_id=spot.topic_id,
            )
            transcript = [
                {
                    "speaker": e.speaker,
                    "listener": e.listener,
                    "message": e.message,
                    "emotion": getattr(e, "emotion", "neutral"),
                    "topic": e.topic,
                }
                for e in conversation.exchanges
            ]
            spot.cached_transcript = list(transcript)

        # 记录消耗
        self.consumed.setdefault(player_id, set()).add(spot_id)

        # 写入情报
        entry = None
        if spot.intel_text:
            entry = IntelEntry(
                intel_id=spot.intel_id,
                text=spot.intel_text,
                spot_id=spot.spot_id,
                source_npcs=spot.npc_pair,
            )
            self.intel.setdefault(player_id, []).append(entry)

        session = {
            "ok": True,
            "spot_id": spot_id,
            "spot_name": spot.name,
            "blocking": "ok",
            "reason": REASONS["ok"],
            "npc_pair": list(spot.npc_pair),
            "topic": spot.topic_name,
            "transcript": transcript,
            "intel": entry.to_dict() if entry else None,
            "finished_at": time.time(),
        }
        self.last_session[player_id] = session
        return session

    # -- 预生成（解决本地模型太慢的问题）-------------------------------------

    async def prefetch(self, spot_id: str) -> bool:
        """提前把对白生成好并缓存。

        本地 Ollama 跑 4 回合大约 10-20 秒。如果等玩家按下按钮才开始生成，
        这段等待会直接毁掉体验。所以推荐在**关卡加载时**把所有偷听点预生成一遍，
        正式游玩时是瞬时的。

        代价是：预生成的版本不会反映玩家当时的世界状态。对"偷听"这种
        玩家只是旁观者的场景来说，这个取舍是可以接受的。
        """
        spot = self.spots.get(spot_id)
        if spot is None or spot.cached_transcript:
            return False
        if any(self.conversations.is_npc_in_conversation(n) for n in spot.npc_pair):
            return False

        conversation = await self.conversations.run_full_conversation(
            npc1_name=spot.npc_pair[0],
            npc2_name=spot.npc_pair[1],
            trigger=ConversationTrigger.EAVESDROP,
            max_turns=spot.max_turns,
            turn_delay=0.0,
            location=spot.location,
            context={"spot_id": spot.spot_id, "prefetch": True},
            topic_id=spot.topic_id,
        )
        spot.cached_transcript = [
            {
                "speaker": e.speaker,
                "listener": e.listener,
                "message": e.message,
                "emotion": getattr(e, "emotion", "neutral"),
                "topic": e.topic,
            }
            for e in conversation.exchanges
        ]
        return True

    async def prefetch_all(self) -> int:
        done = 0
        for spot_id in self.spots:
            if await self.prefetch(spot_id):
                done += 1
        return done

    # -- 查询 ---------------------------------------------------------------

    def get_intel(self, player_id: str) -> List[Dict[str, Any]]:
        return [e.to_dict() for e in self.intel.get(player_id, [])]

    def intel_brief(self, player_id: str) -> str:
        """把玩家已知情报拼成一段文本，可直接注入 NPC 的 system prompt。

        这样"偷听到的内容"就不只是收藏品，而会真的变成后续对话里的新选项。
        """
        entries = self.intel.get(player_id, [])
        if not entries:
            return ""
        lines = "\n".join(f"- {e.text}" for e in entries)
        return f"\nTHE PLAYER HAS OVERHEARD THE FOLLOWING (they may bring it up):\n{lines}\n"

    def status(self, player_id: str) -> Dict[str, Any]:
        return {
            "player_id": player_id,
            "presence": asdict(self.get_presence(player_id)),
            "spots_total": len(self.spots),
            "spots_consumed": sorted(self.consumed.get(player_id, set())),
            "intel_count": len(self.intel.get(player_id, [])),
            "intel": self.get_intel(player_id),
        }
