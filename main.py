"""
Fable CoT Leak Fix

Fable 模型偶尔将第二层思考链泄漏到正文中，与实际回复粘连，
中间缺少空格（例如 "dissect.Yes"）。

本插件在两个阶段修复：
1. on_decorating_result — 实时 cosmetic：
   检测泄漏边界，将泄漏的 CoT 以合并转发消息发送，
   并将显示链中的正文替换为干净版本。
2. on_llm_request — 下一轮结构修复：
   扫描 req.contexts 中的 assistant 消息，
   将残留的泄漏 CoT 从上下文历史中清除，
   确保 LLM 收到的上下文是干净的。

F(A) = A(F)
"""

import re

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import Node, Plain
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

# Pattern: lowercase letter + period + uppercase letter with no space between.
# This is the seam where Fable's leaked CoT glues onto the actual response.
LEAK_BOUNDARY = re.compile(r"[a-z]\.[A-Z]")


def _split_leaked_cot(text: str) -> tuple[str, str] | None:
    """
    If the text contains a leaked CoT boundary, return (leaked_cot, actual_response).
    Otherwise return None.
    """
    match = LEAK_BOUNDARY.search(text)
    if not match:
        return None

    split_pos = match.start() + 2
    leaked_cot = text[:split_pos].strip()
    actual_response = text[split_pos:].strip()

    if not leaked_cot or not actual_response:
        return None

    return leaked_cot, actual_response


@register(
    "astrbot_plugin_fix_fable_cot",
    "Felis Abyssalis & Abyss AI",
    "Fable 模型泄漏的第二层 CoT 从正文中分离出来，以合并转发消息发送",
    "1.0.0",
)
class FixFableCotPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    # ------------------------------------------------------------------
    # Stage 1: Cosmetic fix — split leaked CoT from display chain
    # ------------------------------------------------------------------

    @filter.on_decorating_result()
    async def fix_display(self, event: AstrMessageEvent):
        try:
            result = event.get_result()
            if not result:
                return

            chain = result.chain
            if not isinstance(chain, list):
                return

            # Concatenate all Plain segments to find the leak boundary
            full_text = ""
            for component in chain:
                if isinstance(component, Plain):
                    full_text += component.text

            if not full_text:
                return

            split = _split_leaked_cot(full_text)
            if not split:
                return

            leaked_cot, actual_response = split

            # Send leaked CoT as forwarded message
            nodes = [
                Node(
                    uin=0,
                    name="🌬️Fable's reasoning process",
                    content=[Plain(leaked_cot)],
                )
            ]
            await event.send(event.chain_result(nodes))

            # Replace all Plain segments in the chain with the clean response
            first_plain_idx = None
            to_remove = []
            for i, component in enumerate(chain):
                if isinstance(component, Plain):
                    if first_plain_idx is None:
                        first_plain_idx = i
                    to_remove.append(i)

            for i in reversed(to_remove):
                chain.pop(i)

            if first_plain_idx is not None:
                chain.insert(first_plain_idx, Plain(actual_response))

        except Exception as e:
            logger.error(f"fix_fable_cot display fix failed: {e}")

    # ------------------------------------------------------------------
    # Stage 2: Structural fix — clean leaked CoT from context history
    # ------------------------------------------------------------------

    @staticmethod
    def _clean_message(msg) -> tuple[object, bool]:
        """
        Clean a single context message. Handles three formats:
        1. Plain string
        2. Dict with string content
        3. Dict with list content (multimodal)

        Returns (cleaned_message, was_modified).
        """
        # Format 1: plain string
        if isinstance(msg, str):
            split = _split_leaked_cot(msg)
            if split:
                return split[1], True
            return msg, False

        # Format 2/3: dict
        if isinstance(msg, dict):
            role = msg.get("role", "")
            if role != "assistant":
                return msg, False

            content = msg.get("content", "")

            # Dict + string content
            if isinstance(content, str):
                split = _split_leaked_cot(content)
                if split:
                    msg_copy = msg.copy()
                    msg_copy["content"] = split[1]
                    return msg_copy, True
                return msg, False

            # Dict + list content (multimodal)
            if isinstance(content, list):
                new_parts = []
                modified = False
                for part in content:
                    if (
                        isinstance(part, dict)
                        and part.get("type") == "text"
                        and isinstance(part.get("text"), str)
                    ):
                        split = _split_leaked_cot(part["text"])
                        if split:
                            part_copy = part.copy()
                            part_copy["text"] = split[1]
                            new_parts.append(part_copy)
                            modified = True
                            continue
                    new_parts.append(part)

                if modified:
                    msg_copy = msg.copy()
                    msg_copy["content"] = new_parts
                    return msg_copy, True
                return msg, False

        return msg, False

    @filter.on_llm_request()
    async def fix_context(self, event: AstrMessageEvent, req: ProviderRequest):
        try:
            if not hasattr(req, "contexts") or not req.contexts:
                return

            new_contexts = []
            total_cleaned = 0
            for msg in req.contexts:
                cleaned, modified = self._clean_message(msg)
                if modified:
                    total_cleaned += 1
                new_contexts.append(cleaned)

            if total_cleaned > 0:
                req.contexts = new_contexts
                logger.info(
                    f"fix_fable_cot: cleaned leaked CoT from "
                    f"{total_cleaned} assistant message(s) in context history"
                )

        except Exception as e:
            logger.error(f"fix_fable_cot context fix failed: {e}")
