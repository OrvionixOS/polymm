"""
Shared fixtures for integration tests.

These tests require network access to Polymarket's Gamma API and Supabase.
Run through proxychains4 to avoid SSL issues:
    proxychains4 -q python -m pytest tests/integration/ -v -s
"""
import asyncio
import pytest
from src.polymarket.market_client import PolymarketEsportsClient
from src.services.odds_service import OddsService


@pytest.fixture
def event_loop():
    """Create an event loop for the test session."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
async def poly_client():
    """Create and clean up a PolymarketEsportsClient."""
    client = PolymarketEsportsClient()
    yield client
    await client.close()


@pytest.fixture
async def odds_service():
    """Create OddsService and fetch v2 odds from Supabase.
    
    Populates _v2_cache with live multi-market odds (h2h, spreads, totals, etc.).
    Does NOT start the background refresh loop.
    """
    service = OddsService()
    await service._fetch_from_supabase_v2()
    yield service
