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
        Opens a bank account. Front-desk sub-location only, AND
        gated to the "Bank Staff" role — staff-only, not
        self-service. A teller runs this on behalf of whoever
        they're helping; if no member is given, it opens one for
        the command's author instead (only useful if the teller
        needs an account too).

    !create-current-account <code> <channel> <name...>
        Registers a Phase 3 government/office current account
        (e.g. "Eko Memorial Hospital"). Front-desk sub-location
        only, AND gated to bank-manager / cbe-deputy / cbe-chairman
        (any one of the three) — same staff tier as !adjust and
        !view-balances, since this is bank infrastructure setup,
        not a self-service player command.

    !with <amount>
        Bank -> cash. ATM sub-location only.

    !transfer <@player | org-code | business-code> <amount> [narration...]
        Bank -> bank, to another player (matches the existing
        phone Bank App's Transfer feature, which calls the same
        database.bank_transfer()) — OR bank -> a registered
        current account (Phase 3's !create-current-account), which
        settles straight into the Central Bank of Eko, logs to
        #cbe-log, and posts an open receipt to the account's
        linked channel — OR bank -> a registered business with an
        open account (Phase 4's !create-business-account), which
        settles straight into that business's OWN balance (never
        the Central Bank) and posts a lightweight receipt to the
        business's receipt channel — UNLESS the sender has an OPEN
        Phase 5 !buy tab with that business, in which case `amount`
        must exactly match the tab's total (mismatch rejected),
        the tab is marked paid, and an itemized receipt posts
        instead of the plain one.

    !create-business-account <code> [receipt-channel]
        Opens the financial account for an already-registered
        business (see cogs/business_admin.py's !business-registration,
        which only creates the location + ownership bookkeeping,
        not a balance). Front-desk sub-location only, AND gated to
        the business's own owner OR bank staff (bank-manager /
        cbe-deputy / cbe-chairman). receipt-channel defaults to the
        business's own registered channel if omitted.

    !cash-bal
        Shows cash balance only. Usable anywhere.

    !pay <@receiver> <amount> [narration...]
        Cash payment. Receiver gets Accept/Decline buttons. Only
        usable when both players are actually at the same location,
        typed from that location's own channel. Phase 5 register
        settlement: if the receiver owns a business with an OPEN
        !buy tab against the payer, `amount` must exactly match
        that tab's total (mismatch rejected) — on acceptance the
        tab is marked paid (ready for !sell) instead of this being
        a plain cash transfer. No matching tab -> plain transfer,
        same as before.

    !view-balances
        Staff-only (bank-manager / cbe-deputy / cbe-chairman). DMs
        the caller every player's bank + cash balance, plus the
        Central Bank / Treasury balances, plus every registered
        business's standalone account balance (Phase 4). Current
        accounts (Phase 3) never carry their own balance — see
        database.py — so there's nothing extra to list for them
        here; the Central Bank total already reflects every payment
        swept into one.

    !cb-with <@player> <amount> [narration...]
        Central Bank of Eko -> a player's bank balance.
        cbe-chairman role AND cbe-chairman sub-location required.

    !adjust <@player|central_bank|treasury|business-code> <amount> [narration...]
        Manual correction tool. Positive amount credits, negative
        debits. Gated to bank-manager, cbe-deputy, or cbe-chairman
        (any one of the three). Current accounts are never a valid
        !adjust target — they hold no balance to adjust (Phase 3).
        A business code is only valid once that business has an
        open account (Phase 4's !create-business-account).
"""

import json

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

# Phase 3 — plain audit-log channel for organization/government
# transfers (!create-current-account transfers). Not a
# sub-location — just a regular text channel an admin creates
# with this exact name. If it doesn't exist, the transfer still
# goes through; only the audit log entry is skipped.
CBE_LOG_CHANNEL_NAME = "cbe-log"

BANK_MANAGER_ROLE = "bank-manager"
CBE_DEPUTY_ROLE = "cbe-deputy"
CBE_CHAIRMAN_ROLE = "cbe-chairman"

# !create-account is gated to this single literal Discord role —
# distinct from the tiered bank-manager/cbe-deputy/cbe-chairman
# roles below. A "Bank Staff" member opens accounts for players;
# they don't necessarily hold any of the three tiered roles.
BANK_STAFF_ROLE = "Bank Staff"

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
    "no_sender_account": "⛔ You don't have a bank account. Visit the front desk and have a teller open one for you.",
    "no_recipient_account": "⛔ {target} doesn't have a bank account yet.",
    "invalid_amount": "⛔ Enter a positive amount.",
    "insufficient_funds": "⛔ You don't have enough in your bank balance.",
}

_WITHDRAW_REASONS = {
    "no_account": "⛔ You don't have a bank account. Visit the front desk and have a teller open one for you.",
    "invalid_amount": "⛔ Enter a positive amount.",
    "insufficient_funds": "⛔ You don't have enough in your bank balance.",
}

_CASH_TRANSFER_REASONS = {
    "invalid_amount": "⛔ Enter a positive amount.",
    "insufficient_funds": "⛔ You don't have enough cash on hand.",
}

_ORG_TRANSFER_REASONS = {
    "no_sender_account": "⛔ You don't have a bank account. Visit the front desk and have a teller open one for you.",
    "no_such_account": "⛔ No current account with that code exists.",
    "invalid_amount": "⛔ Enter a positive amount.",
    "insufficient_funds": "⛔ You don't have enough in your bank balance.",
}

_BUSINESS_TRANSFER_REASONS = {
    "no_sender_account": "⛔ You don't have a bank account. Visit the front desk and have a teller open one for you.",
    "no_such_account": "⛔ That business doesn't have an open account yet — the owner needs to run `!create-business-account` first.",
    "invalid_amount": "⛔ Enter a positive amount.",
    "insufficient_funds": "⛔ You don't have enough in your bank balance.",
}

_BUSINESS_ACCOUNT_REASONS = {
    "no_such_business": "⛔ No registered business with that code exists. It needs `!business-registration` first.",
    "already_open": "⛔ That business already has an open account.",
}

_CB_WITHDRAW_REASONS = {
    "invalid_amount": "⛔ Enter a positive amount.",
    "insufficient_funds": "⛔ The Central Bank of Eko doesn't have enough funds for that. Something has gone very wrong.",
}

_ADJUST_REASONS = {
    "insufficient_funds": "⛔ That account doesn't have enough balance to cover a debit of that size.",
    "no_such_account": "⛔ No institution account with that code exists.",
}


# ================================================================
# SHARED RECEIPT/AUDIT-TRAIL HELPERS
# ================================================================
#
# post_register_receipt, post_org_receipt, and post_business_receipt
# are all plain module-level functions (no `ctx`, no `self`) so
# that both !transfer (cogs/banking.py's text command) and the
# phone Bank App's Transfer flow (cogs/phone.py) can call the exact
# same posting logic and leave the same paper trail, regardless of
# which front door the money came through. Each takes the `guild`
# and the payer's display name directly instead of a Context, and
# each is best-effort: by the time any of these run, the underlying
# database transfer has already succeeded, so a missing/deleted
# channel here only means a missing receipt, never a failed
# payment. Each returns True/False for whether its main receipt
# posted, so the caller (ctx.send for the text command, an
# ephemeral followup for the phone modal) can warn the payer if it
# didn't.
#
# See cogs/business_shop.py's !buy for how a register gets opened
# in the first place, and database.settle_register()/database
# .get_open_register()/get_open_registers_for_owner_customer() for
# the lookups used by post_register_receipt below.
# ================================================================

async def post_register_receipt(
    guild: discord.Guild,
    payer_name: str,
    business,
    register,
    narration: str = ""
) -> bool:

    """
    Best-effort itemized receipt for a just-settled Phase 5
    register, posted to the business's receipt channel if it has
    an open account (!create-business-account), else falling back
    to the business's own registered location channel. Returns
    True if posted, False if no reachable channel was found — the
    payment/settlement itself has already succeeded either way.
    """

    account = database.get_business_account(business["code"])

    if account is not None:
        channel_name = account["receipt_channel_name"]
    else:
        location = database.get_location(business["code"])
        channel_name = location["channel_name"] if location else None

    channel = (
        discord.utils.get(guild.text_channels, name=channel_name)
        if channel_name else None
    )

    if channel is None:
        return False

    lines = json.loads(register["items"])

    embed = discord.Embed(
        title=f"🧾 Order Paid — {business['name']}",
        color=discord.Color.blurple()
    )
    embed.add_field(name="From", value=payer_name, inline=True)
    embed.add_field(name="Total", value=f"₦{register['total']:,}", inline=True)
    embed.add_field(
        name="Items",
        value="\n".join(
            f"• {line['qty']} x {line['item_name']} (₦{line['price']:,} each)"
            for line in lines
        ),
        inline=False
    )

    if narration:
        embed.add_field(name="Narration", value=narration, inline=False)

    try:
        await channel.send(embed=embed)
        return True

    except discord.HTTPException:
        return False


async def post_org_receipt(
    guild: discord.Guild,
    payer_name: str,
    org,
    amount: int,
    narration: str = ""
) -> bool:

    """
    Posts the open, unsigned receipt to a Phase 3 current account's
    linked channel (sender name + amount only — no sender balance,
    per spec), and a matching audit line to #cbe-log if that
    channel exists. Both are best-effort: the money has already
    moved by the time this runs, so a missing/deleted channel here
    only means a missing paper trail, not a failed payment. Shared
    by !transfer's "Pay an Organization" path and the phone Bank
    App's equivalent flow. Returns True if the receipt itself
    posted — the #cbe-log audit line is attempted independently and
    doesn't affect the return value, same as before this was split
    out of !transfer.
    """

    receipt_embed = discord.Embed(
        title=f"🧾 Payment Received — {org['name']}",
        color=discord.Color.blurple()
    )
    receipt_embed.add_field(name="From", value=payer_name, inline=True)
    receipt_embed.add_field(name="Amount", value=f"₦{amount:,}", inline=True)

    if narration:
        receipt_embed.add_field(name="Narration", value=narration, inline=False)

    receipt_channel = discord.utils.get(
        guild.text_channels,
        name=org["channel_name"]
    )

    posted = False

    if receipt_channel is not None:

        try:
            await receipt_channel.send(embed=receipt_embed)
            posted = True

        except discord.HTTPException:
            pass

    log_channel = discord.utils.get(
        guild.text_channels,
        name=CBE_LOG_CHANNEL_NAME
    )

    if log_channel is not None:

        # Matches the "Central Bank Deposit" receipt style —
        # From/Source/Amount/Note fields. `Source` names the
        # current account this deposit is tied to (e.g.
        # "Treasury", "Tax_Authority") even though the money
        # itself always settles into the single Central Bank
        # of Eko balance (see current_account_transfer()) —
        # without this field the log can't tell which account
        # a given deposit was actually for.
        log_embed = discord.Embed(
            title="🏛️ Central Bank Deposit",
            color=discord.Color.blurple()
        )
        log_embed.add_field(name="From", value=payer_name, inline=False)
        log_embed.add_field(
            name="Source",
            value=f"{org['name']} (`{org['code']}`)",
            inline=False
        )
        log_embed.add_field(name="Amount", value=f"₦{amount:,}", inline=False)

        if narration:
            log_embed.add_field(name="Note", value=narration, inline=False)

        try:
            await log_channel.send(embed=log_embed)

        except discord.HTTPException:
            pass

    return posted


async def post_business_receipt(
    guild: discord.Guild,
    payer_name: str,
    business,
    amount: int,
    narration: str = ""
) -> bool:

    """
    Posts a lightweight (non-itemized) receipt to a business's
    receipt channel (set via !create-business-account) for a plain
    deposit — i.e. one that didn't settle an open Phase 5 tab (see
    post_register_receipt above for the itemized version). Shared
    by !transfer's "Pay a Business" path and the phone Bank App's
    equivalent flow. Best-effort, same reasoning as post_org_receipt
    above. Returns True if posted, False if no reachable receipt
    channel was found.
    """

    account = database.get_business_account(business["code"])

    receipt_embed = discord.Embed(
        title=f"🧾 Payment Received — {business['name']}",
        color=discord.Color.blurple()
    )
    receipt_embed.add_field(name="From", value=payer_name, inline=True)
    receipt_embed.add_field(name="Amount", value=f"₦{amount:,}", inline=True)

    if narration:
        receipt_embed.add_field(name="Narration", value=narration, inline=False)

    receipt_channel = discord.utils.get(
        guild.text_channels,
        name=account["receipt_channel_name"]
    ) if account is not None else None

    if receipt_channel is None:
        return False

    try:
        await receipt_channel.send(embed=receipt_embed)
        return True

    except discord.HTTPException:
        return False


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

        if not isinstance(ctx.author, discord.Member):
            return

        if not _has_role(ctx.author, BANK_STAFF_ROLE):
            await ctx.send(
                f"⛔ Only **{BANK_STAFF_ROLE}** can open a bank account. "
                f"Individuals can't self-register — ask a teller at "
                f"the front desk."
            )
            return

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
    # !CREATE-CURRENT-ACCOUNT — Phase 3 government/office accounts
    # ============================================================

    @commands.command(name="create-current-account")
    async def create_current_account(
        self,
        ctx: commands.Context,
        code: str,
        channel: discord.TextChannel,
        *,
        name: str
    ):

        if not isinstance(ctx.author, discord.Member):
            return

        if not _has_any_role(ctx.author, BANK_STAFF_ROLES):
            await ctx.send(
                f"⛔ Only **{', '.join(BANK_STAFF_ROLES)}** can register "
                f"a current account."
            )
            return

        sub = await self._require_sub_location(ctx, FRONT_DESK_CODE)

        if sub is None:
            return

        code = code.lower().strip().replace(" ", "-")

        if database.get_current_account(code) is not None:
            await ctx.send(
                f"⛔ A current account with the code `{code}` already exists."
            )
            return

        if not name.strip():
            await ctx.send("⛔ A display name is required.")
            return

        created = database.create_current_account(
            code=code,
            name=name.strip(),
            channel_name=channel.name,
            created_by=ctx.author.id
        )

        if not created:
            await ctx.send(
                "⛔ Current account could not be created — the code was "
                "taken between the check above and now. Try again."
            )
            return

        embed = discord.Embed(
            title="🏛️ Current Account Registered",
            color=discord.Color.green()
        )
        embed.add_field(name="Name", value=name.strip(), inline=False)
        embed.add_field(name="Code", value=f"`{code}`", inline=True)
        embed.add_field(name="Receipt Channel", value=channel.mention, inline=True)
        embed.add_field(
            name="How to pay",
            value=f"`!transfer {code} <amount> [narration]`",
            inline=False
        )

        await ctx.send(embed=embed)

    # ============================================================
    # !CREATE-BUSINESS-ACCOUNT — Phase 4 financial half
    # ============================================================
    #
    # Deliberately separate from !business-registration (which
    # lives in cogs/business_admin.py and only creates the
    # location + ownership bookkeeping). This is the step that
    # actually opens a balance for the business to receive money
    # into — a registered business with no account yet simply
    # isn't a valid !transfer/!adjust target (see database.py's
    # has_business_account()).
    # ============================================================

    @commands.command(name="create-business-account")
    async def create_business_account(
        self,
        ctx: commands.Context,
        code: str,
        channel: discord.TextChannel = None
    ):

        if not isinstance(ctx.author, discord.Member):
            return

        sub = await self._require_sub_location(ctx, FRONT_DESK_CODE)

        if sub is None:
            return

        code = code.lower().strip()

        business = database.get_business(code)

        if business is None:
            await ctx.send(_BUSINESS_ACCOUNT_REASONS["no_such_business"])
            return

        is_owner = str(ctx.author.id) == business["owner_id"]

        if not is_owner and not _has_any_role(ctx.author, BANK_STAFF_ROLES):
            await ctx.send(
                f"⛔ Only **{business['name']}**'s owner or bank staff "
                f"(**{', '.join(BANK_STAFF_ROLES)}**) can open its account."
            )
            return

        if channel is not None:
            receipt_channel_name = channel.name

        else:

            location = database.get_location(code)

            if location is None:
                await ctx.send(
                    "⛔ That business has no registered location channel "
                    "to default to — specify a receipt channel."
                )
                return

            receipt_channel_name = location["channel_name"]

        created, reason = database.create_business_account(
            code=code,
            receipt_channel_name=receipt_channel_name,
            created_by=ctx.author.id
        )

        if not created:
            await ctx.send(
                _BUSINESS_ACCOUNT_REASONS.get(reason, f"⛔ Failed ({reason}).")
            )
            return

        embed = discord.Embed(
            title="🏪 Business Account Opened",
            color=discord.Color.green()
        )
        embed.add_field(name="Business", value=business["name"], inline=False)
        embed.add_field(name="Code", value=f"`{code}`", inline=True)
        embed.add_field(name="Starting Balance", value="₦0", inline=True)
        embed.add_field(
            name="Receipt Channel",
            value=f"#{receipt_channel_name}",
            inline=True
        )
        embed.add_field(
            name="How to pay",
            value=f"`!transfer {code} <amount> [narration]`",
            inline=False
        )

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
    # !TRANSFER — bank -> bank, OR bank -> current account
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
            member = None

        # ------------------------------------------------------
        # Player recipient — existing bank -> bank path.
        # ------------------------------------------------------

        if member is not None:

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

            return

        # ------------------------------------------------------
        # Not a player mention — try a Phase 3 current account
        # (org/government) recipient instead.
        # ------------------------------------------------------

        org_code = recipient.strip().lower().replace(" ", "-")
        org = database.get_current_account(org_code)

        if org is not None:

            ok, reason = database.current_account_transfer(
                ctx.author.id,
                org_code,
                amount
            )

            if not ok:
                await ctx.send(_ORG_TRANSFER_REASONS.get(reason, f"⛔ Transfer failed ({reason})."))
                return

            player = database.get_player(ctx.author.id)

            embed = discord.Embed(
                title="🏦 Bank Transfer Sent",
                color=discord.Color.green()
            )
            embed.add_field(name="To", value=org["name"], inline=True)
            embed.add_field(name="Amount", value=f"₦{amount:,}", inline=True)
            embed.add_field(name="Your New Bank Balance", value=f"₦{player['balance']:,}", inline=False)

            if narration:
                embed.add_field(name="Narration", value=narration, inline=False)

            await ctx.send(embed=embed)

            posted = await post_org_receipt(
                ctx.guild, ctx.author.display_name, org, amount, narration
            )

            if not posted:
                await ctx.send(
                    f"⚠️ Payment succeeded, but {org['name']}'s receipt "
                    f"channel `#{org['channel_name']}` no longer exists — "
                    f"no receipt could be posted."
                )

            return

        # ------------------------------------------------------
        # Not a current account either — try a Phase 4 business
        # recipient (only valid once it has an open account).
        # ------------------------------------------------------

        business = database.get_business(org_code)

        if business is None:
            await ctx.send(
                "⛔ That's not a player mention, a recognized current "
                "account, or a registered business."
            )
            return

        # --------------------------------------------------------
        # Phase 5 — if the sender has an OPEN !buy tab with this
        # business, the amount must match it exactly (mismatch
        # rejected outright, same as a normal deposit of the wrong
        # amount would otherwise silently go through). No open tab
        # -> falls through to a plain business deposit, unchanged.
        # --------------------------------------------------------

        register = database.get_open_register(org_code, ctx.author.id)

        if register is not None and register["total"] != amount:
            await ctx.send(
                f"⛔ You have an open tab at **{business['name']}** — "
                f"the amount must exactly match the tab total of "
                f"₦{register['total']:,}."
            )
            return

        ok, reason = database.business_account_transfer(
            ctx.author.id,
            org_code,
            amount
        )

        if not ok:
            await ctx.send(_BUSINESS_TRANSFER_REASONS.get(reason, f"⛔ Transfer failed ({reason})."))
            return

        player = database.get_player(ctx.author.id)

        embed = discord.Embed(
            title="🏦 Bank Transfer Sent",
            color=discord.Color.green()
        )
        embed.add_field(name="To", value=business["name"], inline=True)
        embed.add_field(name="Amount", value=f"₦{amount:,}", inline=True)
        embed.add_field(name="Your New Bank Balance", value=f"₦{player['balance']:,}", inline=False)

        if narration:
            embed.add_field(name="Narration", value=narration, inline=False)

        await ctx.send(embed=embed)

        if register is not None:

            settle_ok, _ = database.settle_register(register["register_id"], amount)

            if settle_ok:

                posted = await post_register_receipt(
                    ctx.guild, ctx.author.display_name, business, register, narration
                )

                if not posted:
                    await ctx.send(
                        f"⚠️ Tab settled, but no receipt channel could be "
                        f"reached — no receipt was posted."
                    )

            else:
                await ctx.send(
                    "⚠️ Payment succeeded, but the tab couldn't be "
                    "automatically marked paid (it may have changed in "
                    "the meantime) — the owner should check `!menu`/"
                    "`!close-register` and sort it out manually."
                )

                posted = await post_business_receipt(
                    ctx.guild, ctx.author.display_name, business, amount, narration
                )

                if not posted:
                    await ctx.send(
                        f"⚠️ Payment succeeded, but {business['name']}'s "
                        f"receipt channel no longer exists — no receipt "
                        f"could be posted."
                    )

        else:

            posted = await post_business_receipt(
                ctx.guild, ctx.author.display_name, business, amount, narration
            )

            if not posted:
                await ctx.send(
                    f"⚠️ Payment succeeded, but {business['name']}'s receipt "
                    f"channel no longer exists — no receipt could be posted."
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

        # ------------------------------------------------------
        # Phase 5 — does the receiver own a business with an OPEN
        # !buy tab against the payer? If so, `amount` must exactly
        # match one of those tabs (mismatch rejected) and accepting
        # will settle it instead of being a plain cash transfer.
        # ------------------------------------------------------

        open_registers = database.get_open_registers_for_owner_customer(
            receiver.id, ctx.author.id
        )

        register = None

        if open_registers:

            register = next(
                (r for r in open_registers if r["total"] == amount),
                None
            )

            if register is None:
                totals = ", ".join(
                    f"₦{r['total']:,} at **{r['business_name']}**"
                    for r in open_registers
                )
                await ctx.send(
                    f"⛔ You have an open tab with {receiver.mention} — "
                    f"the amount must exactly match one of: {totals}."
                )
                return

        embed = discord.Embed(
            title="💵 Cash Payment Requested",
            color=discord.Color.gold()
        )
        embed.add_field(name="From", value=ctx.author.mention, inline=True)
        embed.add_field(name="To", value=receiver.mention, inline=True)
        embed.add_field(name="Amount", value=f"₦{amount:,}", inline=True)

        if register is not None:
            embed.add_field(
                name="Settles Tab At",
                value=register["business_name"],
                inline=True
            )

        if narration:
            embed.add_field(name="Narration", value=narration, inline=False)

        view = _PayView(
            payer_id=ctx.author.id,
            receiver_id=receiver.id,
            amount=amount,
            narration=narration,
            register=register,
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

        players = database.all_players_with_bank_accounts()
        institutions = database.all_institution_accounts()
        business_accounts = database.all_business_accounts()

        # One flat, numbered list — institution accounts first,
        # then business accounts, then players — each tagged with
        # an account-type label. No tier/lock styling here: that's
        # a separate feature for later, not part of this pass.
        rows = []

        for row in institutions:
            rows.append((row["name"], row["code"], "Current", row["balance"]))

        for row in business_accounts:
            rows.append((row["name"], row["code"], "Business", row["balance"]))

        for row in players:

            member = ctx.guild.get_member(int(row["user_id"]))
            label = member.display_name if member else f"User {row['user_id']}"

            rows.append((label, str(row["user_id"]), "Savings", row["balance"]))

        lines = [
            f"**{idx:02d}.** {name} (`{code}`) — {account_type} — ₦{balance:,}"
            for idx, (name, code, account_type, balance)
            in enumerate(rows, start=1)
        ]

        chunks = [
            lines[i:i + VIEW_BALANCES_CHUNK_SIZE]
            for i in range(0, len(lines), VIEW_BALANCES_CHUNK_SIZE)
        ] or [[]]

        footer_note = (
            "ℹ️ Current accounts (Phase 3, e.g. registered offices) hold "
            "no balance of their own — every payment into one sweeps "
            "straight into the Central Bank of Eko balance above. "
            "Business balances are standalone and never swept into it."
        )

        try:

            for chunk_index, chunk in enumerate(chunks):

                embed = discord.Embed(
                    title="🏦 All Account Balances",
                    description="\n".join(chunk) if chunk else "_Nothing to show._",
                    color=discord.Color.blurple()
                )

                if chunk_index == len(chunks) - 1:
                    embed.set_footer(text=footer_note)

                await ctx.author.send(embed=embed)

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
        business = None

        try:
            member = await commands.MemberConverter().convert(ctx, target)

        except commands.BadArgument:

            slug = target.strip().lower()
            lowered = slug.replace("-", "_")

            if lowered in ("central_bank", "cbe", "centralbank"):
                institution_code = "central_bank"

            elif lowered == "treasury":
                institution_code = "treasury"

            else:

                business = database.get_business(slug)

                if business is None:
                    await ctx.send(
                        "⛔ That's not a player mention, a recognized "
                        "institution account (`central_bank` / "
                        "`treasury`), or a registered business. Current "
                        "accounts (Phase 3) hold no balance and can "
                        "never be adjusted directly."
                    )
                    return

        if member is not None:
            ok, reason = database.adjust_player_balance(member.id, amount)
            new_balance = database.get_player(member.id)["balance"] if ok else None
            account_label = member.mention

        elif institution_code is not None:
            ok, reason = database.adjust_institution_balance(institution_code, amount)
            new_balance = database.get_institution_account(institution_code)["balance"] if ok else None
            account_label = database.get_institution_account(institution_code)["name"]

        else:

            if database.get_business_account(business["code"]) is None:
                await ctx.send(
                    f"⛔ **{business['name']}** hasn't opened a business "
                    f"account yet — run `!create-business-account "
                    f"{business['code']}` first."
                )
                return

            ok, reason = database.adjust_business_balance(business["code"], amount)
            new_balance = database.get_business_account(business["code"])["balance"] if ok else None
            account_label = f"{business['name']} (business)"

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

    def __init__(
        self,
        payer_id: int,
        receiver_id: int,
        amount: int,
        narration: str = "",
        register=None
    ):
        super().__init__(timeout=PAY_RESPONSE_TIMEOUT_SECONDS)
        self.payer_id = payer_id
        self.receiver_id = receiver_id
        self.amount = amount
        self.narration = narration
        # Phase 5 — an open cash_registers row (from
        # database.get_open_registers_for_owner_customer()) if this
        # payment's amount matched an open tab the receiver's
        # business has with the payer, else None for a plain
        # cash transfer.
        self.register = register
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

        content = f"✅ Payment of ₦{self.amount:,} accepted."

        # ----------------------------------------------------
        # Phase 5 — settle the matched tab, if any (see !pay's
        # lookup above). The cash has already moved either way;
        # this only flips the tab to "paid" (ready for !sell) and
        # posts the itemized receipt.
        # ----------------------------------------------------

        if self.register is not None:

            settle_ok, _ = database.settle_register(
                self.register["register_id"], self.amount
            )

            business = database.get_business(self.register["business_code"])

            if settle_ok and business is not None:

                payer_member = interaction.guild.get_member(self.payer_id)
                payer_name = payer_member.display_name if payer_member else "Unknown"

                posted = await post_register_receipt(
                    interaction.guild,
                    payer_name,
                    business,
                    self.register,
                    self.narration,
                )

                content += f" Tab at **{business['name']}** marked paid."

                if not posted:
                    content += " (No receipt channel could be reached.)"

            else:
                content += (
                    " ⚠️ The tab couldn't be automatically marked paid "
                    "(it may have changed) — the owner should sort it "
                    "out manually."
                )

        await interaction.response.edit_message(
            content=content,
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
