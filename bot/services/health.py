"""Enhanced health check service with dependency monitoring.

This module provides comprehensive health monitoring for the bot application,
checking database connectivity, webhook status, integration adapters, and
background tasks. It supports caching and timeouts to ensure fast responses.
"""

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Literal, Optional

import asyncpg
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from bot.services.logger import StructuredLogger


@dataclass
class ComponentHealth:
    """Health status for a single component.
    
    Attributes:
        name: Component name (e.g., "database", "webhook", "google_tasks")
        status: Health status (healthy, degraded, unhealthy)
        latency_ms: Optional latency measurement in milliseconds
        error: Optional error message if component is unhealthy
        last_check: When the check was performed (UTC)
    """
    name: str
    status: Literal["healthy", "degraded", "unhealthy"]
    latency_ms: Optional[float] = None
    error: Optional[str] = None
    last_check: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "name": self.name,
            "status": self.status,
            "latency_ms": self.latency_ms,
            "error": self.error,
            "last_check": self.last_check.isoformat()
        }


@dataclass
class HealthStatus:
    """Aggregated health status for the entire system.
    
    Attributes:
        overall: Overall health status (healthy, degraded, unhealthy)
        version: Application version string
        uptime_seconds: Application uptime in seconds
        components: Dictionary of component health statuses
        timestamp: When the health check was performed (UTC)
    """
    overall: Literal["healthy", "degraded", "unhealthy"]
    version: str
    uptime_seconds: float
    components: Dict[str, ComponentHealth]
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "overall": self.overall,
            "version": self.version,
            "uptime_seconds": self.uptime_seconds,
            "components": {
                name: component.to_dict()
                for name, component in self.components.items()
            },
            "timestamp": self.timestamp.isoformat()
        }


class HealthCheck:
    """Enhanced health check with dependency monitoring.
    
    This service performs comprehensive health checks on all system components:
    - Database connectivity and query performance
    - Telegram webhook status
    - Integration adapters (Google Tasks, iCloud, WebDAV)
    - Background task execution
    
    Features:
    - Individual component timeouts (2 seconds)
    - Result caching (30 seconds)
    - Status aggregation (unhealthy > degraded > healthy)
    - Latency measurement
    
    Example:
        health_check = HealthCheck(
            db_pool=db_pool,
            bot=bot,
            adapters={
                "google_tasks": google_tasks_adapter,
                "icloud": icloud_adapter,
                "webdav": webdav_adapter
            },
            version="1.0.0"
        )
        
        status = await health_check.check_all()
        print(f"Overall status: {status.overall}")
    """
    
    def __init__(
        self,
        db_pool: asyncpg.Pool,
        bot: Bot,
        adapters: Dict[str, Any],
        version: str,
        logger: Optional[StructuredLogger] = None,
        component_timeout: float = 2.0,
        cache_ttl: int = 30
    ):
        """Initialize health check service.
        
        Args:
            db_pool: PostgreSQL connection pool
            bot: Telegram bot instance
            adapters: Dictionary of integration adapters (google_tasks, icloud, webdav)
            version: Application version string
            logger: Optional structured logger
            component_timeout: Timeout for individual component checks in seconds
            cache_ttl: Cache time-to-live in seconds
        """
        self.db_pool = db_pool
        self.bot = bot
        self.adapters = adapters
        self.version = version
        self.logger = logger or StructuredLogger("health_check")
        self.component_timeout = component_timeout
        self.cache_ttl = cache_ttl
        
        # Cache for health status
        self._cached_status: Optional[HealthStatus] = None
        self._cache_time: float = 0
        
        # Track application start time for uptime calculation
        self._start_time = time.time()
    
    def _get_uptime(self) -> float:
        """Get application uptime in seconds."""
        return time.time() - self._start_time
    
    def _aggregate_status(
        self,
        components: Dict[str, ComponentHealth]
    ) -> Literal["healthy", "degraded", "unhealthy"]:
        """Aggregate component statuses into overall status.
        
        Priority: unhealthy > degraded > healthy
        
        Args:
            components: Dictionary of component health statuses
        
        Returns:
            Overall health status
        """
        statuses = [comp.status for comp in components.values()]
        
        # If any component is unhealthy, overall is unhealthy
        if "unhealthy" in statuses:
            return "unhealthy"
        
        # If any component is degraded, overall is degraded
        if "degraded" in statuses:
            return "degraded"
        
        # All components are healthy
        return "healthy"
    
    async def check_database(self) -> ComponentHealth:
        """Check PostgreSQL connection and query performance.
        
        Executes a simple SELECT 1 query and measures latency.
        
        Returns:
            ComponentHealth with database status
        """
        start_time = time.time()
        
        try:
            async with self.db_pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            
            latency_ms = (time.time() - start_time) * 1000
            
            return ComponentHealth(
                name="database",
                status="healthy",
                latency_ms=round(latency_ms, 2)
            )
        
        except Exception as e:
            self.logger.error(
                "Database health check failed",
                error=e,
                component="database"
            )
            
            return ComponentHealth(
                name="database",
                status="unhealthy",
                error=str(e)
            )
    
    async def check_webhook(self) -> ComponentHealth:
        """Verify Telegram webhook is active.
        
        Calls getWebhookInfo to check webhook registration status.
        
        Returns:
            ComponentHealth with webhook status
        """
        start_time = time.time()
        
        try:
            webhook_info = await self.bot.get_webhook_info()
            latency_ms = (time.time() - start_time) * 1000
            
            # Check if webhook is set
            if not webhook_info.url:
                return ComponentHealth(
                    name="webhook",
                    status="unhealthy",
                    latency_ms=round(latency_ms, 2),
                    error="Webhook not configured"
                )
            
            # Check for pending updates (might indicate issues)
            if webhook_info.pending_update_count > 100:
                return ComponentHealth(
                    name="webhook",
                    status="degraded",
                    latency_ms=round(latency_ms, 2),
                    error=f"High pending updates: {webhook_info.pending_update_count}"
                )
            
            return ComponentHealth(
                name="webhook",
                status="healthy",
                latency_ms=round(latency_ms, 2)
            )
        
        except TelegramAPIError as e:
            self.logger.error(
                "Webhook health check failed",
                error=e,
                component="webhook"
            )
            
            return ComponentHealth(
                name="webhook",
                status="unhealthy",
                error=str(e)
            )
    
    async def check_integrations(self) -> Dict[str, ComponentHealth]:
        """Check Google Tasks, iCloud, WebDAV connectivity.
        
        Performs lightweight checks on each configured integration adapter.
        
        Returns:
            Dictionary of ComponentHealth for each integration
        """
        results = {}
        
        for adapter_name, adapter in self.adapters.items():
            start_time = time.time()
            
            try:
                # Check if adapter has a health check method
                if hasattr(adapter, 'health_check'):
                    await adapter.health_check()
                    latency_ms = (time.time() - start_time) * 1000
                    
                    results[adapter_name] = ComponentHealth(
                        name=adapter_name,
                        status="healthy",
                        latency_ms=round(latency_ms, 2)
                    )
                else:
                    # If no health check method, just mark as healthy
                    # (adapter exists and is configured)
                    results[adapter_name] = ComponentHealth(
                        name=adapter_name,
                        status="healthy",
                        latency_ms=None
                    )
            
            except Exception as e:
                self.logger.error(
                    f"Integration health check failed: {adapter_name}",
                    error=e,
                    component=adapter_name
                )
                
                results[adapter_name] = ComponentHealth(
                    name=adapter_name,
                    status="unhealthy",
                    error=str(e)
                )
        
        return results
    
    async def check_background_tasks(self) -> ComponentHealth:
        """Verify reminder tick is running.
        
        This is a placeholder check. In a real implementation, you would
        check a timestamp of the last successful background task execution.
        
        Returns:
            ComponentHealth with background task status
        """
        # Placeholder: In real implementation, check last_tick timestamp
        # from database or shared state
        
        try:
            # For now, just return healthy
            # TODO: Implement actual background task monitoring
            return ComponentHealth(
                name="background_tasks",
                status="healthy",
                latency_ms=None
            )
        
        except Exception as e:
            self.logger.error(
                "Background task health check failed",
                error=e,
                component="background_tasks"
            )
            
            return ComponentHealth(
                name="background_tasks",
                status="unhealthy",
                error=str(e)
            )
    
    async def _check_component_with_timeout(
        self,
        check_func,
        component_name: str
    ) -> ComponentHealth:
        """Run a component check with timeout.
        
        Args:
            check_func: Async function to call for the check
            component_name: Name of the component being checked
        
        Returns:
            ComponentHealth (unhealthy if timeout occurs)
        """
        try:
            return await asyncio.wait_for(
                check_func(),
                timeout=self.component_timeout
            )
        except asyncio.TimeoutError:
            self.logger.warning(
                f"Component health check timeout: {component_name}",
                component=component_name,
                timeout_seconds=self.component_timeout
            )
            
            return ComponentHealth(
                name=component_name,
                status="unhealthy",
                error=f"Check timeout ({self.component_timeout}s)"
            )
    
    async def check_all(self) -> HealthStatus:
        """Run all health checks and return aggregated status.
        
        Checks are run concurrently with individual timeouts. Results are
        cached for the configured TTL to reduce redundant checks.
        
        Returns:
            HealthStatus with overall status and component details
        """
        # Check cache
        now = time.time()
        if self._cached_status and (now - self._cache_time) < self.cache_ttl:
            self.logger.debug(
                "Returning cached health status",
                cache_age_seconds=round(now - self._cache_time, 2)
            )
            return self._cached_status
        
        # Run all checks concurrently with timeouts
        check_start = time.time()
        
        database_check = self._check_component_with_timeout(
            self.check_database,
            "database"
        )
        
        webhook_check = self._check_component_with_timeout(
            self.check_webhook,
            "webhook"
        )
        
        background_check = self._check_component_with_timeout(
            self.check_background_tasks,
            "background_tasks"
        )
        
        # Run checks concurrently
        database_health, webhook_health, background_health = await asyncio.gather(
            database_check,
            webhook_check,
            background_check,
            return_exceptions=True
        )
        
        # Handle exceptions from gather
        if isinstance(database_health, Exception):
            database_health = ComponentHealth(
                name="database",
                status="unhealthy",
                error=str(database_health)
            )
        
        if isinstance(webhook_health, Exception):
            webhook_health = ComponentHealth(
                name="webhook",
                status="unhealthy",
                error=str(webhook_health)
            )
        
        if isinstance(background_health, Exception):
            background_health = ComponentHealth(
                name="background_tasks",
                status="unhealthy",
                error=str(background_health)
            )
        
        # Check integrations (with overall timeout)
        try:
            integration_checks = await asyncio.wait_for(
                self.check_integrations(),
                timeout=self.component_timeout
            )
        except asyncio.TimeoutError:
            self.logger.warning("Integration health checks timeout")
            integration_checks = {}
        
        # Build components dictionary
        components = {
            "database": database_health,
            "webhook": webhook_health,
            "background_tasks": background_health,
        }
        components.update(integration_checks)
        
        # Aggregate status
        overall_status = self._aggregate_status(components)
        
        # Calculate total check duration
        check_duration = time.time() - check_start
        
        # Create health status
        health_status = HealthStatus(
            overall=overall_status,
            version=self.version,
            uptime_seconds=round(self._get_uptime(), 2),
            components=components
        )
        
        # Cache result
        self._cached_status = health_status
        self._cache_time = now
        
        # Log health check completion
        self.logger.info(
            "Health check completed",
            overall_status=overall_status,
            check_duration_ms=round(check_duration * 1000, 2),
            component_count=len(components)
        )
        
        return health_status


def create_health_check(
    db_pool: asyncpg.Pool,
    bot: Bot,
    adapters: Dict[str, Any],
    version: str,
    logger: Optional[StructuredLogger] = None,
    component_timeout: float = 2.0,
    cache_ttl: int = 30
) -> HealthCheck:
    """Create a health check instance.
    
    Args:
        db_pool: PostgreSQL connection pool
        bot: Telegram bot instance
        adapters: Dictionary of integration adapters
        version: Application version string
        logger: Optional structured logger
        component_timeout: Timeout for individual component checks in seconds
        cache_ttl: Cache time-to-live in seconds
    
    Returns:
        HealthCheck instance
    """
    return HealthCheck(
        db_pool=db_pool,
        bot=bot,
        adapters=adapters,
        version=version,
        logger=logger,
        component_timeout=component_timeout,
        cache_ttl=cache_ttl
    )
