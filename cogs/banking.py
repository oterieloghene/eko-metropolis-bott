"""
Phase 2 — Core Banking
======================

Builds on Phase 1's locations/sub_locations tables. The bank's
"rooms" (front-desk, atm, bank-manager, deposit, auditor,
cbe-deputy, cbe-chairman) are expected to already exist as
sub-locations attached to the "bank" parent location — created by
an admin via !create-sub-location, using exactly these codes and
matching Discord role names (see location_admin.py). This cog does
not create those rooms; it only enforces that the right command is
typed from the right one.

Two separate Naira pools per player, per config's split:

    players.balance      -> BANK balance (existing column, gated
                             behind database.has_bank_account —
                             see !create-account below).
    players.cash_balance  -> CASH in hand (new column, no account
                             needed — usable anywhere).

Two fixed institution ledgers (database.institution_accounts):

    central_bank -> Central Bank of Eko. Permanent sink for
                    government/office deposits (Phase 3), and the
                    source !cb-with draws from. Never disappears.
    treasury      -> Treasury. Government's own spending pool.

Commands:

    !create-account [@player]
        Opens a bank account. Front-desk sub-location only. If no
        member is given, opens one for the command's author (self-
        service); a teller can also run this on behalf of whoever
        they're helping.

    !with <amount>
        Bank -> cash. ATM sub-location only.

    !transfer <@player> <amount> [narration...]
        Bank -> bank. Works anywhere (matches the existing phone
        Bank App's Transfer feature, which calls the same
        database.bank_transfer()). Org/business recipients aren't
        wired up yet — that lands with !create-current-account
        (Phase 3) and !create-business-account (Phase 4).

    !cash-bal
        Shows cash balance only. Usable anywhere.

    !pay <@receiver> <amount> [narration...]
        Cash payment. Receiver gets Accept/Decline buttons. Only
        usable when both players are actually at the same location,
        typed from that location's own channel. Cash register
        settlement (Phase 5's !buy) isn't wired up yet — every !pay
        right now is a plain cash transfer.

    !view-balances
        Staff-only (bank-manager / cbe-deputy / cbe-chairman). DMs
        the caller every player's bank + cash balance, plus the
        Central Bank / Treasury balances. Org/business balances
        will be added here in Phase 3/4.

    !cb-with <@player> <amount> [narration...]
        Central Bank of Eko -> a player's bank balance.
        cbe-chairman role AND cbe-chairman sub-location required.

    !adjust <@player|central_bank|treasury> <amount> [narration...]
        Manual correction tool. Positive amount credits, negative
        debits. Gated to bank-manager, cbe-deputy, or cbe-chairman
        (any one of the three). Business/org accounts will become
        valid targets once Phase 3/4 exist.
"""

import discord
from discord.ext import commands

import database
import permissions

from cogs import contacts as contacts_cog


# ================================================================
# SUB-LOCATION / ROLE CONSTANTS
# ================================================================

FRONT_DESK_CODE = "front-desk"
ATM_CODE = "atm"
CBE_CHAIRMAN_CODE = "cbe-chairman"

BANK_MANAGER_ROLE = "bank-manager"
CBE_DEPUTY_ROLE = "cbe-deputy"
CBE_CHAIRMAN_ROLE = "cbe-chairman"

# !adjust and !view-balances are gated to any ONE of these three —
# not all required. !cb-with is separately gated to CBE_CHAIRMAN_ROLE
# alone, since it's specifically Central-Bank-to-player and
# chairman-only (see database.cb_withdraw_to_player's docstring).
BANK_STAFF_ROLES = (
    BANK_MANAGER_ROLE,
    CBE_DEPUTY_ROLE,
    CBE_CHAIRMAN_ROLE,
)

PAY_RESPONSE_TIMEOUT_SECONDS = 120

# How many players per DM chunk in !view-balances, kept comfortably
# under Discord's 2000-char plain-message limit.
VIEW_BALANCES_CHUNK_SIZE = 20


# ================================================================
# ROLE HELPERS (same pattern as cogs/police.py, cogs/ambulance.py)
# ================================================================

def _has_role(member: discord.Member, role_name: str) -> bool:
    return discord.utils.get(member.roles, name=role_name) is not None


def _has_any_role(member: discord.Member, role_names) -> bool:
    return any(_has_role(member, name) for name in role_names)


# ================================================================
# FAILURE-REASON -> MESSAGE HELPERS
# ================================================================

_BANK_TRANSFER_REASONS = {
    "no_sender_account": "⛔ You don't have a bank account. Open one with `!create-account` at the front desk.",
    "no_recipient_account": "⛔ {target} doesn't have a bank account yet.",
    "invalid_amount": "⛔ Enter a positive amount.",
    "insufficient_funds": "⛔ You don't have enough in your bank balance.",
}

_WITHDRAW_REASONS = {
    "no_account": "⛔ You don't have a bank account. Open one with `!create-account` at the front desk.",
    "invalid_amount": "⛔ Enter a positive amount.",
    "insufficient_funds": "⛔ You don't have enough in your bank balance.",
}

_CASH_TRANSFER_REASONS = {
    "invalid_amount": "⛔ Enter a positive amount.",
    "insufficient_funds": "⛔ You don't have enough cash on hand.",
}

_CB_WITHDRAW_REASONS = {
    "invalid_amount": "⛔ Enter a positive amount.",
    "insufficient_funds": "⛔ The Central Bank of Eko doesn't have enough funds for that. Something has gone very wrong.",
}

_ADJUST_REASONS = {
    "insufficient_funds": "⛔ That account doesn't have enough balance to cover a debit of that size.",
    "no_such_account": "⛔ No institution account with that code exists.",
}


class BankingCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ============================================================
    # SUB-LOCATION GATE (same shape as checks.require_location,
    # generalized to work against database.sub_locations instead
    # of the static config.LOCATIONS dict)
    # ============================================================

    async def _require_sub_location(
        self,
        ctx: commands.Context,
        sub_code: str
    ):
        """
        Returns the sub-location row if:
            - it exists,
            - the command was typed in its channel, AND
            - the author's actual database location is its parent.

        Otherwise sends an explanatory error and returns None.
        """

        sub = database.get_sub_location(sub_code)

        if sub is None:
            await ctx.send(
                f"⛔ The `{sub_code}` room hasn't been set up yet. "
                f"An admin needs to run `!create-sub-location bank "
                f"{sub_code} ...` first."
            )
            return None

        if ctx.channel.name != sub["channel_name"]:
            await ctx.send(
                f"⛔ This only works in #{sub['channel_name']}."
            )
            return None

        player = database.get_or_create_player(ctx.author.id)

        if player["traveling"]:
            await ctx.send(
                "⛔ You are currently travelling and cannot do this."
            )
            return None

        if player["location"] != sub["parent_code"]:
            await ctx.send(
                f"⛔ You need to actually be at **{sub['name']}** "
                f"to do this."
            )
            return None

        return sub

    # ============================================================
    # !CREATE-ACCOUNT
    # ============================================================

    @commands.command(name="create-account")
    async def create_account(
        self,
        ctx: commands.Context,
        member: discord.Member = None
    ):

        sub = await self._require_sub_location(ctx, FRONT_DESK_CODE)

        if sub is None:
            return

        target = member or ctx.author

        if target.bot:
            await ctx.send("⛔ Bots can't have bank accounts.")
            return

        database.get_or_create_player(target.id)

        created = database.create_bank_account(target.id)

        if not created:
            await ctx.send(
                f"⛔ {target.mention} already has a bank account."
            )
            return

        embed = discord.Embed(
            title="🏦 Bank Account Opened",
            color=discord.Color.green()
        )
        embed.add_field(name="Account Holder", value=target.mention)

        await ctx.send(embed=embed)

    # ============================================================
    # !WITH — bank -> cash, ATM only
    # ============================================================

    @commands.command(name="with")
    async def withdraw(
        self,
        ctx: commands.Context,
        amount: int
    ):

        sub = await self._require_sub_location(ctx, ATM_CODE)

        if sub is None:
            return

        ok, reason = database.withdraw_to_cash(ctx.author.id, amount)

        if not ok:
            await ctx.send(_WITHDRAW_REASONS.get(reason, f"⛔ Withdrawal failed ({reason})."))
            return

        player = database.get_player(ctx.author.id)

        embed = discord.Embed(
            title="🏧 Cash Withdrawn",
            color=discord.Color.green()
        )
        embed.add_field(name="Withdrawn", value=f"₦{amount:,}", inline=True)
        embed.add_field(name="Bank Balance", value=f"₦{player['balance']:,}", inline=True)
        embed.add_field(name="Cash Balance", value=f"₦{player['cash_balance']:,}", inline=True)

        await ctx.send(embed=embed)

    # ============================================================
    # !TRANSFER — bank -> bank
    # ============================================================

    @commands.command(name="transfer")
    async def transfer(
        self,
        ctx: commands.Context,
        recipient: str,
        amount: int,
        *,
        narration: str = ""
    ):

        try:
            member = await commands.MemberConverter().convert(ctx, recipient)

        except commands.BadArgument:
            await ctx.send(
                "⛔ Org/business transfer recipients aren't available "
                "yet — for now `!transfer` only works to another "
                "player (mention them)."
            )
            return

        if member.bot:
            await ctx.send("⛔ You can't transfer to a bot.")
            return

        if member.id == ctx.author.id:
            await ctx.send("⛔ You can't transfer to yourself.")
            return

        ok, reason = database.bank_transfer(ctx.author.id, member.id, amount)

        if not ok:
            message = _BANK_TRANSFER_REASONS.get(
                reason,
                f"⛔ Transfer failed ({reason})."
            ).format(target=member.mention)
            await ctx.send(message)
            return

        player = database.get_player(ctx.author.id)

        embed = discord.Embed(
            title="🏦 Bank Transfer Sent",
            color=discord.Color.green()
        )
        embed.add_field(name="To", value=member.mention, inline=True)
        embed.add_field(name="Amount", value=f"₦{amount:,}", inline=True)
        embed.add_field(name="Your New Bank Balance", value=f"₦{player['balance']:,}", inline=False)

        if narration:
            embed.add_field(name="Narration", value=narration, inline=False)

        await ctx.send(embed=embed)

        await contacts_cog.send_transaction_alert(
            self.bot,
            ctx.guild,
            ctx.author.id,
            member.id,
            f"🏦 You received ₦{amount:,} from {ctx.author.display_name}."
            + (f"\n📝 {narration}" if narration else ""),
        )

    # ============================================================
    # !CASH-BAL
    # ============================================================

    @commands.command(name="cash-bal")
    async def cash_bal(self, ctx: commands.Context):

        player = database.get_or_create_player(ctx.author.id)

        embed = discord.Embed(title="💵 Cash Balance", color=discord.Color.gold())
        embed.add_field(name="Amount", value=f"₦{player['cash_balance']:,}")

        await ctx.send(embed=embed)

    # ============================================================
    # !PAY — cash payment, accept/decline
    # ============================================================

    @commands.command(name="pay")
    async def pay(
        self,
        ctx: commands.Context,
        receiver: discord.Member,
        amount: int,
        *,
        narration: str = ""
    ):

        if receiver.bot:
            await ctx.send("⛔ You can't pay a bot.")
            return

        if receiver.id == ctx.author.id:
            await ctx.send("⛔ You can't pay yourself.")
            return

        if amount <= 0:
            await ctx.send("⛔ Enter a positive amount.")
            return

        payer = database.get_or_create_player(ctx.author.id)
        recv = database.get_or_create_player(receiver.id)

        if payer["location"] != recv["location"]:
            await ctx.send(
                f"⛔ {receiver.mention} isn't at the same location as "
                f"you right now."
            )
            return

        expected_channel = permissions.get_channel_for_code(
            ctx.guild,
            payer["location"]
        )

        if expected_channel is None or ctx.channel.id != expected_channel.id:
            await ctx.send(
                "⛔ You need to type this from the channel matching "
                "where you actually are."
            )
            return

        if payer["cash_balance"] < amount:
            await ctx.send("⛔ You don't have enough cash on hand.")
            return

        embed = discord.Embed(
            title="💵 Cash Payment Requested",
            color=discord.Color.gold()
        )
        embed.add_field(name="From", value=ctx.author.mention, inline=True)
        embed.add_field(name="To", value=receiver.mention, inline=True)
        embed.add_field(name="Amount", value=f"₦{amount:,}", inline=True)

        if narration:
            embed.add_field(name="Narration", value=narration, inline=False)

        view = _PayView(
            payer_id=ctx.author.id,
            receiver_id=receiver.id,
            amount=amount,
        )

        message = await ctx.send(
            f"{receiver.mention}, {ctx.author.mention} wants to pay you:",
            embed=embed,
            view=view
        )

        view.message = message

    # ============================================================
    # !VIEW-BALANCES — staff-only, DM
    # ============================================================

    @commands.command(name="view-balances")
    async def view_balances(self, ctx: commands.Context):

        if not isinstance(ctx.author, discord.Member):
            return

        if not _has_any_role(ctx.author, BANK_STAFF_ROLES):
            await ctx.send(
                f"⛔ Only **{', '.join(BANK_STAFF_ROLES)}** can view "
                f"all balances."
            )
            return

        players = database.all_players()
        institutions = database.all_institution_accounts()

        lines = []

        for row in players:

            member = ctx.guild.get_member(int(row["user_id"]))
            label = member.display_name if member else f"User {row['user_id']}"

            lines.append(
                f"**{label}** — Bank: ₦{row['balance']:,} | "
                f"Cash: ₦{row['cash_balance']:,}"
            )

        header = "**🏦 Institution Accounts**\n" + "\n".join(
            f"**{row['name']}** — ₦{row['balance']:,}"
            for row in institutions
        )

        header += "\n\n**👤 Player Balances**"
        header += "\n\n⚠️ Org/business balances aren't available yet (Phase 3/4)."

        chunks = [
            lines[i:i + VIEW_BALANCES_CHUNK_SIZE]
            for i in range(0, len(lines), VIEW_BALANCES_CHUNK_SIZE)
        ] or [[]]

        try:

            await ctx.author.send(header)

            for chunk in chunks:

                if chunk:
                    await ctx.author.send("\n".join(chunk))

        except discord.Forbidden:
            await ctx.send(
                "⛔ I couldn't DM you — check that your DMs are open "
                "to server members."
            )
            return

        await ctx.send("📬 Sent you a DM with every balance.")

    # ============================================================
    # !CB-WITH — Central Bank of Eko -> player, chairman-only
    # ============================================================

    @commands.command(name="cb-with")
    async def cb_with(
        self,
        ctx: commands.Context,
        member: discord.Member,
        amount: int,
        *,
        narration: str = ""
    ):

        if not isinstance(ctx.author, discord.Member):
            return

        if not _has_role(ctx.author, CBE_CHAIRMAN_ROLE):
            await ctx.send(
                f"⛔ Only the **{CBE_CHAIRMAN_ROLE}** can do this."
            )
            return

        sub = await self._require_sub_location(ctx, CBE_CHAIRMAN_CODE)

        if sub is None:
            return

        if member.bot:
            await ctx.send("⛔ You can't credit a bot.")
            return

        ok, reason = database.cb_withdraw_to_player(member.id, amount)

        if not ok:
            await ctx.send(_CB_WITHDRAW_REASONS.get(reason, f"⛔ Failed ({reason})."))
            return

        recipient_player = database.get_player(member.id)

        embed = discord.Embed(
            title="🏛️ Central Bank of Eko Disbursement",
            color=discord.Color.green()
        )
        embed.add_field(name="To", value=member.mention, inline=True)
        embed.add_field(name="Amount", value=f"₦{amount:,}", inline=True)
        embed.add_field(
            name="Recipient's New Bank Balance",
            value=f"₦{recipient_player['balance']:,}",
            inline=False
        )

        if narration:
            embed.add_field(name="Narration", value=narration, inline=False)

        await ctx.send(embed=embed)

        await contacts_cog.send_transaction_alert(
            self.bot,
            ctx.guild,
            ctx.author.id,
            member.id,
            f"🏛️ The Central Bank of Eko credited your bank balance "
            f"with ₦{amount:,}."
            + (f"\n📝 {narration}" if narration else ""),
        )

    # ============================================================
    # !ADJUST — manual correction tool
    # ============================================================

    @commands.command(name="adjust")
    async def adjust(
        self,
        ctx: commands.Context,
        target: str,
        amount: int,
        *,
        narration: str = ""
    ):

        if not isinstance(ctx.author, discord.Member):
            return

        if not _has_any_role(ctx.author, BANK_STAFF_ROLES):
            await ctx.send(
                f"⛔ Only **{', '.join(BANK_STAFF_ROLES)}** can adjust "
                f"balances."
            )
            return

        member = None
        institution_code = None

        try:
            member = await commands.MemberConverter().convert(ctx, target)

        except commands.BadArgument:

            lowered = target.strip().lower().replace("-", "_")

            if lowered in ("central_bank", "cbe", "centralbank"):
                institution_code = "central_bank"

            elif lowered == "treasury":
                institution_code = "treasury"

            else:
                await ctx.send(
                    "⛔ That's not a player mention or a recognized "
                    "institution account (`central_bank` / `treasury`). "
                    "Business/org account adjustment isn't available "
                    "yet (Phase 3/4)."
                )
                return

        if member is not None:
            ok, reason = database.adjust_player_balance(member.id, amount)
            new_balance = database.get_player(member.id)["balance"] if ok else None
            account_label = member.mention

        else:
            ok, reason = database.adjust_institution_balance(institution_code, amount)
            new_balance = database.get_institution_account(institution_code)["balance"] if ok else None
            account_label = database.get_institution_account(institution_code)["name"]

        if not ok:
            await ctx.send(_ADJUST_REASONS.get(reason, f"⛔ Adjustment failed ({reason})."))
            return

        embed = discord.Embed(
            title="🛠️ Balance Adjusted",
            color=discord.Color.orange()
        )
        embed.add_field(name="Account", value=account_label, inline=True)
        embed.add_field(
            name="Adjustment",
            value=f"{'+' if amount >= 0 else ''}₦{amount:,}",
            inline=True
        )
        embed.add_field(name="New Balance", value=f"₦{new_balance:,}", inline=True)
        embed.add_field(name="Adjusted By", value=ctx.author.mention, inline=False)

        if narration:
            embed.add_field(name="Narration", value=narration, inline=False)

        await ctx.send(embed=embed)


# ================================================================
# !PAY ACCEPT/DECLINE UI
# ================================================================

class _PayView(discord.ui.View):

    def __init__(self, payer_id: int, receiver_id: int, amount: int):
        super().__init__(timeout=PAY_RESPONSE_TIMEOUT_SECONDS)
        self.payer_id = payer_id
        self.receiver_id = receiver_id
        self.amount = amount
        self.message: discord.Message = None
        self.resolved = False

    async def _disable(self):
        for child in self.children:
            child.disabled = True

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, emoji="✅")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):

        if interaction.user.id != self.receiver_id:
            await interaction.response.send_message(
                "⛔ This payment request isn't yours to accept.",
                ephemeral=True
            )
            return

        self.resolved = True
        await self._disable()

        ok, reason = database.cash_transfer(self.payer_id, self.receiver_id, self.amount)

        if not ok:
            await interaction.response.edit_message(
                content=_CASH_TRANSFER_REASONS.get(
                    reason,
                    f"⛔ Payment failed ({reason})."
                ),
                embed=None,
                view=self
            )
            self.stop()
            return

        await interaction.response.edit_message(
            content=f"✅ Payment of ₦{self.amount:,} accepted.",
            view=self
        )

        self.stop()

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger, emoji="❌")
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):

        if interaction.user.id != self.receiver_id:
            await interaction.response.send_message(
                "⛔ This payment request isn't yours to decline.",
                ephemeral=True
            )
            return

        self.resolved = True
        await self._disable()

        await interaction.response.edit_message(
            content="❌ Payment declined.",
            view=self
        )

        self.stop()

    async def on_timeout(self):

        if self.resolved or self.message is None:
            return

        await self._disable()

        try:
            await self.message.edit(
                content="⌛ Payment request expired.",
                view=self
            )

        except discord.HTTPException:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(BankingCog(bot))
