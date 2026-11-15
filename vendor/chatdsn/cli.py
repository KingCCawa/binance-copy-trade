#!/usr/bin/env python3
"""
ChatDSN CLI - AI Agent Copy Trading for Binance Futures

A minimal Python implementation inspired by Karpathy's nanochat and Simon Willison's llm.
"""

import logging
import sys
import time
from pathlib import Path

# Setup logging first
def setup_logging(level: str = "INFO"):
    """Setup logging configuration."""
    log_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )


def main():
    """Main CLI entry point."""
    # Load environment first
    from .config import load_env_file, config
    load_env_file()

    # Setup logging
    setup_logging(logging.getLevelName(config.log_level))

    # Import click here after env is loaded
    try:
        import click
    except ImportError:
        print("Error: click library required. Install with: pip install click")
        sys.exit(1)

    @click.group()
    @click.version_option(version="2.0.0")
    def cli():
        """ChatDSN - AI Agent Copy Trading for Binance Futures"""
        pass

    @cli.command()
    def agents():
        """List all available AI trading agents."""
        from .api import ApiClient

        click.echo("Fetching available agents...")

        api = ApiClient()
        agents_list = api.list_agents()

        if not agents_list:
            click.echo("❌ Failed to fetch agents or no agents available")
            return

        click.echo(f"\n📊 Available AI Agents ({len(agents_list)}):\n")
        for i, agent in enumerate(agents_list, 1):
            click.echo(f"  {i}. {agent}")

        click.echo(f"\nUse: chatdsn follow <agent-name>")

    @cli.command()
    @click.argument("agent_name")
    @click.option("--poll", is_flag=True, help="Continuously poll for changes")
    @click.option("--interval", default=30, help="Poll interval in seconds")
    @click.option("--dry-run", is_flag=True, help="Simulate trades without execution")
    def follow(agent_name: str, poll: bool, interval: int, dry_run: bool):
        """Follow and copy trade an AI agent."""
        from .api import ApiClient
        from .binance import BinanceService
        from .trading import TradingExecutor
        from .capital import CapitalManager
        from .risk import RiskManager
        from .history import OrderHistoryManager
        from .follow import FollowService
        from .telegram import TelegramService

        click.echo(f"🤖 Following agent: {agent_name}")

        if dry_run:
            click.echo("⚠️  DRY RUN MODE - No actual trades will be executed")

        # Validate config
        valid, error = config.validate()
        if not valid:
            click.echo(f"❌ Configuration error: {error}")
            sys.exit(1)

        # Initialize services
        api = ApiClient()
        binance = BinanceService(
            config.binance_api_key,
            config.binance_api_secret,
            config.binance_testnet
        )

        # Check balance
        balance_info = binance.get_balance()
        if not balance_info:
            click.echo("❌ Failed to connect to Binance")
            sys.exit(1)

        click.echo(f"💰 Balance: {balance_info['available_balance']:.2f} USDT")
        click.echo(f"🔧 Mode: {config.capital_mode}")
        click.echo(f"🎯 Margin Mode: {config.margin_mode}")

        # Initialize managers
        capital = CapitalManager(
            config.capital_mode,
            config.total_margin,
            config.fixed_margin_per_coin
        )
        risk = RiskManager(config.default_price_tolerance_pct)
        history = OrderHistoryManager()

        # Initialize executor
        executor = TradingExecutor(binance, risk, capital, dry_run=dry_run)

        # Initialize follow service
        follower = FollowService(api, binance, executor, capital, history)

        # Setup Telegram if enabled
        if config.telegram_enabled:
            telegram = TelegramService(config.telegram_token, config.telegram_chat_id)
            follower.set_telegram(telegram)
            click.echo("📱 Telegram notifications enabled")

        # Run follow loop
        iteration = 0
        try:
            while True:
                iteration += 1
                click.echo(f"\n{'='*60}")
                click.echo(f"Iteration {iteration} - {time.strftime('%Y-%m-%d %H:%M:%S')}")
                click.echo(f"{'='*60}")

                # Execute follow
                results = follower.follow_once(agent_name)

                # Display results
                if "error" in results:
                    click.echo(f"❌ Error: {results['error']}")
                elif "status" in results and results["status"] == "initialized":
                    click.echo("✅ State initialized - will start copying on next iteration")
                else:
                    click.echo(f"📊 Positions: {results.get('positions_count', 0)}")
                    click.echo(f"  ✅ New opened: {results.get('new_opened', 0)}")
                    click.echo(f"  🔴 Closed: {results.get('closed', 0)}")
                    click.echo(f"  🔄 Switched: {results.get('switched', 0)}")

                    if results.get('errors', 0) > 0:
                        click.echo(f"  ❌ Errors: {results['errors']}")

                    manually_closed = results.get('manually_closed', [])
                    if manually_closed:
                        click.echo(f"  ⚠️  Manual closes: {', '.join(manually_closed)}")

                # Exit if not polling
                if not poll:
                    click.echo("\n✅ Single check complete")
                    break

                # Wait for next iteration
                click.echo(f"\n⏳ Waiting {interval}s until next check...")
                time.sleep(interval)

        except KeyboardInterrupt:
            click.echo("\n\n🛑 Stopped by user")
            sys.exit(0)
        except Exception as e:
            click.echo(f"\n\n❌ Fatal error: {e}")
            logging.exception("Fatal error in follow loop")
            sys.exit(1)

    @cli.command()
    def status():
        """Show configuration and connection status."""
        from .binance import BinanceService

        click.echo("📋 Configuration Status\n")

        # Config validation
        valid, error = config.validate()
        if valid:
            click.echo("✅ Configuration: Valid")
        else:
            click.echo(f"❌ Configuration: {error}")
            return

        # Display key settings
        click.echo(f"\n🔧 Settings:")
        click.echo(f"  API Base: {config.nof1_api_base}")
        click.echo(f"  Testnet: {config.binance_testnet}")
        click.echo(f"  Capital Mode: {config.capital_mode}")
        click.echo(f"  Margin Mode: {config.margin_mode}")
        click.echo(f"  Leverage: {config.default_leverage}x")
        click.echo(f"  Price Tolerance: {config.default_price_tolerance_pct}%")
        click.echo(f"  Telegram: {'Enabled' if config.telegram_enabled else 'Disabled'}")

        # Test Binance connection
        click.echo(f"\n🔌 Testing Binance connection...")
        try:
            binance = BinanceService(
                config.binance_api_key,
                config.binance_api_secret,
                config.binance_testnet
            )

            balance = binance.get_balance()
            if balance:
                click.echo(f"✅ Connected to Binance")
                click.echo(f"  Total Balance: {balance['total_balance']:.2f} USDT")
                click.echo(f"  Available: {balance['available_balance']:.2f} USDT")

                # Show positions
                positions = binance.get_positions()
                if positions:
                    click.echo(f"\n📈 Open Positions ({len(positions)}):")
                    for pos in positions:
                        pnl = pos['unrealized_pnl']
                        pnl_emoji = "📈" if pnl >= 0 else "📉"
                        click.echo(
                            f"  {pnl_emoji} {pos['symbol']}: {pos['side']} "
                            f"{pos['quantity']:.4f} @ {pos['entry_price']:.2f} "
                            f"(PnL: {pnl:.2f})"
                        )
                else:
                    click.echo(f"\n📊 No open positions")
            else:
                click.echo("❌ Failed to connect to Binance")
        except Exception as e:
            click.echo(f"❌ Binance error: {e}")

    @cli.command()
    def profit():
        """Analyze profit/loss from order history."""
        from .history import OrderHistoryManager
        from .binance import BinanceService

        click.echo("📊 Profit/Loss Analysis\n")

        history = OrderHistoryManager()
        stats = history.get_statistics()

        click.echo(f"Order History:")
        click.echo(f"  Total Orders: {stats['total_orders']}")
        click.echo(f"  Unique Coins: {stats['unique_coins']}")
        click.echo(f"  Profit Exits: {stats['profit_exits']}")
        click.echo(f"  Manual Closes: {stats['manual_closes']}")

        # Try to get current positions for unrealized P&L
        try:
            binance = BinanceService(
                config.binance_api_key,
                config.binance_api_secret,
                config.binance_testnet
            )

            positions = binance.get_positions()
            if positions:
                total_unrealized = sum(p['unrealized_pnl'] for p in positions)
                click.echo(f"\n📈 Current Positions:")
                click.echo(f"  Unrealized P&L: {total_unrealized:.2f} USDT")

                for pos in positions:
                    pnl = pos['unrealized_pnl']
                    pnl_pct = (pnl / (pos['entry_price'] * pos['quantity'])) * 100
                    emoji = "📈" if pnl >= 0 else "📉"
                    click.echo(
                        f"  {emoji} {pos['symbol']}: {pnl:+.2f} USDT ({pnl_pct:+.2f}%)"
                    )
        except Exception as e:
            click.echo(f"\n⚠️  Could not fetch current positions: {e}")

    @cli.command()
    def telegram_test():
        """Test Telegram notifications."""
        from .telegram import TelegramService

        if not config.telegram_enabled:
            click.echo("❌ Telegram is not enabled in configuration")
            return

        click.echo("📱 Testing Telegram...")

        telegram = TelegramService(config.telegram_token, config.telegram_chat_id)
        success = telegram.test()

        if success:
            click.echo("✅ Telegram test message sent successfully!")
        else:
            click.echo("❌ Failed to send Telegram message")

    # Run CLI
    cli()


if __name__ == "__main__":
    main()
