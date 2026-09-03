"""Unit tests for interfaces/telegram_bot.py command handlers.

Focuses on correctness-critical behaviors:
    - Unauthorized chats cannot issue control commands.
    - Malformed command arguments are rejected.
    - /kill and /resume invoke real control actions.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import TimedOut

from interfaces.telegram_bot import TelegramControlPlane


class _FakeMessage:
    def __init__(self) -> None:
        self.reply_text = AsyncMock()


def _build_update(chat_id: int):
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat_id),
        message=_FakeMessage(),
    )


def _build_context(args: list[str] | None = None):
    return SimpleNamespace(args=args or [])


@pytest.fixture
def status_provider():
    return lambda: {
        "trading_enabled": True,
        "evaluations_count": 10,
        "profitable_signals_count": 2,
        "executions_count": 1,
        "risk": {
            "is_paused": False,
            "pause_reason": None,
            "daily_pnl_usdt": "1.23",
        },
    }


@pytest.fixture
def pnl_provider():
    return lambda: Decimal("1.23")


@pytest.fixture
def control_action():
    return AsyncMock()


@pytest.fixture
def telegram_bot(status_provider, pnl_provider, control_action):
    return TelegramControlPlane(
        token="123456:ABCdef",
        authorized_chat_id="999",
        status_provider=status_provider,
        control_action=control_action,
        pnl_provider=pnl_provider,
    )


@pytest.mark.asyncio
async def test_kill_rejects_unauthorized_chat(telegram_bot, control_action):
    """Unauthorized chat cannot trigger /kill action."""
    update = _build_update(chat_id=111)
    context = _build_context()

    await telegram_bot.handle_kill(update, context)

    control_action.assert_not_awaited()
    update.message.reply_text.assert_awaited_once()
    assert "Unauthorized" in update.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_kill_rejects_malformed_arguments(telegram_bot, control_action):
    """/kill with unexpected arguments is rejected instead of silently ignored."""
    update = _build_update(chat_id=999)
    context = _build_context(args=["now"])

    await telegram_bot.handle_kill(update, context)

    control_action.assert_not_awaited()
    update.message.reply_text.assert_awaited_once_with("Usage: /kill")


@pytest.mark.asyncio
async def test_kill_invokes_control_action(telegram_bot, control_action):
    """Authorized /kill must invoke real control action callback."""
    update = _build_update(chat_id=999)
    context = _build_context()

    await telegram_bot.handle_kill(update, context)

    control_action.assert_awaited_once()
    called_enabled, called_reason, called_force = control_action.await_args.args
    assert called_enabled is False
    assert "/kill" in called_reason
    assert called_force is False


@pytest.mark.asyncio
async def test_resume_invokes_control_action_force_resume(telegram_bot, control_action):
    """Authorized /resume must invoke control action with force resume semantics."""
    update = _build_update(chat_id=999)
    context = _build_context()

    await telegram_bot.handle_resume(update, context)

    control_action.assert_awaited_once()
    called_enabled, called_reason, called_force = control_action.await_args.args
    assert called_enabled is True
    assert "/resume" in called_reason
    assert called_force is True


@pytest.mark.asyncio
async def test_status_and_pnl_commands_return_expected_payload(telegram_bot):
    """/status and /pnl return runtime and pnl fields."""
    status_update = _build_update(chat_id=999)
    pnl_update = _build_update(chat_id=999)

    await telegram_bot.handle_status(status_update, _build_context())
    await telegram_bot.handle_pnl(pnl_update, _build_context())

    status_update.message.reply_text.assert_awaited_once()
    status_text = status_update.message.reply_text.await_args.args[0]
    assert "evaluations=10" in status_text
    assert "executions=1" in status_text

    pnl_update.message.reply_text.assert_awaited_once_with("daily_pnl_usdt=1.23")


@pytest.mark.asyncio
async def test_reply_timeout_does_not_raise(telegram_bot):
    """Transient Telegram timeout in reply path is logged and swallowed."""
    update = _build_update(chat_id=999)
    update.message.reply_text = AsyncMock(side_effect=TimedOut("timed out"))

    await telegram_bot.handle_status(update, _build_context())

    update.message.reply_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_global_error_handler_does_not_raise(telegram_bot):
    """Application-level Telegram error hook logs callback exceptions safely."""
    context = SimpleNamespace(error=RuntimeError("handler failed"))
    await telegram_bot._on_error(update=object(), context=context)