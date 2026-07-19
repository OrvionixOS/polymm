"""
Main Entry Point - Polymarket Trading Bot

Entry point that selects and runs the appropriate bot strategy.

Usage:
    python src/main.py           # Run SportsBot (pre-match, esports + rugby)
    python src/main.py --live    # Run LiveBot (live matches only)
    python src/main.py --spread  # Run SpreadBot (weather markets)
"""
import argparse
import asyncio
import os
import signal
import sys

# Add project root to path BEFORE importing src modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.bots import SportsBot, LiveBot, SpreadBot


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Polymarket Trading Bot")
    parser.add_argument("--live", action="store_true", help="Run LiveBot (live matches only)")
    parser.add_argument("--spread", action="store_true", help="Run SpreadBot (weather markets)")
    args = parser.parse_args()
    
    # Select bot based on CLI args
    if args.spread:
        print("🌡️ Starting SpreadBot (weather markets)...")
        bot = SpreadBot()
    elif args.live:
        print("🔴 Starting LiveBot (live matches only)...")
        from src.infra.redis_cache import configure_redis_env_prefix
        configure_redis_env_prefix("LIVE_")
        bot = LiveBot()
    else:
        print("📊 Starting SportsBot (pre-match)...")
        from src.infra.redis_cache import configure_redis_env_prefix
        configure_redis_env_prefix("SPORT_")
        bot = SportsBot()
    
    # Store the main task so we can cancel it on shutdown
    main_task = None
    
    def shutdown_handler(sig):
        print(f"\n⚠️ Received {sig.name}, shutting down...")
        if main_task:
            main_task.cancel()
    
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda s=sig: shutdown_handler(s))
    
    main_task = asyncio.current_task()
    try:
        await bot.run()
    except asyncio.CancelledError:
        pass
    finally:
        # Cancel all remaining tasks (reconnect timers, etc.)
        # This prevents "Task was destroyed but it is pending!" noise
        for task in asyncio.all_tasks():
            if task is not asyncio.current_task() and not task.done():
                task.cancel()
        # Give cancelled tasks a chance to clean up
        await asyncio.sleep(0.1)


if __name__ == "__main__":
    asyncio.run(main())
