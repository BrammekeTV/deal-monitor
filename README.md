# 🃏 Deal Monitor – Pokémon card deals on Vinted → Discord

A production-ready Discord bot that continuously monitors [Vinted](https://www.vinted.com) for
Pokémon card deals and automatically posts them into a Discord channel with rich embeds, deal
scoring, and duplicate prevention.

---

## ✨ Features

| Feature | Details |
|---|---|
| 🔍 Multi-term search | Configurable list of Pokémon-related search terms |
| 🌍 Country filtering | Scrape specific Vinted country domains (NL, DE, FR, GB, …) |
| 📊 Deal scoring | Automated 0-100 score based on price, keywords, seller rating, bundles |
| 🚫 Duplicate prevention | SQLite-backed seen-listing store |
| 🎨 Rich Discord embeds | Title, price, EMV, discount %, score bar, seller, thumbnail |
| 🔕 Blacklist | Keyword blacklist to filter fakes / proxies |
| ⚙️ Slash commands | Manage filters and search terms at runtime via `/set_filter`, `/add_term`, etc. |
| 🔄 Auto-reconnect | discord.py handles reconnection transparently |
| 📋 Logging | Rotating log files + console output |
| 🐳 Docker support | One-command start with `docker-compose up -d` |

---

## 🗂 Project structure

```
deal-monitor/
├── main.py                  # Entry point
├── requirements.txt
├── .env.example
├── Dockerfile
├── docker-compose.yml
│
├── config/
│   ├── config.yaml          # All tuneable settings (no secrets here!)
│   └── settings.py          # Loads config.yaml + env vars
│
├── database/
│   ├── schema.sql           # SQLite schema
│   └── db.py                # Async database helper (aiosqlite)
│
├── scraper/
│   ├── base.py              # Abstract BaseScraper + Listing dataclass
│   └── vinted.py            # Playwright-based Vinted scraper
│
├── utils/
│   ├── logger.py            # Centralised logging setup
│   ├── deal_scorer.py       # Configurable deal scoring logic
│   └── embed_builder.py     # discord.py Embed builders
│
└── bot/
    ├── client.py            # Bot factory (intents, events)
    └── cogs/
        ├── monitor.py       # Background scrape + post loop
        └── filters.py       # Slash commands for runtime config
```

---

## 🚀 Quick start

### 1. Clone & install

```bash
git clone https://github.com/BrammekeTV/deal-monitor.git
cd deal-monitor
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env with your tokens
```

Edit `config/config.yaml` to adjust search terms, price limits, and scoring.

### 3. Create a Discord bot

1. Go to <https://discord.com/developers/applications> → **New Application**.
2. Under **Bot**, create a bot and copy the token → paste into `.env` as `DISCORD_BOT_TOKEN`.
3. Under **OAuth2 → URL Generator**, select scopes `bot` + `applications.commands`.
4. Enable **Send Messages** and **Embed Links** permissions.
5. Invite the bot to your server and note the target channel ID.

### 4. Run

```bash
python main.py
```

---

## 🐳 Docker

```bash
cp .env.example .env   # fill in your tokens
docker-compose up -d
docker-compose logs -f
```

---

## ⚙️ Configuration reference (`config/config.yaml`)

| Key | Default | Description |
|---|---|---|
| `scraper.interval_min` | 60 | Minimum scrape interval (seconds) |
| `scraper.interval_max` | 300 | Maximum scrape interval (seconds) |
| `scraper.results_per_term` | 30 | Max listings to fetch per search term |
| `scraper.headless` | `true` | Run browser headlessly |
| `scraper.countries` | `[]` | Country codes to restrict search (empty = global) |
| `deal.max_price` | 500 | Maximum listing price (EUR) |
| `deal.min_score` | 30 | Minimum deal score to trigger a Discord post |
| `deal.blacklist_keywords` | `[fake, proxy, …]` | Keywords that disqualify a listing |
| `deal.min_seller_rating` | 3.0 | Minimum seller star rating (0 to disable) |
| `market_values` | see config | Title substring → estimated EUR market value |

---

## 🤖 Slash commands

| Command | Description |
|---|---|
| `/status` | Show scrape stats and next-run time |
| `/set_filter key value` | Override a filter at runtime |
| `/get_filters` | List all active overrides |
| `/del_filter key` | Remove an override |
| `/add_term term` | Add a search term for the current session |
| `/remove_term term` | Remove a search term |
| `/reload_config` | Reload `config/config.yaml` from disk |

---

## 📐 Deal score breakdown

| Component | Points |
|---|---|
| Listing passes all hard filters | +10 |
| Discount vs estimated market value | 0–30 |
| Positive keyword matches | 0–20 |
| Bundle detected | +10 |
| Seller rating | 0–10 |
| **Max total** | **100** |

---

## 🔒 Security

- Secrets live only in `.env` (never in `config.yaml` or source code).
- `.env` is listed in `.gitignore`.
- The bot uses only the permissions it needs.

---

## 🛠 Extending to other marketplaces

1. Create `scraper/marketplace_name.py`.
2. Subclass `BaseScraper` from `scraper/base.py`.
3. Implement `setup()`, `teardown()`, `search()`, and `get_listing()`.
4. Instantiate your scraper in `bot/cogs/monitor.py` alongside `VintedScraper`.

---

## 📜 License

MIT
