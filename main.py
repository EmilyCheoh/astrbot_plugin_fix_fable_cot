import re

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
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

    @filter.on_llm_response()
    async def fix_fable_cot(self, event: AstrMessageEvent, resp):
        try:
            text = getattr(resp, "_completion_text", None)
            if not text or not isinstance(text, str):
                return resp

            match = LEAK_BOUNDARY.search(text)
            if not match:
                return resp

            # Split right after the period: everything up to and including '.'
            # is leaked CoT; everything after is the actual response.
            split_pos = match.start() + 2
            leaked_cot = text[:split_pos].strip()
            actual_response = text[split_pos:].strip()

            if not leaked_cot or not actual_response:
                return resp

            # Send leaked CoT as forwarded message
            nodes = [
                Node(
                    uin=0,
                    name="🤖💭Fable's reasoning process",
                    content=[Plain(leaked_cot)],
                )
            ]
            await event.send(event.chain_result(nodes))

            # Patch the response: replace text with actual response only
            resp._completion_text = actual_response

            if resp.result_chain is not None:
                resp.result_chain = MessageChain().message(actual_response)

        except Exception as e:
            logger.error(f"fix_fable_cot failed: {e}")

        return resp
