"""
bot/cogs/maintenance.py
~~~~~~~~~~~~~~~~~~~~~~~
Discord slash commands for maintenance and pricing validation.

Commands:
  /update       – Pull latest code from git, update dependencies, and report
                  current version, new version, changed files, and status.
  /check-price  – Manually validate the pricing system for a given search query.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from utils.logger import get_logger

if TYPE_CHECKING:
    from bot.cogs.monitor import MonitorCog

logger = get_logger(__name__)

# Absolute path to the repository root (two levels above this file).
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------


async def _run(*args: str) -> tuple[int, str]:
    """Run a subprocess command from the repo root; return (returncode, combined output)."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=_REPO_ROOT,
    )
    stdout, stderr = await proc.communicate()
    output = (stdout + stderr).decode(errors="replace").strip()
    return proc.returncode, output


async def _git_version() -> str:
    """Return a human-readable version: nearest annotated tag or short commit hash."""
    rc, out = await _run("git", "describe", "--tags", "--always", "--dirty")
    return out.strip() if rc == 0 and out.strip() else "unknown"


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------


class MaintenanceCog(commands.Cog, name="Maintenance"):
    """Slash commands for self-update and pricing validation."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    def _get_monitor(self) -> "MonitorCog | None":
        return self.bot.cogs.get("Monitor")  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # /update
    # ------------------------------------------------------------------

    @app_commands.command(
        name="update",
        description="Update the bot from its configured git repository",
    )
    async def update(self, interaction: discord.Interaction) -> None:
        """Pull the latest changes, update dependencies, and report the result."""
        await interaction.response.defer(ephemeral=True, thinking=True)

        current_version = await _git_version()

        # Capture the current HEAD hash before pulling (reflog may not exist
        # in shallow clones, so we store the hash explicitly).
        rc_head, pre_pull_hash = await _run("git", "rev-parse", "HEAD")
        pre_pull_hash = pre_pull_hash.strip() if rc_head == 0 else ""

        # 1. Fetch latest refs and tags.
        rc, fetch_out = await _run("git", "fetch", "--tags")
        if rc != 0:
            await interaction.followup.send(
                f"❌ `git fetch` failed:\n```\n{fetch_out[:1800]}\n```",
                ephemeral=True,
            )
            return

        # 2. Pull (rebase to avoid a merge commit on a linear history).
        rc, pull_out = await _run("git", "pull", "--rebase")
        if rc != 0:
            await interaction.followup.send(
                f"❌ `git pull` failed:\n```\n{pull_out[:1800]}\n```",
                ephemeral=True,
            )
            return

        # 3. Collect the list of changed files using the saved pre-pull hash.
        changed_files: list[str] = []
        if pre_pull_hash:
            rc_diff, diff_out = await _run(
                "git", "diff", "--name-only", pre_pull_hash, "HEAD"
            )
            if rc_diff == 0 and diff_out.strip():
                changed_files = [f for f in diff_out.splitlines() if f.strip()]

        # 4. Re-install dependencies when requirements.txt was touched.
        #    Always run pip when we can't determine what changed.
        deps_updated = False
        deps_error = ""
        should_update_deps = not changed_files or "requirements.txt" in changed_files
        if should_update_deps:
            rc_pip, pip_out = await _run(
                sys.executable,
                "-m",
                "pip",
                "install",
                "-r",
                "requirements.txt",
                "--quiet",
            )
            if rc_pip == 0:
                deps_updated = True
            else:
                deps_error = pip_out

        new_version = await _git_version()

        # Build response embed.
        updated = new_version != current_version
        embed = discord.Embed(
            title="🔄 Bot Update",
            colour=0x00FF7F if updated else 0x5865F2,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(
            name="📌 Current Version", value=f"`{current_version}`", inline=True
        )
        embed.add_field(
            name="🆕 New Version", value=f"`{new_version}`", inline=True
        )
        embed.add_field(
            name="✅ Status",
            value="Updated successfully" if updated else "Already up to date",
            inline=True,
        )

        if changed_files:
            files_text = "\n".join(f"• `{f}`" for f in changed_files[:20])
            if len(changed_files) > 20:
                files_text += f"\n… and {len(changed_files) - 20} more"
            embed.add_field(
                name=f"📂 Updated Files ({len(changed_files)})",
                value=files_text,
                inline=False,
            )

        if deps_updated:
            embed.add_field(
                name="📦 Dependencies", value="Updated successfully", inline=True
            )
        elif deps_error:
            embed.add_field(
                name="📦 Dependencies",
                value=f"⚠️ Update failed:\n```{deps_error[:400]}```",
                inline=False,
            )

        logger.info(
            "/update by %s: %s → %s (%d file(s) changed)",
            interaction.user,
            current_version,
            new_version,
            len(changed_files),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    # /check-price
    # ------------------------------------------------------------------

    @app_commands.command(
        name="check-price",
        description="Manually validate the pricing system for a search query",
    )
    @app_commands.describe(query="Search term to price-check (e.g. 'Charizard VMAX PSA 10')")
    async def check_price(self, interaction: discord.Interaction, query: str) -> None:
        """Fetch live market prices for *query* and display the results."""
        await interaction.response.defer(ephemeral=True, thinking=True)

        monitor = self._get_monitor()
        if monitor is None or monitor._http is None:
            await interaction.followup.send(
                "❌ The monitor cog is not ready yet. Please try again in a moment.",
                ephemeral=True,
            )
            return

        from utils.price_lookup import lookup_prices

        results = await lookup_prices(
            monitor._http,
            query,
            browser=monitor._browser,
        )

        if not results:
            await interaction.followup.send(
                f"⚠️ No price data found for **{query}**.\n"
                "Verify that the eBay App ID is configured (`EBAY_APP_ID`) "
                "or that Cardmarket scraping is enabled in `config.yaml`.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title=f"💰 Price Check: {query}",
            colour=0x00FF7F,
            timestamp=datetime.now(timezone.utc),
        )
        for result in results:
            embed.add_field(
                name=f"🔎 {result.platform} ({result.sample_count} sample(s))",
                value=(
                    f"[Search results]({result.search_url})\n"
                    f"**Avg:** {result.avg_price:.2f} {result.currency}  "
                    f"| **Low:** {result.min_price:.2f}  "
                    f"| **High:** {result.max_price:.2f}"
                ),
                inline=False,
            )

        logger.info(
            "/check-price '%s' by %s → %d platform(s)",
            query,
            interaction.user,
            len(results),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
