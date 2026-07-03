import re

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import Node, Plain
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

# Pattern: lowercase letter + period + uppercase letter with no space between.
# This is the seam where Fable's leaked CoT glues onto the actual response.
LEAK_BOUNDARY = re.compile(r"[a-z]\.[A-Z]")


@register(
    "astrbot_plugin_fix_fable_cot",
    "Felis Abyssalis & Abyss AI",
    "Fable 模型泄漏的第二层 CoT 从正文中分离出来，以合并转发消息发送",
    "1.0.0",
)
class FixFableCotPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    @filter.on_decorating_result()
    async def fix_fable_cot(self, event: AstrMessageEvent):
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

            match = LEAK_BOUNDARY.search(full_text)
            if not match:
                return

            # Split right after the period: everything up to and including '.'
            # is leaked CoT; everything after is the actual response.
            split_pos = match.start() + 2
            leaked_cot = full_text[:split_pos].strip()
            actual_response = full_text[split_pos:].strip()

            if not leaked_cot or not actual_response:
                return

            # Send leaked CoT as forwarded message
            nodes = [
                Node(
                    uin=0,
                    name="🌬️Fable's reasoning process",
                    content=[Plain(leaked_cot)],
                )
            ]
            await event.send(event.chain_result(nodes))

            # Replace all Plain segments in the chain with the clean response.
            # Remove existing Plain components, then insert one clean Plain at
            # the position of the first original Plain component.
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
            logger.error(f"fix_fable_cot failed: {e}")
