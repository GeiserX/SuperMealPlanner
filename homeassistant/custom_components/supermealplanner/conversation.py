"""The conversation agent: what Assist heard goes to the chef, and its answer is read out."""

from __future__ import annotations

from typing import Literal

from homeassistant.components import conversation
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import ChefConfigEntry
from .client import ChefError, Slow
from .const import DOMAIN
from .speech import BYE, DOWN, SLOW, asked_more, is_closing, lang, with_more

BUDGET = 120  # seconds we wait for the chef; Assist's own pipeline allows 300


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ChefConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add this person's agent."""
    async_add_entities([ChefAgent(entry)])


def device_info(entry: ChefConfigEntry) -> DeviceInfo:
    """One device per entry, holding the agent and the speech to text."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=f"Chef ({entry.title})",
        manufacturer="SuperMealPlanner",
    )


class ChefAgent(conversation.ConversationEntity, conversation.AbstractConversationAgent):
    """Says what it hears to the chef as this entry's person."""

    _attr_has_entity_name = True
    _attr_name = None

    def __init__(self, entry: ChefConfigEntry) -> None:
        """Set up the agent for one paired person."""
        self.entry = entry
        self._attr_unique_id = f"{entry.entry_id}-agent"
        self._attr_device_info = device_info(entry)

    @property
    def supported_languages(self) -> list[str] | Literal["*"]:
        """Any language: the chef answers in the household's own."""
        return MATCH_ALL

    async def async_added_to_hass(self) -> None:
        """Register as an agent Assist pipelines can pick."""
        await super().async_added_to_hass()
        conversation.async_set_agent(self.hass, self.entry, self)

    async def async_will_remove_from_hass(self) -> None:
        """Unregister."""
        conversation.async_unset_agent(self.hass, self.entry)
        await super().async_will_remove_from_hass()

    async def _async_handle_message(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
    ) -> conversation.ConversationResult:
        """Ask the chef, wait for the answer, and end it in a question to keep listening."""
        language = user_input.language
        previous = next(
            (
                c.content
                for c in reversed(chat_log.content[:-1])
                if isinstance(c, conversation.AssistantContent)
            ),
            None,
        )
        if asked_more(previous, language) and is_closing(user_input.text, language):
            speech = BYE[lang(language)]
        else:
            try:
                speech = with_more(
                    await self.entry.runtime_data.ask(user_input.text, BUDGET), language
                )
            except Slow:
                speech = SLOW[lang(language)]
            except ChefError:
                speech = DOWN[lang(language)]
        chat_log.async_add_assistant_content_without_tools(
            conversation.AssistantContent(agent_id=user_input.agent_id, content=speech)
        )
        return conversation.async_get_result_from_chat_log(user_input, chat_log)
