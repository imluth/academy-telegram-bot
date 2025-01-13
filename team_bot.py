import os
import logging
from datetime import datetime, timedelta
from collections import defaultdict
import asyncio
import random
import sys
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, filters
from telegram.error import BadRequest, RetryAfter
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

class RateLimiter:
    """Rate limiter to control request frequency per user"""
    def __init__(self, rate_limit=3, per_seconds=1):  # Allow 3 requests per second
        self.rate_limit = rate_limit
        self.per_seconds = per_seconds
        self.requests = defaultdict(list)
        self.cooldowns = defaultdict(float)
    
    async def acquire(self, user_id, action_type="default"):
        """
        Check if a user can perform an action based on rate limits
        Returns (allowed, wait_time)
        """
        now = datetime.now()
        
        # Check cooldown only for admin actions
        if action_type in ["start_play", "cancel_play"]:
            cooldown_key = f"{user_id}_{action_type}"
            if now.timestamp() < self.cooldowns[cooldown_key]:
                wait_time = self.cooldowns[cooldown_key] - now.timestamp()
                return False, wait_time
        
        # Clean old requests
        self.requests[user_id] = [
            req_time for req_time in self.requests[user_id]
            if now - req_time < timedelta(seconds=self.per_seconds)
        ]
        
        if len(self.requests[user_id]) >= self.rate_limit:
            wait_time = self.per_seconds - (now - self.requests[user_id][0]).total_seconds()
            if wait_time > 0:
                return False, wait_time
        
        self.requests[user_id].append(now)
        
        # Set cooldown only for admin actions
        if action_type in ["start_play", "cancel_play"]:
            cooldown_key = f"{user_id}_{action_type}"
            self.cooldowns[cooldown_key] = now.timestamp() + 5  # 5-second cooldown for admin actions
        
        return True, 0

class MessageDebouncer:
    """Debounce message updates to prevent rapid updates"""
    def __init__(self, delay=0.5):  # Reduced to 0.5 seconds
        self.delay = delay
        self.last_update = defaultdict(float)
    
    async def should_update(self, message_id):
        now = datetime.now().timestamp()
        # Allow more frequent updates initially
        if not self.last_update[message_id]:
            self.last_update[message_id] = now
            return True
        if now - self.last_update[message_id] < self.delay:
            return False
        self.last_update[message_id] = now
        return True

class FootballPlayBot:
    def __init__(self, token):
        self.token = token
        self.players = []
        self.max_players = 12
        self.play_open = False
        self.current_play_day = None
        self.last_team_message = None
        self.last_error_time = None
        self.error_count = 0
        
        # Create logs directory if it doesn't exist
        self.logs_dir = 'logs'
        try:
            os.makedirs(self.logs_dir, exist_ok=True)
        except Exception as e:
            print(f"Warning: Could not create logs directory: {e}")
            self.logs_dir = '/tmp'  # Fallback to /tmp if /app/logs is not writable
        
        # Configure logging
        self.setup_logging()
        
        # Initialize rate limiter, update lock, and message debouncer with more lenient settings
        self.rate_limiter = RateLimiter(rate_limit=3, per_seconds=1)  # 3 requests per second
        self.update_lock = asyncio.Lock()
        self.retry_delays = defaultdict(int)
        self.message_debouncer = MessageDebouncer(delay=0.5)  # 0.5 second delay
        
        # Play details for different days
        self.play_details = {
            'Sat': {
                'day': 'Saturday Night',
                'time': '10pm to 11pm',
                'location': 'Teenage Ground'
            },
            'Wed': {
                'day': 'Wednesday Night', 
                'time': '11pm to 12am',
                'location': 'Teenage Ground'
            }
        }
    
    def setup_logging(self):
        """Set up logging with date-based log files and rotation"""
        try:
            self.logger = logging.getLogger('FootballPlayBot')
            self.logger.setLevel(logging.INFO)
            
            # Create a file handler with today's date
            today = datetime.now().strftime('%Y-%m-%d')
            log_file = os.path.join(self.logs_dir, f'{today}_football_play_bot.log')
            
            # Create handlers with rotation
            file_handler = logging.FileHandler(log_file)
            file_handler.setLevel(logging.INFO)
            
            console_handler = logging.StreamHandler()
            console_handler.setLevel(logging.INFO)
            
            # Create formatter
            formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
            file_handler.setFormatter(formatter)
            console_handler.setFormatter(formatter)
            
            # Add handlers
            self.logger.addHandler(file_handler)
            self.logger.addHandler(console_handler)
            
            self.logger.info("Logging setup completed successfully")
            
        except Exception as e:
            print(f"Warning: Could not set up file logging: {e}")
            # Fallback to console-only logging
            self.logger = logging.getLogger('FootballPlayBot')
            self.logger.setLevel(logging.INFO)
            console_handler = logging.StreamHandler()
            console_handler.setLevel(logging.INFO)
            formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
            console_handler.setFormatter(formatter)
            self.logger.addHandler(console_handler)
    
    def format_player_list(self):
        """Format the player list with play details"""
        if not self.current_play_day:
            return "No play day selected"
        
        details = self.play_details[self.current_play_day]
        
        list_lines = [
            f"**{details['day']} Play {details['time']}**",
            f"{details['location']}\n",
            "In List :"
        ]
        
        for i in range(1, self.max_players + 1):
            if i <= len(self.players):
                player = self.players[i-1]
                player_display = player['username']
                if player.get('is_plus_one'):
                    player_display += " (+1)"
                list_lines.append(f"{i}. {player_display}")
            else:
                list_lines.append(f"{i}.")
        
        return "\n".join(list_lines)
    
    def format_teams_message(self, teams):
        """Format the teams message with play details"""
        if not self.current_play_day or not self.play_details.get(self.current_play_day):
            return "Error: Play day not set"

        details = self.play_details[self.current_play_day]

        message = (
            f"**{details['day']} Play {details['time']}**\n"
            f"{details['location']}\n\n"
            f"**Team List :**\n"
            f"Team Black ⚫️ :\n" + "\n".join(
                f"- {p['username']}{' (+1)' if p.get('is_plus_one') else ''}"
                for p in teams[0]
            ) + "\n\n" +
            f"Team White ⚪️ :\n" + "\n".join(
                f"- {p['username']}{' (+1)' if p.get('is_plus_one') else ''}"
                for p in teams[1]
            )
        )
        return message
    
    async def start_play(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Initialize a new play signup list with admin check and rate limiting"""
        try:
            user = update.effective_user
            self.logger.info(f"Start play attempt by {user.username}")
            
            # Check rate limit for starting new games
            allowed, wait_time = await self.rate_limiter.acquire(user.id, "start_play")
            if not allowed:
                await update.message.reply_text(
                    f"Please wait {wait_time:.1f} seconds before starting a new play list."
                )
                return
            
            # Admin check for groups
            if update.message.chat.type in ['group', 'supergroup']:
                try:
                    chat_member = await update.message.chat.get_member(user.id)
                    if chat_member.status not in ['administrator', 'creator']:
                        self.logger.warning(f"Non-admin {user.username} attempted to start play")
                        await update.message.reply_text(
                            "❌ Sorry, only group administrators can start a play list."
                        )
                        return
                except Exception as e:
                    self.logger.error(f"Error checking admin status: {str(e)}")
                    await update.message.reply_text(
                        f"Error checking admin status: {str(e)}"
                    )
                    return
            
            # Check for active play list
            if self.play_open:
                self.logger.warning("Attempt to start play when a list is already in progress")
                await update.message.reply_text(
                    "A play list is already in progress! Please use /cancel_play first."
                )
                return
            
            # Determine play day
            if update.message.text.lower().startswith('/play wed'):
                play_day = 'Wed'
            elif update.message.text.lower().startswith('/play sat'):
                play_day = 'Sat'
            else:
                await update.message.reply_text(
                    "Please use:\n"
                    "/play Wed\n"
                    "/play Sat"
                )
                return
            
            # Initialize new play list
            async with self.update_lock:
                self.players = []
                self.play_open = True
                self.current_play_day = play_day
                self.error_count = 0
                
                self.logger.info(f"Play list started for {play_day}")
                
                keyboard = [
                    [
                        InlineKeyboardButton("✅ In", callback_data='join_play'),
                        InlineKeyboardButton("✅+1", callback_data='join_play_plus_one'),
                        InlineKeyboardButton("❌ Out", callback_data='cancel_join')
                    ]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                try:
                    message = await update.message.reply_text(
                        self.format_player_list(),
                        reply_markup=reply_markup
                    )
                    self.logger.info(f"Play list message sent successfully for {play_day}")
                except Exception as e:
                    self.logger.error(f"Error sending initial play list message: {str(e)}")
                    self.play_open = False
                    await update.message.reply_text(
                        "Error starting play list. Please try again."
                    )
                    
        except Exception as e:
            self.logger.error(f"Error in start_play: {str(e)}")
            await update.message.reply_text(
                "An error occurred while starting the play list. Please try again."
            )
            self.play_open = False
    
    async def handle_play_response(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle inline keyboard responses with rate limiting and error handling"""
        try:
            query = update.callback_query
            user_id = query.from_user.id
            
            # Check rate limit
            allowed, wait_time = await self.rate_limiter.acquire(user_id)
            if not allowed:
                await query.answer(
                    f"Please wait {wait_time:.1f} seconds before trying again",
                    show_alert=True
                )
                return
            
            # Use lock for thread safety
            async with self.update_lock:
                try:
                    await query.answer()
                except Exception as e:
                    if "Query is too old" in str(e):
                        self.logger.warning("Ignored old query callback")
                        return
                    raise
                
                if not self.play_open:
                    self.logger.warning("No active play list")
                    try:
                        await query.edit_message_text(
                            "No active play list. Use /play Wed or /play Sat",
                            reply_markup=None
                        )
                    except Exception as e:
                        self.logger.warning(f"Could not update message: {str(e)}")
                    return
                
                # Process user action
                message_changed = await self._process_user_action(query)
                if not message_changed:
                    return
                
                # Check if we should update the message (debouncing)
                if not await self.message_debouncer.should_update(query.message.message_id):
                    return
                
                # Update message with backoff
                await self._update_message_with_backoff(query, context)
                
        except RetryAfter as e:
            self.retry_delays[user_id] = min(self.retry_delays[user_id] * 2 + 1, 30)
            await asyncio.sleep(e.retry_after)
            await self.handle_play_response(update, context)
            
        except Exception as e:
            self.logger.error(f"Error in handle_play_response: {str(e)}")
            if "Message to edit not found" not in str(e):
                self.error_count += 1
            
            if self.error_count >= 20:  # Increased threshold
                self.logger.error("Too many errors occurred. Closing play list.")
                self.play_open = False
                try:
                    await query.edit_message_text(
                        "Too many errors occurred. Play list has been closed. Please start a new list.",
                        reply_markup=None
                    )
                except Exception:
                    pass
    
    async def _process_user_action(self, query):
        """Process user action and return whether message needs updating"""
        user = query.from_user
        username = user.username or f"{user.first_name} {user.last_name or ''}".strip()
        
        if query.data == 'join_play':
            # Check rate limit for joining
            allowed, wait_time = await self.rate_limiter.acquire(user.id, "join")
            if not allowed:
                await query.answer(
                    f"Please wait {wait_time:.1f} seconds between join attempts",
                    show_alert=True
                )
                return False
                
            if any(p['username'] == username and not p.get('is_plus_one', False) for p in self.players):
                await query.answer(text=f"{username}, you're already on the list!", show_alert=True)
                return False
            
            if len(self.players) >= self.max_players:
                await query.answer(text="Play list is full!", show_alert=True)
                return False
            
            self.players.append({
                'username': username,
                'rating': 5,
                'is_plus_one': False
            })
            self.logger.info(f"{username} joined the play list")
            return True
            
        elif query.data == 'join_play_plus_one':
            # Check rate limit for joining
            allowed, wait_time = await self.rate_limiter.acquire(user.id, "join")
            if not allowed:
                await query.answer(
                    f"Please wait {wait_time:.1f} seconds between join attempts",
                    show_alert=True
                )
                return False
            
            if len(self.players) >= self.max_players:
                await query.answer(text="Play list is full!", show_alert=True)
                return False
            
            self.players.append({
                'username': username,
                'rating': 5,
                'is_plus_one': True
            })
            self.logger.info(f"{username} joined the play list as +1")
            return True
            
        elif query.data == 'cancel_join':
            # Check rate limit for leaving
            allowed, wait_time = await self.rate_limiter.acquire(user.id, "leave")
            if not allowed:
                await query.answer(
                    f"Please wait {wait_time:.1f} seconds between leave attempts",
                    show_alert=True
                )
                return False
            
            original_length = len(self.players)
            self.players = [p for p in self.players if p['username'] != username]
            message_changed = len(self.players) != original_length
            
            if message_changed:
                self.logger.info(f"{username} left the play list")
            else:
                self.logger.info(f"{username} attempted to leave but was not in the list")
            
            return message_changed
        
        return False
    
    async def _update_message_with_backoff(self, query, context):
        """Update message with exponential backoff retry logic"""
        user_id = query.from_user.id
        base_delay = self.retry_delays[user_id]
        max_retries = 3
        
        for attempt in range(max_retries):
            try:
                if len(self.players) >= self.max_players:
                    await self._handle_full_list(query, context)
                else:
                    await self._update_player_list(query)
                self.retry_delays[user_id] = max(0, base_delay - 1)
                break
            except RetryAfter as e:
                if attempt < max_retries - 1:
                    await asyncio.sleep(e.retry_after)
                    continue
                raise
    
    async def _handle_full_list(self, query, context):
        """Handle full player list and team creation"""
        self.play_open = False
        teams = self.create_balanced_teams()
        
        if len(teams[0]) != 6 or len(teams[1]) != 6:
            raise ValueError("Unable to create balanced teams")
        
        teams_message = self.format_teams_message(teams)
        self.last_team_message = teams_message
        
        await query.edit_message_text(
            "Play list is full! Teams have been created.",
            reply_markup=None
        )
        
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=teams_message
        )
    
    async def _update_player_list(self, query):
        """Update player list message with current state"""
        keyboard = [
            [
                InlineKeyboardButton("✅ In", callback_data='join_play'),
                InlineKeyboardButton("✅+1", callback_data='join_play_plus_one'),
                InlineKeyboardButton("❌ Out", callback_data='cancel_join')
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            text=self.format_player_list(),
            reply_markup=reply_markup
        )
    
    def create_balanced_teams(self):
        """Create two balanced teams with exactly 6 players each"""
        try:
            if len(self.players) != 12:
                self.logger.warning(f"Unexpected number of players: {len(self.players)}")
                return [self.players[:6], self.players[6:]]
            
            # Separate main players and +1 players
            main_players = [p for p in self.players if not p.get('is_plus_one', False)]
            plus_one_players = [p for p in self.players if p.get('is_plus_one', False)]
            
            # Sort main players by rating
            sorted_main_players = sorted(main_players, key=lambda x: x['rating'], reverse=True)
            
            # Initialize teams
            team_black = []
            team_white = []
            
            # Distribute main players in snake draft order
            for i, player in enumerate(sorted_main_players):
                if i % 2 == 0:
                    if len(team_black) < 6:
                        team_black.append(player)
                else:
                    if len(team_white) < 6:
                        team_white.append(player)
            
            # Distribute +1 players to balance teams
            random.shuffle(plus_one_players)
            for player in plus_one_players:
                if len(team_black) < 6:
                    team_black.append(player)
                elif len(team_white) < 6:
                    team_white.append(player)
            
            # Balance teams if needed
            while len(team_black) < 6 and len(team_white) > 6:
                team_black.append(team_white.pop())
            while len(team_white) < 6 and len(team_black) > 6:
                team_white.append(team_black.pop())
            
            self.logger.info(f"Team Black size: {len(team_black)}, Team White size: {len(team_white)}")
            
            return [team_black, team_white]
        
        except Exception as e:
            self.logger.error(f"Error in create_balanced_teams: {str(e)}")
            return [self.players[:6], self.players[6:]]
    
    async def cancel_play(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Cancel the current play signup with admin check and rate limiting"""
        try:
            user = update.effective_user
            self.logger.info(f"Cancel play attempt by {user.username}")
            
            # Check rate limit for canceling
            allowed, wait_time = await self.rate_limiter.acquire(user.id, "cancel_play")
            if not allowed:
                await update.message.reply_text(
                    f"Please wait {wait_time:.1f} seconds before canceling a play list."
                )
                return
            
            # Admin check for groups
            if update.message.chat.type in ['group', 'supergroup']:
                try:
                    chat_member = await update.message.chat.get_member(user.id)
                    if chat_member.status not in ['administrator', 'creator']:
                        self.logger.warning(f"Non-admin {user.username} attempted to cancel play")
                        await update.message.reply_text(
                            "❌ Sorry, only group administrators can cancel a play list."
                        )
                        return
                except Exception as e:
                    self.logger.error(f"Error checking admin status: {str(e)}")
                    await update.message.reply_text(
                        f"Error checking admin status: {str(e)}"
                    )
                    return
            
            # Reset play list
            async with self.update_lock:
                self.play_open = False
                self.players = []
                self.current_play_day = None
                self.error_count = 0
                
                self.logger.info("Play list cancelled")
                await update.message.reply_text("⛔️ Play cancelled for today.")
            
        except Exception as e:
            self.logger.error(f"Error in cancel_play: {str(e)}")
            await update.message.reply_text(
                "An error occurred while cancelling the play list. Please try again."
            )
    
    def run(self):
        """Start the Telegram bot"""
        try:
            self.logger.info("Starting Football Play Bot")
            
            app = Application.builder().token(self.token).build()
            
            # Register handlers
            app.add_handler(CommandHandler("play", self.start_play))
            app.add_handler(CommandHandler("cancel_play", self.cancel_play))
            app.add_handler(CallbackQueryHandler(self.handle_play_response))
            
            self.logger.info("Bot is running...")
            app.run_polling(allowed_updates=Update.ALL_TYPES)
            
        except Exception as e:
            self.logger.error(f"Error running bot: {str(e)}")
            raise

def main():
    try:
        # Retrieve token from environment variable
        token = os.getenv('TELEGRAM_BOT_TOKEN')
        
        if not token:
            print("Error: No Telegram Bot Token found. Please set TELEGRAM_BOT_TOKEN environment variable or in .env file.")
            sys.exit(1)
        
        # Initialize and start the bot
        bot = FootballPlayBot(token)
        print("Starting bot...")
        bot.logger.info("Bot initialized successfully")
        bot.run()
        
    except Exception as e:
        print(f"Critical error starting bot: {e}")
        sys.exit(1)

if __name__ == '__main__':
    main()