"""Telegram control-plane interface for DeltaQuant.

This module implements operator-facing commands for Phase 4:
    - /status
    - /kill
    - /resume
    - /pnl

Command handlers validate authorization and reject malformed arguments.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Awaitable, Callable

from loguru import logger
from telegram.error import TelegramError, TimedOut
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes


StatusProvider = Callable[[], dict]
ControlAction = Callable[[bool, str, bool], Awaitable[None]]
PnlProvider = Callable[[], Decimal]


class TelegramControlPlane:
    """Async Telegram bot wrapper for operator control commands.

    Attributes:
        token: Telegram bot token.
        authorized_chat_id: Only this chat ID may run control commands.
        status_provider: Callable returning current runtime status snapshot.
        control_action: Async callback to apply kill/resume state changes.
        pnl_provider: Callable returning current daily PnL from RiskManager.
    """

    def __init__(
        self,
        token: str,
        authorized_chat_id: str,
        status_provider: StatusProvider,
        control_action: ControlAction,
        pnl_provider: PnlProvider,
    ) -> None:
        """Initialize Telegram control-plane wrapper.

        Args:
            token: Telegram bot token.
            authorized_chat_id: Allowed chat id as configured in settings.
            status_provider: Runtime status provider callable.
            control_action: Callback to set trading enabled/disabled state.
            pnl_provider: Daily PnL provider callable.
        """
        self.token = token
        self.authorized_chat_id = str(authorized_chat_id)
        self.status_provider = status_provider
        self.control_action = control_action
        self.pnl_provider = pnl_provider

        self._application = Application.builder().token(token).build()
        self._application.add_handler(CommandHandler("status", self.handle_status))
        self._application.add_handler(CommandHandler("kill", self.handle_kill))
        self._application.add_handler(CommandHandler("resume", self.handle_resume))
        self._application.add_handler(CommandHandler("pnl", self.handle_pnl))
        self._application.add_error_handler(self._on_error)
        self._running = False

    async def start(self) -> None:
        """Start Telegram polling in the current event loop."""
        if self._running:
            return
        await self._application.initialize()
        await self._application.start()
        if self._application.updater is None:
            raise RuntimeError("Telegram updater is not available")
        await self._application.updater.start_polling(drop_pending_updates=True)
        self._running = True
        logger.info("telegram_bot_started")

    async def stop(self) -> None:
        """Stop Telegram polling and release resources."""
        if not self._running:
            return
        try:
            if self._application.updater is not None:
                await self._application.updater.stop()
            await self._application.stop()
            await self._application.shutdown()
        finally:
            self._running = False
            logger.info("telegram_bot_stopped")

    async def send_incident_alert(self, message: str) -> None:
        """Send incident or circuit-breaker alert to the authorized chat.

        Args:
            message: Alert body text.
        """
        try:
            await self._application.bot.send_message(
                chat_id=self.authorized_chat_id,
                text=message,
            )
        except Exception as exc:
            logger.error("telegram_alert_send_failed err='{}'", exc)

    async def handle_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /status command.

        Returns risk-manager status and orchestrator runtime counters.
        """
        if not await self._authorize(update):
            return
        if getattr(context, "args", None):
            await self._reply(update, "Usage: /status")
            return

        status = self.status_provider()
        risk = status.get("risk", {})
        message = (
            "DeltaQuant status\n"
            f"trading_enabled={status.get('trading_enabled')}\n"
            f"is_paused={risk.get('is_paused')}\n"
            f"pause_reason={risk.get('pause_reason')}\n"
            f"daily_pnl_usdt={risk.get('daily_pnl_usdt')}\n"
            f"evaluations={status.get('evaluations_count')}\n"
            f"profitable_signals={status.get('profitable_signals_count')}\n"
            f"executions={status.get('executions_count')}"
        )
        await self._reply(update, message)

    async def handle_kill(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /kill command.

        Applies kill switch through the control-plane callback.
        """
        if not await self._authorize(update):
            return
        if getattr(context, "args", None):
            await self._reply(update, "Usage: /kill")
            return

        reason = "[control-plane] Operator command /kill"
        await self.control_action(False, reason, False)
        await self._reply(update, "Trading paused via control-plane kill switch.")

    async def handle_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /resume command.

        Resumes trading through the control-plane callback.
        """
        if not await self._authorize(update):
            return
        if getattr(context, "args", None):
            await self._reply(update, "Usage: /resume")
            return

        reason = "[control-plane] Operator command /resume"
        await self.control_action(True, reason, True)
        await self._reply(update, "Trading resumed via control-plane.")

    async def handle_pnl(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /pnl command.

        Returns current cumulative daily PnL in USDT.
        """
        if not await self._authorize(update):
            return
        if getattr(context, "args", None):
            await self._reply(update, "Usage: /pnl")
            return

        pnl = self.pnl_provider()
        await self._reply(update, f"daily_pnl_usdt={pnl}")

    async def _authorize(self, update: Update) -> bool:
        """Authorize command sender chat id.

        Args:
            update: Incoming Telegram update.

        Returns:
            True if command is authorized, otherwise False.
        """
        chat = update.effective_chat
        if chat is None:
            return False
        if str(chat.id) != self.authorized_chat_id:
            logger.warning("telegram_unauthorized_chat chat_id={}", chat.id)
            await self._reply(update, "Unauthorized chat id.")
            return False
        return True

    async def _reply(self, update: Update, text: str) -> None:
        """Reply helper that tolerates missing payloads and transient API errors."""
        if update.message is not None:
            try:
                await update.message.reply_text(text)
            except TimedOut as exc:
                logger.warning("telegram_reply_timed_out err='{}'", exc)
            except TelegramError as exc:
                logger.error("telegram_reply_failed err='{}'", exc)

    async def _on_error(
        self,
        update: object,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Global error handler for python-telegram-bot callback exceptions.

        Args:
            update: Update object that triggered the error.
            context: Telegram callback context with exception details.
        """
        logger.error(
            "telegram_handler_exception err='{}' update='{}'",
            context.error,
            type(update).__name__,
        )
