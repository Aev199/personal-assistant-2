"""Error handling service with user notifications and rate limiting.

This module provides centralized error handling with:
- Error classification by type (network, auth, database, validation)
- User-friendly messages in Russian
- Admin notifications for critical errors
- Rate limiting to prevent notification flooding
- Integration with structured logging
"""

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Dict, Tuple
import asyncpg
from collections import defaultdict, deque

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from bot.services.logger import StructuredLogger


@dataclass
class ErrorContext:
    """Context information for error handling.
    
    Attributes:
        user_id: Telegram user ID
        chat_id: Telegram chat ID
        handler_name: Name of the handler where error occurred
        message_text: Optional message text that triggered the error
        callback_data: Optional callback data that triggered the error
        task_id: Optional task ID related to the error
        project_id: Optional project ID related to the error
        timestamp: When the error occurred (UTC)
    """
    user_id: int
    chat_id: int
    handler_name: str
    message_text: Optional[str] = None
    callback_data: Optional[str] = None
    task_id: Optional[int] = None
    project_id: Optional[int] = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class RateLimiter:
    """Rate limiter for error notifications.
    
    Prevents notification flooding by limiting the number of notifications
    per user within a time window.
    """
    
    def __init__(self, max_notifications: int = 5, window_seconds: int = 60):
        """Initialize rate limiter.
        
        Args:
            max_notifications: Maximum notifications per user per window
            window_seconds: Time window in seconds
        """
        self.max_notifications = max_notifications
        self.window_seconds = window_seconds
        # Store timestamps of notifications per user
        self._notifications: Dict[int, deque] = defaultdict(lambda: deque(maxlen=max_notifications))
    
    def is_allowed(self, user_id: int) -> bool:
        """Check if notification is allowed for user.
        
        Args:
            user_id: Telegram user ID
        
        Returns:
            True if notification is allowed, False if rate limit exceeded
        """
        now = time.time()
        user_notifications = self._notifications[user_id]
        
        # Remove old notifications outside the window
        while user_notifications and user_notifications[0] < now - self.window_seconds:
            user_notifications.popleft()
        
        # Check if limit exceeded
        if len(user_notifications) >= self.max_notifications:
            return False
        
        # Record this notification
        user_notifications.append(now)
        return True
    
    def get_remaining_time(self, user_id: int) -> int:
        """Get remaining time until next notification is allowed.
        
        Args:
            user_id: Telegram user ID
        
        Returns:
            Seconds until next notification is allowed, or 0 if allowed now
        """
        now = time.time()
        user_notifications = self._notifications[user_id]
        
        if len(user_notifications) < self.max_notifications:
            return 0
        
        # Time until oldest notification expires
        oldest = user_notifications[0]
        remaining = int((oldest + self.window_seconds) - now)
        return max(0, remaining)


class ErrorHandler:
    """Centralized error handling with user notifications.
    
    This service handles errors throughout the bot application by:
    - Classifying errors by type
    - Generating user-friendly messages in Russian
    - Sending notifications to users via Telegram
    - Sending detailed error reports to admin
    - Logging all errors with structured context
    - Implementing rate limiting to prevent notification flooding
    
    Example:
        error_handler = ErrorHandler(bot, logger, admin_id=123456789)
        
        try:
            await some_operation()
        except Exception as e:
            context = ErrorContext(
                user_id=message.from_user.id,
                chat_id=message.chat.id,
                handler_name="task_create",
                task_id=42
            )
            await error_handler.handle_error(
                e, context,
                notify_user=True,
                notify_admin=True
            )
    """
    
    def __init__(
        self,
        bot: Bot,
        logger: StructuredLogger,
        admin_id: int,
        max_notifications_per_minute: int = 5
    ):
        """Initialize error handler.
        
        Args:
            bot: Telegram bot instance for sending notifications
            logger: Structured logger for error logging
            admin_id: Telegram user ID of admin for critical notifications
            max_notifications_per_minute: Maximum notifications per user per minute
        """
        self.bot = bot
        self.logger = logger
        self.admin_id = admin_id
        self.rate_limiter = RateLimiter(
            max_notifications=max_notifications_per_minute,
            window_seconds=60
        )
    
    def _classify_error(self, error: Exception) -> str:
        """Classify error by type.
        
        Args:
            error: The exception to classify
        
        Returns:
            Error classification: network, auth, database, validation, or unknown
        """
        error_type = type(error).__name__
        error_msg = str(error).lower()
        
        # Database errors (check first to avoid confusion with connection errors)
        if any(keyword in error_type.lower() for keyword in ['postgres', 'database', 'sql']):
            return 'database'
        if any(keyword in error_msg for keyword in ['database', 'connection pool', 'postgres']):
            return 'database'
        
        # Authentication errors
        if any(keyword in error_type.lower() for keyword in ['auth', 'unauthorized', 'forbidden']):
            return 'auth'
        if any(keyword in error_msg for keyword in ['401', '403', 'unauthorized', 'forbidden', 'auth']):
            return 'auth'
        
        # Network errors
        if any(keyword in error_type.lower() for keyword in ['timeout', 'connection', 'network']):
            return 'network'
        if any(keyword in error_msg for keyword in ['timeout', 'connection', 'network']):
            return 'network'
        
        # Validation errors
        if any(keyword in error_type.lower() for keyword in ['validation', 'value', 'type']):
            return 'validation'
        
        return 'unknown'
    
    def get_user_message(self, error: Exception) -> str:
        """Generate user-friendly error message in Russian.
        
        Args:
            error: The exception that occurred
        
        Returns:
            User-friendly error message in Russian
        """
        error_type = type(error).__name__
        error_msg = str(error).lower()
        classification = self._classify_error(error)
        
        # Specific error messages for known scenarios
        
        # WebDAV timeout
        if 'timeout' in error_type.lower() and any(keyword in error_msg for keyword in ['webdav', 'obsidian', 'sync']):
            return "⚠️ Синхронизация с Obsidian заняла слишком много времени. Попробуйте позже."
        
        # Generic timeout
        if 'timeout' in error_type.lower():
            return "⚠️ Операция заняла слишком много времени. Попробуйте позже."
        
        # Google Tasks auth error
        if 'google' in error_msg and classification == 'auth':
            return "🔒 Ошибка авторизации Google Tasks. Требуется повторная настройка."
        
        # Database connection error
        if classification == 'database':
            return "❌ Временная проблема с базой данных. Попробуйте через минуту."
        
        # Generic classification-based messages
        if classification == 'network':
            return "🌐 Проблема с сетевым подключением. Проверьте интернет и попробуйте снова."
        
        if classification == 'auth':
            return "🔒 Ошибка авторизации. Проверьте настройки доступа."
        
        if classification == 'validation':
            return "⚠️ Некорректные данные. Проверьте введённую информацию."
        
        # Generic error message
        return "❌ Произошла ошибка. Попробуйте позже или обратитесь к администратору."
    
    def get_admin_message(self, error: Exception, context: ErrorContext) -> str:
        """Generate detailed error message for admin.
        
        Args:
            error: The exception that occurred
            context: Error context with metadata
        
        Returns:
            Detailed technical error message for admin
        """
        import traceback
        
        error_type = type(error).__name__
        error_msg = str(error)
        classification = self._classify_error(error)
        
        # Build detailed message
        lines = [
            "🚨 <b>Error Report</b>",
            "",
            f"<b>Type:</b> {error_type}",
            f"<b>Classification:</b> {classification}",
            f"<b>Message:</b> {error_msg}",
            "",
            "<b>Context:</b>",
            f"• User ID: {context.user_id}",
            f"• Chat ID: {context.chat_id}",
            f"• Handler: {context.handler_name}",
        ]
        
        if context.task_id:
            lines.append(f"• Task ID: {context.task_id}")
        if context.project_id:
            lines.append(f"• Project ID: {context.project_id}")
        if context.message_text:
            # Truncate long messages
            msg_preview = context.message_text[:100]
            if len(context.message_text) > 100:
                msg_preview += "..."
            lines.append(f"• Message: {msg_preview}")
        if context.callback_data:
            lines.append(f"• Callback: {context.callback_data}")
        
        lines.append(f"• Timestamp: {context.timestamp.isoformat()}")
        
        # Add traceback (truncated)
        if hasattr(error, '__traceback__') and error.__traceback__:
            tb = ''.join(traceback.format_tb(error.__traceback__))
            # Truncate to last 500 chars to avoid message length limits
            if len(tb) > 500:
                tb = "..." + tb[-500:]
            lines.append("")
            lines.append("<b>Traceback:</b>")
            lines.append(f"<pre>{tb}</pre>")
        
        return "\n".join(lines)
    
    async def _send_user_notification(
        self,
        user_id: int,
        chat_id: int,
        message: str,
        db_pool: Optional[asyncpg.Pool] = None,
    ) -> bool:
        """Send notification to user.
        
        Args:
            user_id: Telegram user ID
            chat_id: Telegram chat ID
            message: Message to send
            db_pool: Asyncpg pool (to show SPA toast instead of plain message)
        
        Returns:
            True if notification sent successfully, False otherwise
        """
        # Check rate limit
        if not self.rate_limiter.is_allowed(user_id):
            remaining = self.rate_limiter.get_remaining_time(user_id)
            self.logger.warning(
                "User notification rate limit exceeded",
                user_id=user_id,
                remaining_seconds=remaining
            )
            return False
        
        try:
            if db_pool:
                from bot.ui.screens import ui_render_home
                from bot.ui.state import ui_get_state, ui_payload_with_toast, ui_set_state
                
                async with db_pool.acquire() as conn:
                    state = await ui_get_state(conn, chat_id)
                    payload = ui_payload_with_toast(state["ui_payload"], message, ttl_sec=30)
                    await ui_set_state(conn, chat_id, ui_payload=payload)
                
                # Attempt to render home screen with the toast
                try:
                    import aiogram
                    from aiogram.types import Message, Chat
                    mock_chat = Chat(id=chat_id, type="private")
                    mock_msg = Message(message_id=0, date=datetime.now(), chat=mock_chat, text="/start")
                    # We inject the bot into the message object for aiogram compatibility without context
                    # if needed, though ui_render_home now extracts bot from message.bot
                except Exception:
                    mock_msg = None
                    pass
                
                if mock_msg:
                    try:
                        # Use a mock message just to pass the chat identification, the UI renderer extracts id
                        await ui_render_home(mock_msg, db_pool)
                        return True
                    except Exception as render_err:
                        self.logger.warning("Failed to render error toast to home screen", error=render_err)
                        # Fall through to plain text below

            # Plain text fallback
            await self.bot.send_message(
                chat_id=chat_id,
                text=message,
                parse_mode=None  # Plain text for user messages
            )
            return True
        except TelegramAPIError as e:
            self.logger.error(
                "Failed to send user notification",
                error=e,
                user_id=user_id,
                chat_id=chat_id
            )
            return False
    
    async def _send_admin_notification(
        self,
        message: str
    ) -> bool:
        """Send notification to admin.
        
        Args:
            message: Message to send (HTML formatted)
        
        Returns:
            True if notification sent successfully, False otherwise
        """
        try:
            await self.bot.send_message(
                chat_id=self.admin_id,
                text=message,
                parse_mode="HTML"
            )
            return True
        except TelegramAPIError as e:
            self.logger.error(
                "Failed to send admin notification",
                error=e,
                admin_id=self.admin_id
            )
            return False
    
    async def handle_error(
        self,
        error: Exception,
        context: ErrorContext,
        *,
        notify_user: bool = True,
        notify_admin: bool = False,
        db_pool: Optional[asyncpg.Pool] = None,
    ) -> None:
        """Handle error with appropriate logging and notifications.
        
        This is the main entry point for error handling. It:
        1. Classifies the error
        2. Logs the error with structured context
        3. Sends user-friendly notification to user (if enabled)
        4. Sends detailed error report to admin (if enabled)
        
        Args:
            error: The exception that occurred
            context: Error context with metadata
            notify_user: Whether to send notification to user
            notify_admin: Whether to send notification to admin
        """
        classification = self._classify_error(error)
        
        # Log error with structured context
        self.logger.error(
            f"Error in {context.handler_name}",
            error=error,
            error_classification=classification,
            user_id=context.user_id,
            chat_id=context.chat_id,
            handler_name=context.handler_name,
            task_id=context.task_id,
            project_id=context.project_id,
            message_text=context.message_text,
            callback_data=context.callback_data,
            timestamp=context.timestamp.isoformat()
        )
        
        # Send notifications concurrently
        notification_tasks = []
        
        if notify_user:
            user_message = self.get_user_message(error)
            notification_tasks.append(
                self._send_user_notification(
                    context.user_id,
                    context.chat_id,
                    user_message,
                    db_pool=db_pool,
                )
            )
        
        if notify_admin:
            admin_message = self.get_admin_message(error, context)
            notification_tasks.append(
                self._send_admin_notification(admin_message)
            )
        
        # Wait for notifications to complete (with timeout)
        if notification_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*notification_tasks, return_exceptions=True),
                    timeout=2.0  # 2 second timeout for notifications
                )
            except asyncio.TimeoutError:
                self.logger.warning(
                    "Notification delivery timeout",
                    user_id=context.user_id,
                    handler_name=context.handler_name
                )


# Convenience function for creating error handler
def create_error_handler(
    bot: Bot,
    admin_id: int,
    logger: Optional[StructuredLogger] = None,
    max_notifications_per_minute: int = 5
) -> ErrorHandler:
    """Create an error handler instance.
    
    Args:
        bot: Telegram bot instance
        admin_id: Telegram user ID of admin
        logger: Optional structured logger (creates default if not provided)
        max_notifications_per_minute: Maximum notifications per user per minute
    
    Returns:
        ErrorHandler instance
    """
    if logger is None:
        from bot.services.logger import get_logger
        logger = get_logger("error_handler")
    
    return ErrorHandler(
        bot=bot,
        logger=logger,
        admin_id=admin_id,
        max_notifications_per_minute=max_notifications_per_minute
    )
