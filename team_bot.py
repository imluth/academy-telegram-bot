import os
import logging
from datetime import datetime, timedelta
from collections import defaultdict
import asyncio
import random
import sys
from typing import Dict, List, Optional, Tuple
import json
from dataclasses import dataclass, asdict
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery
from telegram.ext import (
    Application, 
    CommandHandler, 
    CallbackQueryHandler, 
    ContextTypes,
    filters,
    MessageHandler
)
from telegram.error import BadRequest, RetryAfter, TelegramError
from dotenv import load_dotenv
import aioredis
from aioredis.client import Redis
import backoff
import signal

# Load environment variables
load_dotenv()

# ================ Player Class ================
@dataclass
class Player:
    """Player data structure"""
    username: str
    user_id: int
    rating: float = 5.0
    is_plus_one: bool = False
    join_time: datetime = None

    def to_dict(self):
        return {
            'username': self.username,
            'user_id': self.user_id,
            'rating': self.rating,
            'is_plus_one': self.is_plus_one,
            'join_time': self.join_time.isoformat() if self.join_time else None
        }

    @classmethod
    def from_dict(cls, data):
        if data.get('join_time'):
            data['join_time'] = datetime.fromisoformat(data['join_time'])
        return cls(**data)

# ================ Redis Connection Class ================
class RedisConnection:
    """Redis connection manager with retry logic"""
    def __init__(self, url: str):
        self.url = url
        self._redis: Optional[Redis] = None
        self._lock = asyncio.Lock()
        self.logger = logging.getLogger('RedisConnection')

    @property
    def redis(self) -> Optional[Redis]:
        return self._redis

    @backoff.on_exception(
        backoff.expo,
        (aioredis.ConnectionError, aioredis.TimeoutError),
        max_tries=5
    )
    async def connect(self) -> None:
        """Connect to Redis with retry logic"""
        if self._redis is None:
            async with self._lock:
                if self._redis is None:  # Double-check pattern
                    try:
                        self._redis = await aioredis.from_url(
                            self.url,
                            encoding="utf-8",
                            decode_responses=True,
                            socket_timeout=5.0,
                            socket_connect_timeout=5.0
                        )
                        await self._redis.ping()  # Verify connection
                        self.logger.info("Successfully connected to Redis")
                    except Exception as e:
                        self.logger.error(f"Failed to connect to Redis: {e}")
                        raise

    async def get_redis(self) -> Redis:
        """Get Redis connection, establishing if necessary"""
        if self._redis is None:
            await self.connect()
        return self._redis

    async def close(self) -> None:
        """Close Redis connection"""
        if self._redis:
            await self._redis.close()
            self._redis = None

# ================ Rate Limiter Class ================
class RateLimiter:
    """Enhanced rate limiter with Redis backend"""
    def __init__(self, redis: Redis, rate_limit=3, per_seconds=1):
        self.redis = redis
        self.rate_limit = rate_limit
        self.per_seconds = per_seconds

    @backoff.on_exception(
        backoff.expo,
        (aioredis.ConnectionError, aioredis.TimeoutError),
        max_tries=3
    )
    async def acquire(self, user_id: int, action_type: str = "default") -> Tuple[bool, float]:
        """Check if a user can perform an action based on rate limits"""
        now = datetime.now().timestamp()
        key = f"rate_limit:{user_id}:{action_type}"
        cooldown_key = f"cooldown:{user_id}:{action_type}"

        try:
            if action_type in ["start_play", "cancel_play"]:
                cooldown = await self.redis.get(cooldown_key)
                if cooldown and float(cooldown) > now:
                    return False, float(cooldown) - now

            requests = await self.redis.zrangebyscore(
                key,
                now - self.per_seconds,
                now,
                withscores=True
            )

            if len(requests) >= self.rate_limit:
                oldest_req = float(requests[0][1])
                wait_time = self.per_seconds - (now - oldest_req)
                if wait_time > 0:
                    return False, wait_time

            pipeline = self.redis.pipeline()
            pipeline.zadd(key, {str(now): now})
            pipeline.expire(key, self.per_seconds * 2)

            if action_type in ["start_play", "cancel_play"]:
                cooldown_time = now + 5
                pipeline.set(cooldown_key, str(cooldown_time))
                pipeline.expire(cooldown_key, 10)

            await pipeline.execute()
            return True, 0
        except Exception as e:
            logging.error(f"Error in RateLimiter.acquire: {e}")
            return False, 1.0

# ================ Message Debouncer Class ================
class MessageDebouncer:
    """Enhanced message debouncer with Redis backend"""
    def __init__(self, redis: Redis, delay=0.5):
        self.redis = redis
        self.delay = delay
        self.logger = logging.getLogger('MessageDebouncer')

    @backoff.on_exception(
        backoff.expo,
        (aioredis.ConnectionError, aioredis.TimeoutError),
        max_tries=3
    )
    async def should_update(self, message_id: int) -> bool:
        try:
            now = datetime.now().timestamp()
            key = f"msg_update:{message_id}"
            
            last_update = await self.redis.get(key)
            if not last_update:
                await self.redis.set(key, str(now), ex=int(self.delay * 2))
                return True
            
            if now - float(last_update) < self.delay:
                return False
            
            await self.redis.set(key, str(now), ex=int(self.delay * 2))
            return True
        except Exception as e:
            self.logger.error(f"Error in should_update: {e}")
            return True

# ================ Play Session Class ================
class PlaySession:
    """Enhanced class to manage play session state"""
    def __init__(self, redis: Redis, chat_id: int):
        self.redis = redis
        self.chat_id = chat_id
        self.key_prefix = f"play_session:{chat_id}"
        self.logger = logging.getLogger('PlaySession')

    async def get_state(self) -> dict:
        try:
            state = await self.redis.get(f"{self.key_prefix}:state")
            return json.loads(state) if state else {}
        except Exception as e:
            self.logger.error(f"Error getting state: {e}")
            return {}

    async def set_state(self, state: dict):
        try:
            await self.redis.set(
                f"{self.key_prefix}:state",
                json.dumps(state),
                ex=86400
            )
        except Exception as e:
            self.logger.error(f"Error setting state: {e}")

    async def get_players(self) -> List[Player]:
        try:
            players_data = await self.redis.get(f"{self.key_prefix}:players")
            if not players_data:
                return []
            return [Player.from_dict(p) for p in json.loads(players_data)]
        except Exception as e:
            self.logger.error(f"Error getting players: {e}")
            return []

    async def set_players(self, players: List[Player]):
        try:
            players_data = json.dumps([p.to_dict() for p in players])
            await self.redis.set(
                f"{self.key_prefix}:players",
                players_data,
                ex=86400
            )
        except Exception as e:
            self.logger.error(f"Error setting players: {e}")

    async def is_open(self) -> bool:
        try:
            return bool(await self.redis.get(f"{self.key_prefix}:open"))
        except Exception as e:
            self.logger.error(f"Error checking if session is open: {e}")
            return False

    async def set_open(self, is_open: bool):
        try:
            if is_open:
                await self.redis.set(f"{self.key_prefix}:open", "1", ex=86400)
            else:
                await self.redis.delete(f"{self.key_prefix}:open")
        except Exception as e:
            self.logger.error(f"Error setting session open state: {e}")

    async def clear(self):
        try:
            await self.redis.delete(
                f"{self.key_prefix}:state",
                f"{self.key_prefix}:players",
                f"{self.key_prefix}:open"
            )
        except Exception as e:
            self.logger.error(f"Error clearing session: {e}")

# ================ Main Bot Class ================
class FootballPlayBot:
    def __init__(self, token: str, redis_url: str):
        self.token = token
        self.max_players = 12
        self.redis_url = redis_url
        self.redis_manager = RedisConnection(redis_url)
        self.retry_delays = defaultdict(int)
        
        self.setup_logging()
        
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
    """Set up logging configuration with correct timezone"""
    try:
        import pytz
        from datetime import datetime
        
        class TimezoneFormatter(logging.Formatter):
            def converter(self, timestamp):
                dt = datetime.fromtimestamp(timestamp)
                timezone = pytz.timezone('Asia/Male')
                return timezone.fromutc(dt.replace(tzinfo=pytz.UTC))

            def formatTime(self, record, datefmt=None):
                dt = self.converter(record.created)
                if datefmt:
                    return dt.strftime(datefmt)
                return dt.strftime('%Y-%m-%d %H:%M:%S')

        self.logger = logging.getLogger('FootballPlayBot')
        self.logger.setLevel(logging.INFO)
        
        formatter = TimezoneFormatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        
        # Console handler
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        self.logger.addHandler(console_handler)
        
        # File handler
        log_dir = os.getenv('LOG_DIR', 'logs')
        os.makedirs(log_dir, exist_ok=True)
        
        log_file = os.path.join(
            log_dir,
            f"{datetime.now(pytz.timezone('Asia/Male')).strftime('%Y-%m-%d')}_football_bot.log"
        )
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(formatter)
        self.logger.addHandler(file_handler)
            
    except Exception as e:
        print(f"Could not set up logging: {e}")
        # Set up basic logging as fallback
        logging.basicConfig(level=logging.INFO)
    
    async def initialize(self):
        """Initialize bot dependencies and connections"""
        try:
            # Initialize Redis connection
            await self.redis_manager.connect()
            redis = await self.redis_manager.get_redis()
            
            # Initialize rate limiter and message debouncer
            self.rate_limiter = RateLimiter(redis)
            self.message_debouncer = MessageDebouncer(redis)
            
            self.logger.info("Bot initialization completed successfully")
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to initialize bot: {e}", exc_info=True)
            return False

    async def shutdown(self, app, stop_event):
        """Graceful shutdown handler"""
        self.logger.info("Received shutdown signal...")
        if app:
            if app.updater and app.updater.running:
                await app.updater.stop()
            await app.stop()
            await app.shutdown()
        stop_event.set()

    async def cleanup(self, app=None):
        """Cleanup bot resources"""
        try:
            if app:
                self.logger.info("Stopping bot...")
                if app.updater and app.updater.running:
                    await app.updater.stop()
                await app.stop()
                await app.shutdown()
            
            await self.redis_manager.close()
            self.logger.info("Cleanup completed")
        except Exception as e:
            self.logger.error(f"Error during cleanup: {e}", exc_info=True)

    async def run(self):
        """Initialize and run the bot"""
        try:
            if not await self.initialize():
                self.logger.error("Failed to initialize bot")
                return

            app = Application.builder().token(self.token).build()
            
            # Add command handlers with correct method names
            app.add_handler(CommandHandler("play", self.handle_start_play))
            app.add_handler(CommandHandler("cancel_play", self.cancel_play))
            app.add_handler(CallbackQueryHandler(self.handle_play_response))
            app.add_error_handler(self.error_handler)
            
            await app.initialize()
            await app.start()
            await app.updater.start_polling(
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=True
            )
            
            self.logger.info("Bot started successfully")
            
            # Create stop event in the current loop
            stop_event = asyncio.Event()
            
            # Set up signal handlers
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(
                    sig,
                    lambda: asyncio.create_task(self.shutdown(app, stop_event))
                )
            
            # Wait for stop event
            await stop_event.wait()
            
        except Exception as e:
            self.logger.error(f"Critical error in run(): {e}", exc_info=True)
            raise
        finally:
            await self.cleanup(app if 'app' in locals() else None)

    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        """Handle errors in updates"""
        try:
            if update and isinstance(update, Update) and update.effective_message:
                await update.effective_message.reply_text(
                    "An error occurred. Please try again."
                )
            self.logger.error(f"Update {update} caused error {context.error}")
        except Exception as e:
            self.logger.error(f"Error in error handler: {e}", exc_info=True)

    def escape_markdown(self, text: str) -> str:
        """Escape Markdown special characters"""
        special_chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
        for char in special_chars:
            text = text.replace(char, f"\\{char}")
        return text

    def format_player_list(self, players: List[Player], play_day: str) -> str:
        """Format the player list with play details"""
        if not play_day or play_day not in self.play_details:
            return "No play day selected"
        
        details = self.play_details[play_day]
        
        # Escape special characters for MarkdownV2
        day = self.escape_markdown(details['day'])
        time = self.escape_markdown(details['time'])
        location = self.escape_markdown(details['location'])
        
        list_lines = [
            f"*{day} Play {time}*",
            f"{location}\n",
            "*Players List:*"
        ]
        
        for i, player in enumerate(players, 1):
            player_display = self.escape_markdown(player.username)
            if player.is_plus_one:
                player_display += " \\(\\+1\\)"
            list_lines.append(f"{i}\\. {player_display}")
        
        for i in range(len(players) + 1, self.max_players + 1):
            list_lines.append(f"{i}\\.")
        
        return "\n".join(list_lines)

    def format_teams_message(self, teams: List[List[Player]], play_day: str) -> str:
        """Format the teams message with play details"""
        if not play_day or play_day not in self.play_details:
            return "Error: Play day not set"

        details = self.play_details[play_day]
        
        # Escape special characters for MarkdownV2
        day = self.escape_markdown(details['day'])
        time = self.escape_markdown(details['time'])
        location = self.escape_markdown(details['location'])
        
        # Create team lists without f-strings for the escape sequences
        team_black = []
        team_white = []
        
        for p in teams[0]:
            player_name = self.escape_markdown(p.username)
            plus_one = " \\(\\+1\\)" if p.is_plus_one else ""
            team_black.append(f"\\- {player_name}{plus_one}")
            
        for p in teams[1]:
            player_name = self.escape_markdown(p.username)
            plus_one = " \\(\\+1\\)" if p.is_plus_one else ""
            team_white.append(f"\\- {player_name}{plus_one}")

        return (
            f"*{day} Play {time}*\n"
            f"{location}\n\n"
            f"*Team List:*\n"
            f"Team Black ⚫️:\n{chr(10).join(team_black)}\n\n"
            f"Team White ⚪️:\n{chr(10).join(team_white)}"
        )

    async def handle_start_play(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Initialize a new play signup with improved error handling"""
        try:
            user = update.effective_user
            chat_id = update.effective_chat.id
            
            # Check rate limit
            allowed, wait_time = await self.rate_limiter.acquire(
                user.id,
                "start_play"
            )
            if not allowed:
                self.logger.info(f"Rate limit hit for start_play - User: {user.username}, Chat: {chat_id}")
                await update.message.reply_text(
                    f"Please wait {wait_time:.1f} seconds before starting a new play list."
                )
                return

            # Admin check for groups
            if update.effective_chat.type in ['group', 'supergroup']:
                member = await context.bot.get_chat_member(chat_id, user.id)
                if member.status not in ['administrator', 'creator']:
                    self.logger.warning(f"Unauthorized play start attempt by {user.username} in chat {chat_id}")
                    await update.message.reply_text(
                        "❌ Only group administrators can start a play list."
                    )
                    return

            # Initialize session
            session = PlaySession(await self.redis_manager.get_redis(), chat_id)
            if await session.is_open():
                self.logger.info(f"Attempt to start play while session active by {user.username} in chat {chat_id}")
                await update.message.reply_text(
                    "A play list is already active! Use /cancel\\_play first."
                )
                return

            # Parse play day
            command_args = update.message.text.lower().split()
            if len(command_args) != 2 or command_args[1] not in ['wed', 'sat']:
                self.logger.info(f"Invalid play day format from {user.username} in chat {chat_id}: {update.message.text}")
                await update.message.reply_text(
                    "Please use:\n/play Wed\n/play Sat"
                )
                return

            play_day = command_args[1].capitalize()[:3]
            
            # Set up new session
            await session.set_open(True)
            await session.set_players([])
            await session.set_state({'play_day': play_day})

            # Create message
            keyboard = [
                [
                    InlineKeyboardButton("✅ In", callback_data='join_play'),
                   #InlineKeyboardButton("✅+1", callback_data='join_play_plus_one'),
                    InlineKeyboardButton("❌ Out", callback_data='cancel_join')
                ]
            ]
            
            try:
                await update.message.reply_text(
                    self.format_player_list([], play_day),
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode='MarkdownV2'
                )
                self.logger.info(
                    f"Play list started for {play_day} in chat {chat_id} by {user.username}"
                )
            except TelegramError as e:
                self.logger.error(f"Failed to send initial message: {e}")
                await session.set_open(False)
                await update.message.reply_text(
                    "Error starting play list\\. Please try again\\."
                )

        except Exception as e:
            self.logger.error(f"Error in handle_start_play: {e}", exc_info=True)
            await update.message.reply_text(
                "An error occurred\\. Please try again\\."
            )

    async def handle_play_response(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle player responses with improved error handling"""
        query = update.callback_query
        user = query.from_user
        chat_id = query.message.chat_id
        
        try:
            # Check rate limit
            allowed, wait_time = await self.rate_limiter.acquire(user.id)
            if not allowed:
                self.logger.info(f"Rate limit hit for user {user.username} (ID: {user.id}) in chat {chat_id}")
                await query.answer(
                    f"Please wait {wait_time:.1f} seconds.",
                    show_alert=True
                )
                return

            session = PlaySession(await self.redis_manager.get_redis(), chat_id)
            
            # Verify session is active
            if not await session.is_open():
                self.logger.info(f"Inactive session access attempt by {user.username} in chat {chat_id}")
                await query.answer("This play list is no longer active.", show_alert=True)
                try:
                    await query.edit_message_reply_markup(reply_markup=None)
                except TelegramError:
                    pass
                return

            # Get current state
            state = await session.get_state()
            players = await session.get_players()

            try:
                await query.answer()
            except TelegramError as e:
                if "Query is too old" in str(e):
                    return
                raise

            # Process action
            success = False
            action_type = query.data
            self.logger.info(f"User {user.username} attempting action '{action_type}' in chat {chat_id}")
            
            if action_type == 'join_play':
                success = await self._handle_join(session, players, user, False, query, context)
            elif action_type == 'join_play_plus_one':
                # TEMPORARY DISABLE START - Revert by removing these 3 lines and uncommenting the line below
                await query.answer("The +1 feature is temporarily disabled", show_alert=True)
                self.logger.info(f"Blocked +1 attempt by {user.username} in chat {chat_id}")
                return
                # TEMPORARY DISABLE END
                # success = await self._handle_join(session, players, user, True, query, context)
            elif action_type == 'cancel_join':
                success = await self._handle_leave(session, players, user, query)
            else:
                await query.answer("Invalid action")
                return

            if success:
                self.logger.info(f"Action '{action_type}' successful for user {user.username} in chat {chat_id}")

            # Update message if needed
            if await self.message_debouncer.should_update(query.message.message_id):
                await self._update_play_message(
                    context.bot,
                    chat_id,
                    query.message.message_id,
                    players,
                    state.get('play_day')
                )

        except RetryAfter as e:
            await asyncio.sleep(e.retry_after)
            await self.handle_play_response(update, context)
        except Exception as e:
            self.logger.error(f"Error in handle_play_response: {e}", exc_info=True)
            try:
                await query.answer(
                    "An error occurred. Please try again.",
                    show_alert=True
                )
            except TelegramError:
                pass

    async def _handle_join(self, session: PlaySession, players: List[Player],
                          user, is_plus_one: bool, query: CallbackQuery,
                          context: ContextTypes.DEFAULT_TYPE) -> bool:
        """Handle player join requests"""
        try:
            
            # TEMPORARY DISABLE START - Block +1 joins at the method level
            if is_plus_one:
                username = user.username or f"{user.first_name} {user.last_name or ''}".strip()
                self.logger.info(f"Blocked +1 join attempt by {username} in chat {session.chat_id}")
                await query.answer("The +1 feature is temporarily disabled", show_alert=True)
                return False
            # TEMPORARY DISABLE END
            
            if len(players) >= self.max_players:
                self.logger.info(f"Join attempt rejected - list full. User: {user.username}, Chat: {session.chat_id}")
                await query.answer("Play list is full!", show_alert=True)
                return False

            username = user.username or f"{user.first_name} {user.last_name or ''}".strip()
            
            # Check if already joined
            existing = next(
                (p for p in players if p.user_id == user.id and p.is_plus_one == is_plus_one),
                None
            )
            if existing:
                self.logger.info(f"Duplicate join attempt by {username} in chat {session.chat_id}")
                await query.answer("You're already on the list!", show_alert=True)
                return False

            # Add player
            new_player = Player(
                username=username,
                user_id=user.id,
                is_plus_one=is_plus_one,
                join_time=datetime.now()
            )
            players.append(new_player)
            
            # Log the join
            join_type = "+1" if is_plus_one else "regular"
            self.logger.info(f"Player {username} joined ({join_type}) - Total players: {len(players)} in chat {session.chat_id}")
            
            # Update state
            await session.set_players(players)
            
            # Check if list is full
            if len(players) >= self.max_players:
                await self._handle_full_list(session, players, query, context)
                return False
            
            return True
            
        except Exception as e:
            self.logger.error(f"Error in _handle_join: {e}", exc_info=True)
            return False

    async def _handle_leave(self, session: PlaySession, players: List[Player],
                          user, query: CallbackQuery) -> bool:
        """Handle player leave requests"""
        try:
            original_count = len(players)
            players = [p for p in players if p.user_id != user.id]
            
            if len(players) == original_count:
                self.logger.info(f"Leave attempt by non-listed player {user.username} in chat {session.chat_id}")
                await query.answer("You're not on the list!", show_alert=True)
                return False
            
            self.logger.info(f"Player {user.username} left - Players remaining: {len(players)} in chat {session.chat_id}")
            await session.set_players(players)
            return True
            
        except Exception as e:
            self.logger.error(f"Error in _handle_leave: {e}", exc_info=True)
            return False

    async def _handle_full_list(self, session: PlaySession, players: List[Player],
                               query: Optional[CallbackQuery] = None,
                               context: Optional[ContextTypes.DEFAULT_TYPE] = None):
        """Handle full player list and team creation"""
        try:
            self.logger.info(f"Player list full in chat {session.chat_id} - Creating teams...")
            
            state = await session.get_state()
            teams = self._create_balanced_teams(players)
            
            # Log team composition
            self.logger.info(f"Teams created for chat {session.chat_id}:")
            self.logger.info("Team Black: " + ", ".join(p.username for p in teams[0]))
            self.logger.info("Team White: " + ", ".join(p.username for p in teams[1]))
            
            # Close session
            await session.set_open(False)
            
            # Save final teams
            await session.set_state({
                **state,
                'teams': [
                    [p.to_dict() for p in team]
                    for team in teams
                ]
            })
            
            teams_message = self.format_teams_message(
                teams,
                state.get('play_day')
            )

            # First, update the inline keyboard message
            if query and context:
                try:
                    await query.edit_message_text(
                        "✅ Play list is full\\! Teams have been created\\.",
                        reply_markup=None,
                        parse_mode='MarkdownV2'
                    )
                except TelegramError as e:
                    self.logger.warning(f"Could not update message: {e}")

            # Then send the teams as a new message
            if context:
                await context.bot.send_message(
                    chat_id=session.chat_id,
                    text=teams_message,
                    parse_mode='MarkdownV2'
                )
                
            self.logger.info(f"Teams successfully announced in chat {session.chat_id}")
                
        except Exception as e:
            self.logger.error(f"Error in _handle_full_list: {e}", exc_info=True)

    def _create_balanced_teams(self, players: List[Player]) -> List[List[Player]]:
        """Create balanced teams with improved algorithm"""
        if len(players) != self.max_players:
            return [players[:6], players[6:]]

        try:
            # Separate regular and +1 players
            regular_players = [p for p in players if not p.is_plus_one]
            plus_one_players = [p for p in players if p.is_plus_one]
            
            # Sort by rating and join time
            regular_players.sort(
                key=lambda p: (-p.rating, p.join_time or datetime.max)
            )
            
            # Initialize teams
            team_black = []
            team_white = []
            
            # Distribute regular players in snake order
            for i, player in enumerate(regular_players):
                if i % 2 == 0:
                    team_black.append(player)
                else:
                    team_white.append(player)
            
            # Distribute +1 players
            random.shuffle(plus_one_players)
            remaining_slots_black = 6 - len(team_black)
            remaining_slots_white = 6 - len(team_white)
            
            team_black.extend(plus_one_players[:remaining_slots_black])
            team_white.extend(
                plus_one_players[
                    remaining_slots_black:
                    remaining_slots_black + remaining_slots_white
                ]
            )
            
            return [team_black, team_white]
            
        except Exception as e:
            self.logger.error(f"Error in _create_balanced_teams: {e}", exc_info=True)
            return [players[:6], players[6:]]  # Fallback to simple split

    async def _update_play_message(self, bot, chat_id: int, message_id: int,
                                 players: List[Player], play_day: str):
        """Update play list message"""
        try:
            keyboard = [
                [
                    InlineKeyboardButton("✅ In", callback_data='join_play'),
                   #InlineKeyboardButton("✅+1", callback_data='join_play_plus_one'),
                    InlineKeyboardButton("❌ Out", callback_data='cancel_join')
                ]
            ]
            
            message_text = self.format_player_list(players, play_day)
            
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=message_text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='MarkdownV2',
                disable_web_page_preview=True
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                self.logger.error(f"Error updating message: {e}")
                raise
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after)
            await self._update_play_message(
                bot, chat_id, message_id, players, play_day
            )
        except Exception as e:
            self.logger.error(f"Error in _update_play_message: {e}", exc_info=True)

    async def cancel_play(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Cancel current play session"""
        try:
            user = update.effective_user
            chat_id = update.effective_chat.id
            
            # Check rate limit
            allowed, wait_time = await self.rate_limiter.acquire(
                user.id,
                "cancel_play"
            )
            if not allowed:
                self.logger.info(f"Rate limit hit for cancel_play - User: {user.username}, Chat: {chat_id}")
                await update.message.reply_text(
                    f"Please wait {wait_time:.1f} seconds\\."
                )
                return

            # Admin check for groups
            if update.effective_chat.type in ['group', 'supergroup']:
                member = await context.bot.get_chat_member(chat_id, user.id)
                if member.status not in ['administrator', 'creator']:
                    self.logger.warning(f"Unauthorized cancel attempt by {user.username} in chat {chat_id}")
                    await update.message.reply_text(
                        "❌ Only administrators can cancel play lists\\."
                    )
                    return

            # Cancel session
            session = PlaySession(await self.redis_manager.get_redis(), chat_id)
            if not await session.is_open():
                self.logger.info(f"Cancel attempt on inactive session by {user.username} in chat {chat_id}")
                await update.message.reply_text(
                    "No active play list to cancel\\."
                )
                return

            await session.clear()  # Clear all session data
            self.logger.info(f"Play cancelled by {user.username} in chat {chat_id}")
            await update.message.reply_text(
                "⛔️ Play cancelled\\.",
                parse_mode='MarkdownV2'
            )
            
        except Exception as e:
            self.logger.error(f"Error in cancel_play: {e}", exc_info=True)
            await update.message.reply_text(
                "An error occurred while cancelling play\\."
            )


# ================ Main Function ================
def main():
    """Main function to run the bot with improved error handling"""
    try:
        # Get configuration
        token = os.getenv('TELEGRAM_BOT_TOKEN')
        redis_url = os.getenv('REDIS_URL', 'redis://localhost:6379')
        
        if not token:
            print("Error: TELEGRAM_BOT_TOKEN not found in environment")
            sys.exit(1)

        # Create bot
        bot = FootballPlayBot(token, redis_url)
        
        # Run bot in the event loop
        asyncio.run(bot.run())
        
    except KeyboardInterrupt:
        print("\nBot stopped by user")
        sys.exit(0)
    except Exception as e:
        print(f"Fatal error: {e}")
        sys.exit(1)

if __name__ == '__main__':
    main()