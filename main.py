"""
Fable CoT Leak Fix

Fable 模型偶尔将第二层思考链泄漏到正文中，与实际回复粘连，
中间缺少空格（例如 "dissect.Yes" "right?The" "now!She"）。

本插件在两个阶段修复：
1. on_llm_response — 实时 cosmetic：
   检测泄漏边界，将泄漏的 CoT 以合并转发消息发送，
   并将回复文本替换为干净版本。
2. on_llm_request — 下一轮结构修复：
   扫描 req.contexts 中的 assistant 消息，
   将残留的泄漏 CoT 从上下文历史中清除，
   确保 LLM 收到的上下文是干净的。

安全措施：
- 跳过代码块（```...```）和行内代码（`...`）内的匹配
- 仅在配置中指定的模型上启用

F(A) = A(F)
"""

import re

from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.message_components import Node, Plain
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register

# Pattern: lowercase letter + sentence-ending punctuation + uppercase letter,
# with no space between. Covers period, question mark, and exclamation mark.
LEAK_BOUNDARY = re.compile(r"[a-z][.?!][A-Z]")

# Patterns for detecting code spans to skip
CODE_BLOCK = re.compile(r"```[\s\S]*?```")
INLINE_CODE = re.compile(r"`[^`]+`")


def _find_code_ranges(text: str) -> list[tuple[int, int]]:
    """Find all character ranges that are inside code blocks or inline code."""
    ranges = []
    for m in CODE_BLOCK.finditer(text):
        ranges.append((m.start(), m.end()))
    for m in INLINE_CODE.finditer(text):
        ranges.append((m.start(), m.end()))
    return ranges


def _is_in_code(pos: int, code_ranges: list[tuple[int, int]]) -> bool:
    """Check if a position falls inside any code range."""
    return any(start <= pos < end for start, end in code_ranges)


def _split_leaked_cot(text: str) -> tuple[str, str] | None:
    """
    If the text contains a leaked CoT boundary (outside code spans),
    return (leaked_cot, actual_response). Otherwise return None.
    """
    code_ranges = _find_code_ranges(text)

    for match in LEAK_BOUNDARY.finditer(text):
        if _is_in_code(match.start(), code_ranges):
            continue

        # Found a valid boundary outside code
        split_pos = match.start() + 2
        leaked_cot = text[:split_pos].strip()
        actual_response = text[split_pos:].strip()

        if not leaked_cot or not actual_response:
            continue

        return leaked_cot, actual_response

    return None


@register(
    "astrbot_plugin_fix_fable_cot",
    "Felis Abyssalis & Abyss AI",
    "Fable 模型泄漏的第二层 CoT 从正文中分离出来，以合并转发消息发送",
    "1.0.0",
)
class FixFableCotPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}

    def _is_affected_model(self, provider_id: str) -> bool:
        """Check if the current provider matches any affected model in config."""
        affected = self.config.get("affected_models", [])
        if not affected:
            return False
        provider_lower = provider_id.lower()
        return any(model.lower() in provider_lower for model in affected)

    # ------------------------------------------------------------------
    # Stage 1: Cosmetic fix — split leaked CoT from LLM response
    # ------------------------------------------------------------------

    @filter.on_llm_response()
    async def fix_display(self, event: AstrMessageEvent, resp):
        try:
            # Model gate
            umo = event.unified_msg_origin
            provider_id = await self.context.get_current_chat_provider_id(umo=umo)
            if not provider_id or not self._is_affected_model(provider_id):
                return resp

            text = getattr(resp, "_completion_text", None)
            if not text or not isinstance(text, str):
                return resp

            split = _split_leaked_cot(text)
            if not split:
                return resp

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

            # Patch the response text
            resp._completion_text = actual_response
            if resp.result_chain is not None:
                resp.result_chain = MessageChain().message(actual_response)

        except Exception as e:
            logger.error(f"fix_fable_cot display fix failed: {e}")

        return resp

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
            # Model gate
            umo = event.unified_msg_origin
            provider_id = await self.context.get_current_chat_provider_id(umo=umo)
            if not provider_id or not self._is_affected_model(provider_id):
                return

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
