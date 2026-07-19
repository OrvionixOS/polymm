"""
Health server - HTTP health check endpoint for deployment platforms.
"""
import asyncio
from datetime import datetime, timezone
from typing import Callable

from aiohttp import web


class HealthServer:
    """
    HTTP health check server for Fly.io and other deployment platforms.
    
    Provides:
    - GET /health - Returns JSON status
    - GET / - Same as /health
    
    Requires injection of:
    - get_status_fn: Callable that returns status dict
    """
    
    def __init__(
        self,
        get_status_fn: Callable[[], dict],
        port: int = 8080,
    ):
        self._get_status = get_status_fn
        self._port = port
        self._running = False
        self._runner = None
    
    async def start(self):
        """Start the health server."""
        async def health_handler(request):
            status = self._get_status()
            return web.json_response(status)
        
        app = web.Application()
        app.router.add_get("/health", health_handler)
        app.router.add_get("/", health_handler)
        
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", self._port)
        await site.start()
        self._running = True
        print(f"✅ Health server running on :{self._port}")
    
    async def run_forever(self, is_running_fn: Callable[[], bool]):
        """Keep running while bot is active."""
        while is_running_fn():
            await asyncio.sleep(1)
    
    async def stop(self):
        """Stop the health server."""
        self._running = False
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
