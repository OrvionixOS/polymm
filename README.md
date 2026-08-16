# polymm

*A Polymarket sports market-making and arbitrage bot in Python - de-vig sportsbook odds, quote just above the best bid, hedge to lock the arb.*

**The retired Python bot behind a public Polymarket wallet.** It market-made on Polymarket's sports markets (esports and traditional) for a few months in early 2026 and finished up about **$5k** net, every trade on-chain at [@b00k13](https://polymarket.com/@b00k13). This repo is the actual code, plus the analytics I built to work out why the "guaranteed" strategy still bled money on the parts that were meant to be safe.

Most "I built a Polymarket bot" repos are a README and a dream. This one traded real money and leaves a public wallet you can reconcile it against. At some point it also mostly stopped being profitable, and the write-ups at the bottom are honest about why.

It was built almost entirely with AI, in my spare time. That's not an excuse for the quality. You may find bugs. There are certainly things to improve. But it's still the exact bot that I used and it could be a good basis for you if you want to get into Polymarket market making.

## Read, learn, improve - not plug and play

I'm giving this away because the code was never the hard part. The edge was **fresh odds and speed**, and here both are on you:

- This is the **retired Python version**. The bot I run now is a Rust rewrite and it's not in this repo.
- **The scrapers aren't included.** The bot reads odds from a source you supply. It ships with a [the-odds-api](https://the-odds-api.com) path - a public, paid API - and the private scraping pipeline that was half the edge is gone.
- It got **too slow** to defend its edge, which is exactly why it stopped making money.

So treat it as a working foundation, not a money printer. The strategy is sound and the plumbing works. Whether it earns is down to what you bring: faster and fresher odds, sharper execution, tighter risk. Put the work in and it can pay. Run it as-is and you'll donate to quicker bots.

## How it works

Cross-venue arbitrage, captured by market-making. In plain terms:

1. **Get a fair price.** Pull bookmaker odds (via the-odds-api) and strip the bookmaker's margin to a fair probability for each side. That fair value is the whole game, and it's only ever as good as your odds are fresh.
2. **Quote it.** Post limit orders on the matching Polymarket market, a cent above the best bid, only where there's enough edge between your fair value and the price (`min_edge`, 5% by default).
3. **Hedge to lock the arb.** When one side fills, place the opposite outcome so you own both sides of the match for under $1 a share. That's a profit locked in whoever wins.
4. **The residual.** You can't always get both legs. When one fills and the hedge doesn't, you're left holding a directional bet. Each of those went on at a 7%+ edge, so a book of them *should* be profitable. Working out why mine wasn't is the second write-up below.

## Architecture

The bot (`src/main.py`) is a single async orchestrator running roughly ten concurrent loops. The pieces:

| Layer | What it does |
|---|---|
| `services/` | Fetch and aggregate odds (`odds_service`, `odds_api_service`) |
| `core/` | De-vig maths, market parsing, team matching (`vig_removal`, `market_parser`, `match_id`) |
| `scanning/` | Compare fair value to Polymarket prices, find edges (`opportunity_scanner`) |
| `execution/` | Place orders, size hedges, track positions (`order_executor`, `hedge_finder`, `order_watcher`) |
| `monitoring/` | Watch for outbids and edge loss, chase hedges (`hedge_seeker`, `order_monitor`) |
| `polymarket/` | The two live websockets - fills (`user_websocket`) and order book (`book_websocket`) |
| `state/` | Match-level state and the reactive event system (`bot_state`, `reactive_handler`) |

The reactive system is the interesting bit - when the fair value moves against a resting order, it emits an `EDGE_LOST` event and cancels before the order gets picked off. In practice, not fast enough - which is the whole story of the autopsy below.

The data path:

```
odds source -> Supabase -> OddsService -> OpportunityScanner -> OrderExecutor -> Polymarket
                                                   ^                                  |
                                                   +-------- book / user websockets --+
```

`src/data/` writes odds, fills and positions to Supabase. The table schemas are in `sql/`.

## Running it

Heads up: this needs your own infrastructure and it will not trade out of the box without it.

1. `pip install -r requirements.txt`
2. Copy `.env.example` to `.env` and fill it in - a Polymarket wallet, a the-odds-api key, and a Supabase project.
3. Create the tables from `sql/` in your Supabase project.
4. `python -m src.main`

`default_shares` is 10, so it starts tiny by design. Watch it for a while before you give it size, and read the autopsy first so you know what you're up against.

## The analytics

I built a second project just to debug the first. These four scripts run against **any public Polymarket wallet** - no private data, just point them at a funder address:

| Script | What it tells you |
|---|---|
| `capital_analysis.py` | Overall portfolio P&L, split into the arb and the directional residual |
| `realized_pnl_by_month.py` | Realised P&L, month by month |
| `position_breakdown.py` | Open positions, normalised per market |
| `underdog_analysis.py` | Favourite vs underdog win-rate and ROI |

Run one with, e.g., `python -m analytics.capital_analysis`.

## Project layout

```
src/         the bot
analytics/   wallet-analysis scripts
sql/         Supabase table schemas
tests/       the test suite
```

## The write-ups

The full story, with the real numbers, is on the blog:

- [Everyone shows the wins. I'm showing the wallet.](https://kacho.io/why-kacho-io) - why I build in public.
- [I ran an arbitrage bot on Polymarket. Here are the real numbers.](https://kacho.io/polymarket-arbitrage-real-numbers) - the strategy and the P&L: the arb made **+$8,293**, the forced directional bets lost **-$3,184**, ~$5k net.
- [Adverse selection eating away my Polymarket bot arbitrage profits.](https://kacho.io/why-my-polymarket-arbitrage-bot-lost-money) - the autopsy: why a book of +EV bets lost money.
- [I recorded Polymarket's 5-minute crypto markets for two months. Here's the dataset.](https://kacho.io/polymarket-5min-crypto-dataset) - a free dataset from a related experiment.

## Listed on

- [Polymart](https://polymart.app) - a directory of Polymarket bots and tools.

## Licence

MIT - do what you like with it. If you build something interesting on top, I'd genuinely like to see it. The wallet's public if you want to pick any of the numbers apart.
