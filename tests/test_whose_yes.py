"""A change that waits for a yes waits for the person whose request it answers. When one turn
answers messages from two people, the newest message's sender used to get the yes, even when the
change answered the other one's request."""

import asyncio

import pytest
import yaml

from .test_conversations import TO_BOT
from .test_settings_stores_bot import msg, run
from .test_turns import buttons, cfg, sends, setup, tap, toasts


def block(**extra):
    return (
        "```change\n" + yaml.safe_dump({**extra, "set": {"household.lunch_time": "14:30"}}) + "```"
    )


def burst(b, first_user, newest_user):
    """Two people write to the bot within the debounce: one turn answers both."""
    b.debounce = 0.3

    async def go():
        first = asyncio.create_task(
            b.handle(None, msg("@chefbot comemos a las 14:30?", user=first_user, message_id=1))
        )
        await asyncio.sleep(0.1)
        await b.handle(None, msg("@chefbot y la cena?", user=newest_user, message_id=2, **TO_BOT))
        await first

    run(go())
    return next(p for p in sends(b, -100) if p.get("reply_markup"))


def waits_for(b, asked, pid, other):
    """Only `pid` can tap the yes: anyone else is told whose it is, and nothing changes."""
    yes = buttons(asked)[0]["callback_data"]
    run(tap(b, yes, other, 1))
    assert b.t("tap_not_yours", name=b.mention(pid)) in toasts(b)
    assert b.pending(-100)  # still waiting
    run(tap(b, yes, b.data.settings.bot.people[pid], 1))
    assert not b.pending(-100)  # settled by their tap


def test_the_yes_is_for_the_person_the_change_block_names(data_dir, tmp_path):
    b, _ = setup(data_dir, tmp_path)
    cfg(data_dir, text="Comida a las 14:30.\n" + block(**{"for": "ana"}))
    asked = burst(b, 8, 7)  # Ana asked; Luis wrote last
    assert b.mention("ana") in asked["text"] and b.mention("luis") not in asked["text"]
    waits_for(b, asked, "ana", 7)


@pytest.mark.parametrize("cook", ["ana", "luis"])  # the first who wrote, and the newest
def test_unclear_whose_it_is_the_cook_gets_the_yes(data_dir, tmp_path, cook):
    b, _ = setup(data_dir, tmp_path, reminders_for=cook)
    cfg(data_dir, text="Comida a las 14:30.\n" + block())
    asked = burst(b, 8, 7)  # Ana first, Luis newest
    other = "luis" if cook == "ana" else "ana"
    assert b.mention(cook) in asked["text"] and b.mention(other) not in asked["text"]
    waits_for(b, asked, cook, b.data.settings.bot.people[other])


def test_unclear_and_no_cook_set_the_first_who_wrote_gets_it(data_dir, tmp_path):
    b, _ = setup(data_dir, tmp_path)
    cfg(data_dir, text="Comida a las 14:30.\n" + block())
    asked = burst(b, 8, 7)  # Ana first, Luis newest
    assert b.mention("ana") in asked["text"] and b.mention("luis") not in asked["text"]
    waits_for(b, asked, "ana", 7)


def test_one_person_in_the_turn_keeps_their_own_yes(data_dir, tmp_path):
    b, _ = setup(data_dir, tmp_path, reminders_for="ana")
    cfg(data_dir, text="Comida a las 14:30.\n" + block())
    asked = burst(b, 7, 7)
    assert b.mention("luis") in asked["text"]  # the cook setting is only for an unclear turn
    waits_for(b, asked, "luis", 8)
