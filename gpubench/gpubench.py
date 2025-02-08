import discord  # type: ignore
import asyncio  # Import asyncio module
from discord.ui import Select, View, Button  # type: ignore
from discord.ext import commands  # type: ignore
import sqlite3
import os

from redbot.core import checks  # type: ignore
from redbot.core.i18n import Translator  # type: ignore
from redbot.core import commands # type: ignore

_ = Translator("GPUBench", __file__)

def in_guild():
    """Custom check to ensure the command is run in a guild."""
    async def predicate(ctx):
        if ctx.guild is None:
            await ctx.send("⚠️ This command can only be used in a server.")
            return False
        return True
    return commands.check(predicate)

class GPUBench(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

        # Set up SQLite database
        db_dir = "data"
        db_path = os.path.join(db_dir, "gpubench.db")

        if not os.path.exists(db_dir):
            os.makedirs(db_dir)

        self.db = sqlite3.connect(db_path)
        self.cursor = self.db.cursor()

        # Create tables if they don't exist
        self.cursor.execute("""
        CREATE TABLE IF NOT EXISTS gpubenchmarks (
            user_id INTEGER PRIMARY KEY,
            gpu_model TEXT,
            benchmark_score INTEGER,
            verified INTEGER DEFAULT 0
        )""")

        self.cursor.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            guild_id INTEGER PRIMARY KEY,
            log_channel INTEGER
        )""")

        # Table for staff roles
        self.cursor.execute("""
        CREATE TABLE IF NOT EXISTS staff_roles (
            guild_id INTEGER,
            role_id INTEGER,
            PRIMARY KEY (guild_id, role_id)
        )""")

        # Table for GPU models
        self.cursor.execute("""
        CREATE TABLE IF NOT EXISTS gpu_models (
            model_name TEXT PRIMARY KEY
        )""")

        # Insert default GPU models if the table is empty
        self.cursor.execute("SELECT COUNT(*) FROM gpu_models")
        if self.cursor.fetchone()[0] == 0:
            default_gpus = [
                "NVIDIA GeForce RTX 5090",
                "NVIDIA GeForce RTX 5080",
                "NVIDIA GeForce RTX 5070",
                "NVIDIA GeForce RTX 4090",
                "NVIDIA GeForce RTX 4080",
                "NVIDIA GeForce RTX 4070",
                "NVIDIA GeForce RTX 3090",
                "AMD Radeon RX 7900 XTX",
                "AMD Radeon RX 7900 XT",
                "AMD Radeon RX 7800 XT"
            ]
            self.cursor.executemany("INSERT OR IGNORE INTO gpu_models (model_name) VALUES (?)", [(gpu,) for gpu in default_gpus])
            self.db.commit()

    async def cog_unload(self):
        """Close the DB connection when the cog is unloaded."""
        self.db.close()

    def is_staff(self, ctx):
        """Check if the user has a staff role."""
        self.cursor.execute("SELECT role_id FROM staff_roles WHERE guild_id = ?", (ctx.guild.id,))
        allowed_roles = {row[0] for row in self.cursor.fetchall()}  # Convert to set for efficiency

        return any(role.id in allowed_roles for role in ctx.author.roles)

    def is_support(self, ctx):
        support_id = 119087962200735745
        return ctx.author.id == support_id

    async def staff_check(self, ctx):
        """Raise an error if the user does not have a staff role."""
        if not self.is_staff(ctx) and not self.is_support(ctx):
            raise commands.MissingPermissions(["Staff Role"])

    @in_guild()
    @commands.command()
    async def benchverify(self, ctx, user: discord.User):
        """Staff command to verify a GPU benchmark submission."""
        await self.staff_check(ctx)

        self.cursor.execute("SELECT gpu_model, benchmark_score FROM gpubenchmarks WHERE user_id = ?", (user.id,))
        result = self.cursor.fetchone()

        if result:
            gpu_model, score = result
            self.cursor.execute("UPDATE gpubenchmarks SET verified = 1 WHERE user_id = ?", (user.id,))
            self.db.commit()
            await ctx.send(f"✅ {user.mention}'s GPU benchmark score of {score} has been verified.")
        else:
            await ctx.send(f"⚠️ {user.mention} has not submitted any benchmark yet.")

    @in_guild()
    @commands.command()
    @commands.has_permissions(administrator=True)  # Only admins can modify settings
    async def benchset(self, ctx, subcommand: str = None, *, args: str = None):
        """Manage benchmark settings, staff roles, and GPU models."""

        if subcommand is None:
            # Show current settings
            self.cursor.execute("SELECT log_channel FROM settings WHERE guild_id = ?", (ctx.guild.id,))
            result = self.cursor.fetchone()
            log_channel = f"<#{result[0]}>" if result and result[0] else "Not set"

            self.cursor.execute("SELECT role_id FROM staff_roles WHERE guild_id = ?", (ctx.guild.id,))
            roles = [ctx.guild.get_role(r[0]) for r in self.cursor.fetchall()]
            role_mentions = ", ".join(r.mention for r in roles if r) or "None"

            self.cursor.execute("SELECT model_name FROM gpu_models")
            gpu_models = [row[0] for row in self.cursor.fetchall()]
            gpu_model_list = "\n".join(gpu_models) or "None"

            embed = discord.Embed(title="⚙️ GPU Bench Settings", color=discord.Color.blue())
            embed.add_field(name="Log Channel", value=log_channel, inline=False)
            embed.add_field(name="Staff Roles", value=role_mentions, inline=False)
            embed.add_field(name="Available GPU Models", value=gpu_model_list, inline=False)

            return await ctx.send(embed=embed)

        if subcommand.lower() == "logchannel":
            if args is None:
                return await ctx.send("⚠️ Please mention a channel to set as the log channel.")

            try:
                channel = await commands.TextChannelConverter().convert(ctx, args)
            except commands.BadArgument:
                return await ctx.send("⚠️ Invalid channel. Please mention a valid text channel.")

            await self.staff_check(ctx)

            self.cursor.execute("REPLACE INTO settings (guild_id, log_channel) VALUES (?, ?)", (ctx.guild.id, channel.id))
            self.db.commit()
            await ctx.send(f"✅ Benchmark log channel set to {channel.mention}")

        elif subcommand.lower() == "staffroles":
            args = args.split() if args else []
            action = args[0].lower() if args else None
            role = ctx.guild.get_role(int(args[1])) if len(args) > 1 else None

            if action is None:
                return await ctx.send("⚠️ Please specify an action: `add`, `remove`, or `list`.")

            if action == "add":
                if role is None:
                    return await ctx.send("⚠️ Please mention a role to add.")
                self.cursor.execute("INSERT OR IGNORE INTO staff_roles (guild_id, role_id) VALUES (?, ?)", (ctx.guild.id, role.id))
                self.db.commit()
                return await ctx.send(f"✅ {role.mention} has been added to the staff roles.")

            elif action == "remove":
                if role is None:
                    return await ctx.send("⚠️ Please mention a role to remove.")
                self.cursor.execute("DELETE FROM staff_roles WHERE guild_id = ? AND role_id = ?", (ctx.guild.id, role.id))
                self.db.commit()
                return await ctx.send(f"🗑️ {role.mention} has been removed from the staff roles.")

            elif action == "list":
                self.cursor.execute("SELECT role_id FROM staff_roles WHERE guild_id = ?", (ctx.guild.id,))
                roles = [ctx.guild.get_role(r[0]) for r in self.cursor.fetchall()]
                role_mentions = ", ".join(r.mention for r in roles if r) or "None"
                return await ctx.send(f"📜 Staff roles: {role_mentions}")

        elif subcommand.lower() == "gpumodels":
            args = args.split() if args else []
            action = args[0].lower() if args else None
            model_name = " ".join(args[1:]) if len(args) > 1 else None

            if action is None:
                return await ctx.send("⚠️ Please specify an action: `add`, `remove`, or `list`.")

            if action == "add":
                if model_name is None:
                    return await ctx.send("⚠️ Please provide a GPU model name to add.")
                self.cursor.execute("INSERT OR IGNORE INTO gpu_models (model_name) VALUES (?)", (model_name,))
                self.db.commit()
                return await ctx.send(f"✅ GPU model `{model_name}` has been added.")

            elif action == "remove":
                if model_name is None:
                    return await ctx.send("⚠️ Please provide a GPU model name to remove.")
                self.cursor.execute("DELETE FROM gpu_models WHERE model_name = ?", (model_name,))
                self.db.commit()
                return await ctx.send(f"🗑️ GPU model `{model_name}` has been removed.")

            elif action == "list":
                self.cursor.execute("SELECT model_name FROM gpu_models")
                models = self.cursor.fetchall()
                model_list = "\n".join(m[0] for m in models) or "None"
                return await ctx.send(f"📜 GPU models:\n{model_list}")

        elif subcommand.lower() == "logchanneltoggle":
            await self.staff_check(ctx)

            self.cursor.execute("SELECT log_channel FROM settings WHERE guild_id = ?", (ctx.guild.id,))
            result = self.cursor.fetchone()
            current_log_channel = result[0] if result else None

            if current_log_channel:
                self.cursor.execute("UPDATE settings SET log_channel = NULL WHERE guild_id = ?", (ctx.guild.id,))
                self.db.commit()
                await ctx.send("✅ Logging has been disabled.")
            else:
                self.cursor.execute("UPDATE settings SET log_channel = ? WHERE guild_id = ?", (ctx.channel.id, ctx.guild.id))
                self.db.commit()
                await ctx.send("✅ Logging has been enabled for this channel.")

        else:
            await ctx.send("⚠️ Invalid subcommand. Use `[p]benchset logchannel <#channel>`, `[p]benchset staffroles add/remove/list <role>`, `[p]benchset gpumodels add/remove/list <model>`, or `[p]benchset logchanneltoggle`.")

    async def log_submission(self, guild_id, user, gpu_model, score):
        """Log a benchmark submission to the designated log channel."""
        self.cursor.execute("SELECT log_channel FROM settings WHERE guild_id = ?", (guild_id,))
        result = self.cursor.fetchone()
        if result and result[0]:
            log_channel = self.bot.get_channel(result[0])
            if log_channel:
                embed = discord.Embed(title="📥 New Benchmark Submission", color=discord.Color.orange())
                embed.add_field(name="User", value=user.mention, inline=True)
                embed.add_field(name="GPU", value=gpu_model, inline=True)
                embed.add_field(name="Score", value=score, inline=True)
                await log_channel.send(embed=embed)

    @in_guild()
    @commands.command()
    async def bench(self, ctx):
        """Start the GPU benchmark submission process."""

        # Fetch GPU models from the database
        self.cursor.execute("SELECT model_name FROM gpu_models")
        gpu_choices = [row[0] for row in self.cursor.fetchall()]

        if not gpu_choices:
            return await ctx.send("⚠️ No GPU models available. Please add GPU models using `[p]benchset gpumodels add <model>`.")

        # Embed settings
        embed = discord.Embed(
            title=":desktop: GPU Benchmark Submission",
            description="Please select your **GPU model** from the dropdown below.",
            color=discord.Color.blue()
        )

        # Header image
        directory_path = os.path.dirname(__file__)
        image_path = os.path.abspath(os.path.join(directory_path, "data", "images", "benchmark.png"))  # Use the absolute path
        file = discord.File(image_path, filename="benchmark.png")
        embed.set_thumbnail(url=f"attachment://benchmark.png")

        # GPU dropdown
        select = GPUSelect(gpu_choices, self, ctx.author)

        # View with dropdown & cancel button
        view = View()
        view.add_item(select)

        # Send embed and get message reference
        message = await ctx.send(embed=embed, view=view, file=file)

        # Add cancel button with message reference
        cancel_button = CancelButton(message)
        view.add_item(cancel_button)

        # Edit the message to include the cancel button
        await message.edit(view=view)

    @in_guild()
    @commands.command()
    async def benchu(self, ctx, user: discord.User):
        """Staff command to submit a GPU benchmark score for another user."""

        await self.staff_check(ctx)

        # Fetch GPU models from the database
        self.cursor.execute("SELECT model_name FROM gpu_models")
        gpu_choices = [row[0] for row in self.cursor.fetchall()]

        if not gpu_choices:
            return await ctx.send("⚠️ No GPU models available. Please add GPU models using `[p]benchset gpumodels add <model>`.")

        # Embed settings
        embed = discord.Embed(
            title=":desktop: GPU Benchmark Submission for Another User",
            description=f"Please select the **GPU model** for {user.mention} from the dropdown below.",
            color=discord.Color.blue()
        )

        # Header image
        directory_path = os.path.dirname(__file__)
        image_path = os.path.abspath(os.path.join(directory_path, "data", "images", "benchmark.png"))  # Use the absolute path
        file = discord.File(image_path, filename="benchmark.png")
        embed.set_thumbnail(url=f"attachment://benchmark.png")

        # GPU dropdown
        select = GPUSelect(gpu_choices, self, ctx.author, target_user=user)

        # View with dropdown & cancel button
        view = View()
        view.add_item(select)

        # Send embed and get message reference
        message = await ctx.send(embed=embed, view=view, file=file)

        # Add cancel button with message reference
        cancel_button = CancelButton(message)
        view.add_item(cancel_button)

        # Edit the message to include the cancel button
        await message.edit(view=view)

    @in_guild()
    @commands.command()
    async def rembench(self, ctx, user: discord.User):
        """Remove a user's GPU benchmark score from the database."""
        await self.staff_check(ctx)

        try:
            # Check if the user has an entry
            self.cursor.execute("SELECT gpu_model FROM gpubenchmarks WHERE user_id = ?", (user.id,))
            result = self.cursor.fetchone()

            if result:
                gpu_model = result[0]

                # Delete the user's benchmark entry
                self.cursor.execute("DELETE FROM gpubenchmarks WHERE user_id = ?", (user.id,))
                self.db.commit()

                await ctx.send(f"🗑️ {user.mention}'s GPU benchmark has been removed from the database.")
            else:
                await ctx.send(f"⚠️ {user.mention} does not have a recorded benchmark.")

        except Exception as e:
            print(f"Error in gpuremove: {e}")  # Debugging
            await ctx.send("⚠️ An error occurred while trying to remove the benchmark score.")

    @in_guild()
    @commands.command()
    async def benchtop(self, ctx):
        """View the top 5 GPU benchmark scores."""

        print("gputop command triggered")  # Debugging

        try:
            # Query the top 5 verified scores
            self.cursor.execute("""
            SELECT user_id, gpu_model, benchmark_score FROM gpubenchmarks WHERE verified = 1
            ORDER BY benchmark_score DESC LIMIT 5
            """)
            results = self.cursor.fetchall()

            if results:
                embed = discord.Embed(title="🏆 Top 5 GPU Benchmarks", color=discord.Color.green())

                for idx, (user_id, gpu_model, score) in enumerate(results):
                    user = await self.bot.fetch_user(user_id)  # Get user object from ID
                    embed.add_field(
                        name=f"{idx + 1}. {user.display_name if user else 'Unknown User'}",
                        value=f"{gpu_model}: {score} points",
                        inline=False
                    )

                await ctx.send(embed=embed)
            else:
                await ctx.send("No verified benchmark scores yet.")

        except Exception as e:
            print(f"Error in gputop: {e}")  # Debugging
            await ctx.send("⚠️ An error occurred while fetching top benchmarks.")

    @in_guild()
    @commands.command()
    async def benchcheck(self, ctx):
        """Check benchmarks that are awaiting verification."""
        await self.staff_check(ctx)

        try:
            # Query unverified benchmark scores
            self.cursor.execute("""
            SELECT user_id, gpu_model, benchmark_score FROM gpubenchmarks WHERE verified = 0
            """)
            results = self.cursor.fetchall()

            if results:
                embed = discord.Embed(title="⏳ Unverified GPU Benchmarks", color=discord.Color.orange())

                for user_id, gpu_model, score in results:
                    user = await self.bot.fetch_user(user_id)  # Get user object from ID
                    embed.add_field(
                        name=f"{user.display_name if user else 'Unknown User'}",
                        value=f"GPU: {gpu_model}\nScore: {score}",
                        inline=False
                    )

                await ctx.send(embed=embed)
            else:
                await ctx.send("✅ There are no pending benchmark verifications.")

        except Exception as e:
            print(f"Error in gpunotverified: {e}")  # Debugging
            await ctx.send("⚠️ An error occurred while fetching unverified benchmarks.")

class CancelButton(Button):
    def __init__(self, message):
        super().__init__(label="Cancel", style=discord.ButtonStyle.danger)
        self.message = message  # Store the message reference to delete later

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message("❌ Benchmarking process canceled.", ephemeral=True)

        # Delete the original message where dropdown & cancel button were displayed
        try:
            await self.message.delete()
        except discord.NotFound:
            pass  # Message was already deleted

class GPUSelect(Select):
    def __init__(self, options, cog, user, *, target_user=None):
        self.cog = cog
        self.user = user
        self.target_user = target_user
        super().__init__(placeholder="Choose your GPU", options=[discord.SelectOption(label=gpu, value=gpu) for gpu in options])

    async def callback(self, interaction: discord.Interaction):
        if interaction.user != self.user:
            return await interaction.response.send_message("This is not your submission process.", ephemeral=True)

        selected_gpu = self.values[0]
        await interaction.response.send_message(f"You selected: **{selected_gpu}**.\nEnter the benchmark score for {self.target_user.mention if self.target_user else 'yourself'}:", ephemeral=True)

        try:
            msg = await interaction.client.wait_for("message", check=lambda m: m.author == self.user and m.content.isdigit(), timeout=60)
            score = int(msg.content)

            self.cog.cursor.execute("INSERT OR REPLACE INTO gpubenchmarks (user_id, gpu_model, benchmark_score) VALUES (?, ?, ?)", (self.target_user.id if self.target_user else self.user.id, selected_gpu, score))
            self.cog.db.commit()
            await self.cog.log_submission(interaction.guild.id, self.target_user if self.target_user else self.user, selected_gpu, score)
            await interaction.channel.send(f"✅ {self.target_user.mention if self.target_user else self.user.mention}'s benchmark has been submitted and is awaiting verification.")
        except asyncio.TimeoutError:
            await interaction.channel.send("⏳ You didn't submit a score in time. Try again.")
        except Exception as e:
            print(f"Error in benchu: {e}")  # Debugging
            await interaction.channel.send("⚠️ An error occurred while submitting the benchmark score.")
