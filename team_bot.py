import os
import logging
from datetime import datetime, timedelta
from collections import defaultdict
import asyncio
import random
import sys
from typing import Dict, List, Optional, Tuple
import json
from dataclasses import dataclass, asdict, fields
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    CallbackQuery,
    ForceReply
)
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

# ================ Guest (+1) Settings ================
GUEST_LIMIT_PER_USER = 2       # how many named guests one member may bring
GUEST_PROMPT_TIMEOUT = 120     # seconds a +1 slot stays reserved while the name is typed
GUEST_NAME_MIN_LEN = 2
GUEST_NAME_MAX_LEN = 32

# ================ Player Class ================
@dataclass
class Player:
    """Player data structure.

    Guests brought via +1 are stored with user_id = 0 so that every lookup keyed on a
    Telegram user id skips them automatically. Ownership lives in added_by_id instead.
    """
    username: str
    user_id: int
    rating: float = 5.0
    is_plus_one: bool = False
    join_time: datetime = None
    guest_id: Optional[str] = None            # set for named guests only
    added_by_id: Optional[int] = None         # Telegram id of the member who brought them
    added_by_username: Optional[str] = None   # display name of that member

    def to_dict(self):
        return {
            'username': self.username,
            'user_id': self.user_id,
            'rating': self.rating,
            'is_plus_one': self.is_plus_one,
            'join_time': self.join_time.isoformat() if self.join_time else None,
            'guest_id': self.guest_id,
            'added_by_id': self.added_by_id,
            'added_by_username': self.added_by_username
        }

    @classmethod
    def from_dict(cls, data):
        # Copy first: records stored by older versions lack the guest fields, and unknown
        # keys are dropped so a future rollback cannot crash on deserialization.
        data = dict(data)
        if data.get('join_time'):
            data['join_time'] = datetime.fromisoformat(data['join_time'])
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

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

    # ---- generic JSON blob helpers (same one-key-per-concern style as above) ----

    async def _get_json(self, key: str) -> dict:
        try:
            raw = await self.redis.get(key)
            return json.loads(raw) if raw else {}
        except Exception as e:
            self.logger.error(f"Error reading {key}: {e}")
            return {}

    async def _set_json(self, key: str, value: dict):
        try:
            if value:
                await self.redis.set(key, json.dumps(value), ex=86400)
            else:
                await self.redis.delete(key)
        except Exception as e:
            self.logger.error(f"Error writing {key}: {e}")

    # ---- pending +1 name prompts (each one reserves a slot while the name is typed) ----

    async def get_pending_plus_ones(self) -> dict:
        """Live +1 reservations keyed by prompt message id. Expired entries are pruned here."""
        key = f"{self.key_prefix}:pending_plus_one"
        pending = await self._get_json(key)
        now = datetime.now().timestamp()
        active = {k: v for k, v in pending.items() if v.get('expires_at', 0) > now}
        if len(active) != len(pending):
            await self._set_json(key, active)
        return active

    async def add_pending_plus_one(self, prompt_message_id: int, user_id: int,
                                   username: str, expires_at: float):
        pending = await self.get_pending_plus_ones()
        pending[str(prompt_message_id)] = {
            'user_id': user_id,
            'username': username,
            'expires_at': expires_at
        }
        await self._set_json(f"{self.key_prefix}:pending_plus_one", pending)

    async def pop_pending_plus_one(self, prompt_message_id: int) -> Optional[dict]:
        """Remove and return a reservation, or None if it was already used or expired."""
        pending = await self.get_pending_plus_ones()
        entry = pending.pop(str(prompt_message_id), None)
        if entry is not None:
            await self._set_json(f"{self.key_prefix}:pending_plus_one", pending)
        return entry

    async def clear_pending_plus_ones(self):
        await self._set_json(f"{self.key_prefix}:pending_plus_one", {})

    # ---- pending removal menus ----

    async def _get_pending_removals(self) -> dict:
        key = f"{self.key_prefix}:pending_removal"
        pending = await self._get_json(key)
        now = datetime.now().timestamp()
        active = {k: v for k, v in pending.items() if v.get('expires_at', 0) > now}
        if len(active) != len(pending):
            await self._set_json(key, active)
        return active

    async def set_pending_removal(self, menu_message_id: int, user_id: int, expires_at: float):
        pending = await self._get_pending_removals()
        pending[str(menu_message_id)] = {'user_id': user_id, 'expires_at': expires_at}
        await self._set_json(f"{self.key_prefix}:pending_removal", pending)

    async def get_pending_removal(self, menu_message_id: int) -> Optional[dict]:
        return (await self._get_pending_removals()).get(str(menu_message_id))

    async def pop_pending_removal(self, menu_message_id: int) -> Optional[dict]:
        pending = await self._get_pending_removals()
        entry = pending.pop(str(menu_message_id), None)
        if entry is not None:
            await self._set_json(f"{self.key_prefix}:pending_removal", pending)
        return entry

    async def clear(self):
        try:
            await self.redis.delete(
                f"{self.key_prefix}:state",
                f"{self.key_prefix}:players",
                f"{self.key_prefix}:open",
                f"{self.key_prefix}:pending_plus_one",
                f"{self.key_prefix}:pending_removal"
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
        # Strong refs for fire-and-forget tasks; asyncio only holds weak ones
        self._background_tasks = set()

        self.setup_logging()
        
        self.play_details = self.load_play_config()

    def setup_logging(self):
        """Set up logging configuration with correct timezone"""
        try:
            import pytz
            from datetime import datetime
            
            class TimezoneFormatter(logging.Formatter):
                def __init__(self, fmt=None, datefmt=None, tz_name='Indian/Maldives'):
                    super().__init__(fmt, datefmt)
                    self.tz = pytz.timezone(tz_name)
                    
                def converter(self, timestamp):
                    dt = datetime.fromtimestamp(timestamp)
                    return dt.astimezone(self.tz)

                def formatTime(self, record, datefmt=None):
                    dt = self.converter(record.created)
                    if datefmt:
                        return dt.strftime(datefmt)
                    return dt.strftime('%Y-%m-%d %H:%M:%S')

            tz_name = 'Indian/Maldives'
            
            # Initialize logger
            self.logger = logging.getLogger('FootballPlayBot')
            self.logger.setLevel(logging.INFO)
            
            # Create formatter
            formatter = TimezoneFormatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                tz_name=tz_name
            )
            
            # Console handler
            console_handler = logging.StreamHandler()
            console_handler.setFormatter(formatter)
            self.logger.addHandler(console_handler)
            
            # File handler
            log_dir = os.getenv('LOG_DIR', 'logs')
            os.makedirs(log_dir, exist_ok=True)
            
            local_now = datetime.now(pytz.timezone(tz_name))
            log_file = os.path.join(
                log_dir,
                f"{local_now.strftime('%Y-%m-%d')}_football_bot.log"
            )
            file_handler = logging.FileHandler(log_file)
            file_handler.setFormatter(formatter)
            self.logger.addHandler(file_handler)
                
        except Exception as e:
            print(f"Could not set up logging: {e}")
            # Set up basic logging as fallback
            logging.basicConfig(level=logging.INFO)
            self.logger = logging.getLogger('FootballPlayBot')
    
    def load_play_config(self) -> dict:
        """Load play schedule configuration from JSON file with fallback to defaults"""
        default_config = {
            'Sat': {'day': 'Saturday Night', 'time': '10pm to 11pm', 'location': 'Teenage Ground'},
            'Wed': {'day': 'Wednesday Night', 'time': '11pm to 12am', 'location': 'Teenage Ground'}
        }
        valid_keys = {'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'}
        required_fields = {'day', 'time', 'location'}

        config_paths = ['/app/play_config.json', 'play_config.json']

        for path in config_paths:
            if os.path.exists(path):
                try:
                    with open(path, 'r') as f:
                        config = json.load(f)

                    if not isinstance(config, dict):
                        self.logger.warning(f"Play config at {path} is not a dict. Using defaults.")
                        return default_config

                    for key, value in config.items():
                        if key not in valid_keys:
                            self.logger.warning(f"Invalid day key '{key}' in play config. Using defaults.")
                            return default_config
                        if not isinstance(value, dict):
                            self.logger.warning(f"Config entry for '{key}' is not a dict. Using defaults.")
                            return default_config
                        if set(value.keys()) != required_fields:
                            self.logger.warning(f"Config entry for '{key}' missing required fields. Using defaults.")
                            return default_config
                        if not all(isinstance(v, str) for v in value.values()):
                            self.logger.warning(f"Config entry for '{key}' has non-string values. Using defaults.")
                            return default_config

                    enabled_days = ', '.join(config.keys())
                    self.logger.info(f"Play config loaded. Enabled days: {enabled_days}")
                    return config

                except json.JSONDecodeError as e:
                    self.logger.warning(f"Invalid JSON in play config at {path}: {e}. Using defaults.")
                    return default_config
                except Exception as e:
                    self.logger.warning(f"Error reading play config at {path}: {e}. Using defaults.")
                    return default_config

        self.logger.warning("No play_config.json found. Using defaults.")
        return default_config

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
            app.add_handler(CallbackQueryHandler(
                self.handle_play_response,
                pattern="^(join_play|join_play_plus_one|cancel_join)$"
            ))
            app.add_handler(CallbackQueryHandler(
                self.handle_removal_choice,
                pattern="^rm_"
            ))
            # Guest names arrive as replies to the bot's ForceReply prompt. Replies to the
            # bot's own messages reach the handler even with group privacy mode enabled.
            app.add_handler(MessageHandler(
                filters.REPLY & filters.TEXT & ~filters.COMMAND,
                self.handle_guest_name_reply
            ))
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

    def _spawn(self, coro):
        """Run a coroutine in the background, keeping a strong reference to the task"""
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    def _display_name(self, user) -> str:
        """Plain display name for a Telegram user"""
        return user.username or f"{user.first_name} {user.last_name or ''}".strip()

    def _mention(self, user) -> str:
        """Mention for plain-text (unparsed) messages"""
        return f"@{user.username}" if user.username else self._display_name(user)

    def _mention_md(self, user) -> str:
        """MarkdownV2 inline mention, works whether or not the user has a @username.

        ForceReply(selective=True) targets users mentioned in the message, so the prompt
        must carry a real mention entity for the reply box to open automatically.
        """
        return f"[{self.escape_markdown(self._display_name(user))}](tg://user?id={user.id})"

    async def _delete_message(self, bot, chat_id: int, message_id: int):
        """Best-effort delete - the bot may not be an admin with delete rights"""
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except TelegramError as e:
            self.logger.debug(f"Could not delete message {message_id} in {chat_id}: {e}")

    def _new_guest_id(self, players: List[Player]) -> str:
        """Short id used to target a specific guest from a removal button"""
        used = {p.guest_id for p in players if p.guest_id}
        while True:
            candidate = f"{random.randrange(16 ** 6):06x}"
            if candidate not in used:
                return candidate

    def _guests_of(self, players: List[Player], user_id: int) -> List[Player]:
        return [p for p in players if p.is_plus_one and p.added_by_id == user_id]

    def _no_slot_message(self, player_count: int) -> str:
        """Tell the difference between a truly full list and one held by a pending +1"""
        if player_count < self.max_players:
            return "The last slot is being filled with a +1 name. Try again in a moment."
        return "Play list is full!"

    def _validate_guest_name(self, raw: str, players: List[Player]):
        """Normalise and check a typed guest name.

        Returns (name, error) - exactly one of the two is None.
        """
        name = " ".join((raw or "").split())

        if len(name) < GUEST_NAME_MIN_LEN:
            return None, f"That name is too short. Please use at least {GUEST_NAME_MIN_LEN} characters."
        if len(name) > GUEST_NAME_MAX_LEN:
            return None, f"That name is too long. Please keep it under {GUEST_NAME_MAX_LEN} characters."
        if not any(c.isalnum() for c in name):
            return None, "That name needs at least one letter or number."
        if any(p.username.casefold() == name.casefold() for p in players):
            return None, f"{name} is already on the list. Please use a different name."

        return name, None

    def _format_player_line(self, player: Player) -> str:
        """Render one player entry for MarkdownV2 output"""
        display = self.escape_markdown(player.username)
        if player.is_plus_one:
            if player.added_by_username:
                display += f" \\(\\+1 by @{self.escape_markdown(player.added_by_username)}\\)"
            else:
                # Unnamed +1 stored by an earlier version of the bot
                display += " \\(\\+1\\)"
        return display

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
            list_lines.append(f"{i}\\. {self._format_player_line(player)}")

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
        
        team_black = [f"\\- {self._format_player_line(p)}" for p in teams[0]]
        team_white = [f"\\- {self._format_player_line(p)}" for p in teams[1]]

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
            valid_days = [d.lower() for d in self.play_details.keys()]
            if len(command_args) != 2 or command_args[1] not in valid_days:
                self.logger.info(f"Invalid play day format from {user.username} in chat {chat_id}: {update.message.text}")
                days_list = "\n".join(f"/play {d}" for d in self.play_details.keys())
                await update.message.reply_text(
                    f"Please use:\n{days_list}"
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
                    InlineKeyboardButton("✅+1", callback_data='join_play_plus_one'),
                    InlineKeyboardButton("❌ Out", callback_data='cancel_join')
                ]
            ]
            
            try:
                sent = await update.message.reply_text(
                    self.format_player_list([], play_day),
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode='MarkdownV2'
                )
                # Remember the list message so flows without a callback query (guest name
                # replies, removal menus) can still re-render it.
                await session.set_state({
                    'play_day': play_day,
                    'list_message_id': sent.message_id
                })
                self.logger.info(
                    f"Play list started for {play_day} in chat {chat_id} by {user.username}"
                )
            except TelegramError as e:
                self.logger.error(f"Failed to send initial message: {e}")
                await session.clear()
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

            # Sessions started before this version have no stored list message id. Backfill
            # it here so the guest-name and removal flows can re-render the list.
            if not state.get('list_message_id'):
                state['list_message_id'] = query.message.message_id
                await session.set_state(state)

            # Process action. Handlers return True only when the player list changed and
            # the list message needs re-rendering.
            action_type = query.data
            self.logger.info(f"User {user.username} attempting action '{action_type}' in chat {chat_id}")

            if action_type == 'join_play':
                needs_update = await self._handle_join(session, players, user, query, context)
            elif action_type == 'join_play_plus_one':
                needs_update = await self._handle_plus_one_request(session, players, user, query, context)
            elif action_type == 'cancel_join':
                needs_update = await self._handle_leave(session, players, user, query, context)
            else:
                await query.answer("Invalid action", show_alert=True)
                return

            if needs_update:
                self.logger.info(f"Action '{action_type}' successful for user {user.username} in chat {chat_id}")

            # Skip the re-render once the session has closed, otherwise it would overwrite
            # the "list is full" message and put the join keyboard back.
            if needs_update and await session.is_open():
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

    async def _send_confirmation(self, context: ContextTypes.DEFAULT_TYPE,
                                chat_id: int, text: str):
        """Post a plain-text confirmation to the group (no parse_mode, so no escaping)"""
        try:
            await context.bot.send_message(chat_id=chat_id, text=text)
        except TelegramError as e:
            self.logger.error(f"Failed to send confirmation message: {e}")

    async def _handle_join(self, session: PlaySession, players: List[Player],
                          user, query: CallbackQuery,
                          context: ContextTypes.DEFAULT_TYPE) -> bool:
        """Handle a regular join request"""
        try:
            # Slots held by in-flight +1 name prompts count as taken
            reserved = len(await session.get_pending_plus_ones())
            if len(players) + reserved >= self.max_players:
                self.logger.info(f"Join attempt rejected - no free slot. User: {user.username}, Chat: {session.chat_id}")
                await query.answer(self._no_slot_message(len(players)), show_alert=True)
                return False

            username = self._display_name(user)

            if any(p.user_id == user.id for p in players):
                self.logger.info(f"Duplicate join attempt by {username} in chat {session.chat_id}")
                await query.answer("You're already on the list!", show_alert=True)
                return False

            players.append(Player(
                username=username,
                user_id=user.id,
                join_time=datetime.now()
            ))
            await session.set_players(players)

            self.logger.info(
                f"Player {username} joined - Total players: {len(players)} in chat {session.chat_id}"
            )

            await query.answer()
            await self._send_confirmation(
                context, session.chat_id,
                f"User {self._mention(user)} added to in list ✅"
            )

            if len(players) >= self.max_players:
                await self._handle_full_list(session, players, context)
                return False

            return True

        except Exception as e:
            self.logger.error(f"Error in _handle_join: {e}", exc_info=True)
            return False

    async def _handle_plus_one_request(self, session: PlaySession, players: List[Player],
                                      user, query: CallbackQuery,
                                      context: ContextTypes.DEFAULT_TYPE) -> bool:
        """Ask the member for the guest's name and reserve a slot while they type.

        Telegram has no dialog box for bots, so this posts a ForceReply prompt: the reply
        box opens for the mentioned member, and their reply comes back to
        handle_guest_name_reply. Always returns False - the player list is unchanged here.
        """
        try:
            pending = await session.get_pending_plus_ones()

            if any(entry['user_id'] == user.id for entry in pending.values()):
                await query.answer(
                    "You already have a name request open. Please reply to it first.",
                    show_alert=True
                )
                return False

            owned = len(self._guests_of(players, user.id))
            if owned >= GUEST_LIMIT_PER_USER:
                await query.answer(
                    f"You can only bring {GUEST_LIMIT_PER_USER} guests.",
                    show_alert=True
                )
                return False

            if len(players) + len(pending) >= self.max_players:
                await query.answer(self._no_slot_message(len(players)), show_alert=True)
                return False

            await query.answer()

            prompt = await context.bot.send_message(
                chat_id=session.chat_id,
                text=(
                    f"{self._mention_md(user)}, reply to this message with the name of the "
                    f"player you are bringing\\.\n"
                    f"Your slot is held for {GUEST_PROMPT_TIMEOUT // 60} minutes\\. "
                    f"Reply *cancel* to drop it\\."
                ),
                reply_markup=ForceReply(
                    selective=True,
                    input_field_placeholder="Guest player name"
                ),
                parse_mode='MarkdownV2'
            )

            expires_at = datetime.now().timestamp() + GUEST_PROMPT_TIMEOUT
            await session.add_pending_plus_one(
                prompt.message_id, user.id, self._display_name(user), expires_at
            )
            self._spawn(self._expire_plus_one_prompt(
                session.chat_id, prompt.message_id, context, GUEST_PROMPT_TIMEOUT
            ))

            self.logger.info(
                f"+1 name prompt {prompt.message_id} opened by {user.username} in chat {session.chat_id}"
            )
            return False

        except Exception as e:
            self.logger.error(f"Error in _handle_plus_one_request: {e}", exc_info=True)
            try:
                await query.answer("Could not start the +1 request. Please try again.", show_alert=True)
            except TelegramError:
                pass
            return False

    async def _expire_plus_one_prompt(self, chat_id: int, prompt_message_id: int,
                                     context: ContextTypes.DEFAULT_TYPE, delay: float):
        """Release a reserved slot whose prompt was never answered.

        Best-effort only: PTB is installed without the job-queue extra, and a container
        restart drops these tasks. The expires_at stamp in Redis remains authoritative and
        is pruned lazily on the next read.
        """
        try:
            await asyncio.sleep(delay)
            session = PlaySession(await self.redis_manager.get_redis(), chat_id)
            if await session.pop_pending_plus_one(prompt_message_id) is None:
                return  # already answered or cancelled
            await self._delete_message(context.bot, chat_id, prompt_message_id)
            self.logger.info(f"+1 prompt {prompt_message_id} expired in chat {chat_id}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.logger.error(f"Error expiring +1 prompt: {e}", exc_info=True)

    async def _reissue_guest_prompt(self, session: PlaySession, user, context: ContextTypes.DEFAULT_TYPE,
                                   old_prompt_id: int, reason: str, expires_at: float) -> bool:
        """Replace a prompt after a rejected name so the reply box opens again.

        The original expiry is carried over, so retries cannot extend the held slot.
        """
        try:
            remaining = expires_at - datetime.now().timestamp()
            if remaining <= 0:
                await session.pop_pending_plus_one(old_prompt_id)
                await self._delete_message(context.bot, session.chat_id, old_prompt_id)
                return False

            prompt = await context.bot.send_message(
                chat_id=session.chat_id,
                text=(
                    f"{self._mention_md(user)}, {self.escape_markdown(reason)}\n"
                    f"Reply to this message with the guest's name, or *cancel* to drop the slot\\."
                ),
                reply_markup=ForceReply(
                    selective=True,
                    input_field_placeholder="Guest player name"
                ),
                parse_mode='MarkdownV2'
            )

            await session.pop_pending_plus_one(old_prompt_id)
            await self._delete_message(context.bot, session.chat_id, old_prompt_id)
            await session.add_pending_plus_one(
                prompt.message_id, user.id, self._display_name(user), expires_at
            )
            self._spawn(self._expire_plus_one_prompt(
                session.chat_id, prompt.message_id, context, remaining
            ))
            return True

        except Exception as e:
            self.logger.error(f"Error reissuing guest prompt: {e}", exc_info=True)
            return False

    async def handle_guest_name_reply(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Capture the guest name typed in reply to a +1 prompt"""
        message = update.message
        if not message or not message.reply_to_message:
            return

        chat_id = message.chat_id
        user = update.effective_user
        prompt_id = message.reply_to_message.message_id

        try:
            session = PlaySession(await self.redis_manager.get_redis(), chat_id)
            entry = (await session.get_pending_plus_ones()).get(str(prompt_id))

            # Any other reply in the group is none of our business
            if entry is None:
                return

            # Throttle before answering, so replying to someone else's prompt is rate limited
            allowed, wait_time = await self.rate_limiter.acquire(user.id, "guest_name")
            if not allowed:
                await message.reply_text(f"Please wait {wait_time:.1f} seconds.")
                return

            if entry['user_id'] != user.id:
                await message.reply_text("That name request isn't yours.")
                return

            if not await session.is_open():
                await session.pop_pending_plus_one(prompt_id)
                await self._delete_message(context.bot, chat_id, prompt_id)
                await message.reply_text("This play list is no longer active.")
                return

            # Explicit cancellation
            if (message.text or "").strip().casefold() == "cancel":
                await session.pop_pending_plus_one(prompt_id)
                await self._delete_message(context.bot, chat_id, prompt_id)
                await self._delete_message(context.bot, chat_id, message.message_id)
                await self._send_confirmation(
                    context, chat_id,
                    f"User {self._mention(user)} cancelled the +1 request."
                )
                self.logger.info(f"+1 request cancelled by {user.username} in chat {chat_id}")
                return

            players = await session.get_players()
            name, error = self._validate_guest_name(message.text, players)

            if error:
                await self._delete_message(context.bot, chat_id, message.message_id)
                reissued = await self._reissue_guest_prompt(
                    session, user, context, prompt_id, error, entry['expires_at']
                )
                if not reissued:
                    await self._send_confirmation(
                        context, chat_id,
                        f"{self._mention(user)}: {error} Your +1 slot was released - tap ✅+1 to try again."
                    )
                return

            # Re-check capacity, ignoring the slot this request is holding
            others_pending = sum(
                1 for key in (await session.get_pending_plus_ones()) if key != str(prompt_id)
            )
            if len(players) + others_pending >= self.max_players:
                await session.pop_pending_plus_one(prompt_id)
                await self._delete_message(context.bot, chat_id, prompt_id)
                await message.reply_text("Sorry, the play list filled up.")
                return

            players.append(Player(
                username=name,
                user_id=0,
                is_plus_one=True,
                join_time=datetime.now(),
                guest_id=self._new_guest_id(players),
                added_by_id=user.id,
                added_by_username=self._display_name(user)
            ))
            await session.set_players(players)
            await session.pop_pending_plus_one(prompt_id)

            await self._delete_message(context.bot, chat_id, prompt_id)
            await self._delete_message(context.bot, chat_id, message.message_id)

            self.logger.info(
                f"Guest '{name}' added by {user.username} - Total players: {len(players)} in chat {chat_id}"
            )
            await self._send_confirmation(
                context, chat_id,
                f"User {self._mention(user)} added {name} to in list as +1 ✅"
            )

            if len(players) >= self.max_players:
                await self._handle_full_list(session, players, context)
            else:
                await self._refresh_list_message(session, players, context)

        except Exception as e:
            self.logger.error(f"Error in handle_guest_name_reply: {e}", exc_info=True)
            try:
                await message.reply_text("An error occurred. Please try again.")
            except TelegramError:
                pass

    async def _discard_open_prompts(self, session: PlaySession,
                                   context: ContextTypes.DEFAULT_TYPE):
        """Remove ForceReply prompts left hanging when a session ends"""
        for prompt_id in await session.get_pending_plus_ones():
            try:
                await self._delete_message(context.bot, session.chat_id, int(prompt_id))
            except (TypeError, ValueError):
                continue

    async def _refresh_list_message(self, session: PlaySession, players: List[Player],
                                   context: ContextTypes.DEFAULT_TYPE):
        """Re-render the play list message from stored state (no callback query available)"""
        state = await session.get_state()
        list_message_id = state.get('list_message_id')
        if not list_message_id:
            return
        await self._update_play_message(
            context.bot, session.chat_id, list_message_id, players, state.get('play_day')
        )

    async def _handle_leave(self, session: PlaySession, players: List[Player],
                          user, query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE) -> bool:
        """Handle leave requests, asking what to remove when the member brought guests"""
        try:
            on_list = any(p.user_id == user.id for p in players)
            guests = self._guests_of(players, user.id)

            if not on_list and not guests:
                self.logger.info(f"Leave attempt by non-listed player {user.username} in chat {session.chat_id}")
                await query.answer("You're not on the list!", show_alert=True)
                return False

            if guests:
                await query.answer()
                await self._send_removal_menu(session, user, on_list, guests, context)
                return False

            await query.answer()

            # Mutate in place so the caller re-renders the updated list
            players[:] = [p for p in players if p.user_id != user.id]
            await session.set_players(players)

            self.logger.info(
                f"Player {user.username} left - Players remaining: {len(players)} in chat {session.chat_id}"
            )
            await self._send_confirmation(
                context, session.chat_id,
                f"User {self._mention(user)} removed from in list ❌"
            )
            return True

        except Exception as e:
            self.logger.error(f"Error in _handle_leave: {e}", exc_info=True)
            return False

    async def _send_removal_menu(self, session: PlaySession, user, on_list: bool,
                                guests: List[Player], context: ContextTypes.DEFAULT_TYPE):
        """Offer the member a choice of what to remove. Options match what they actually own."""
        try:
            buttons = []
            if on_list:
                buttons.append([InlineKeyboardButton("Just me", callback_data='rm_self')])
            for guest in guests:
                label = guest.username if len(guest.username) <= 20 else f"{guest.username[:19]}…"
                buttons.append([InlineKeyboardButton(label, callback_data=f"rm_guest:{guest.guest_id}")])
            if on_list or len(guests) > 1:
                buttons.append([InlineKeyboardButton("Everyone", callback_data='rm_all')])
            buttons.append([InlineKeyboardButton("✖️ Keep everyone", callback_data='rm_cancel')])

            menu = await context.bot.send_message(
                chat_id=session.chat_id,
                text=f"{self._mention_md(user)}, who should be removed?",
                reply_markup=InlineKeyboardMarkup(buttons),
                parse_mode='MarkdownV2'
            )

            # Ownership is stored server-side so nobody else can act on this menu
            await session.set_pending_removal(
                menu.message_id, user.id, datetime.now().timestamp() + 300
            )

        except Exception as e:
            self.logger.error(f"Error in _send_removal_menu: {e}", exc_info=True)

    async def handle_removal_choice(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Apply the member's choice from the removal menu"""
        query = update.callback_query
        user = query.from_user
        chat_id = query.message.chat_id
        menu_id = query.message.message_id

        try:
            session = PlaySession(await self.redis_manager.get_redis(), chat_id)
            entry = await session.get_pending_removal(menu_id)

            if entry is None:
                await query.answer("This menu has expired.", show_alert=True)
                await self._delete_message(context.bot, chat_id, menu_id)
                return

            if entry['user_id'] != user.id:
                await query.answer("This menu isn't yours.", show_alert=True)
                return

            allowed, wait_time = await self.rate_limiter.acquire(user.id)
            if not allowed:
                await query.answer(f"Please wait {wait_time:.1f} seconds.", show_alert=True)
                return

            await query.answer()
            await session.pop_pending_removal(menu_id)
            await self._delete_message(context.bot, chat_id, menu_id)

            choice = query.data
            if choice == 'rm_cancel':
                return

            if not await session.is_open():
                await self._send_confirmation(context, chat_id, "This play list is no longer active.")
                return

            players = await session.get_players()

            if choice == 'rm_self':
                def targeted(p):
                    return p.user_id == user.id
            elif choice == 'rm_all':
                def targeted(p):
                    return p.user_id == user.id or (p.is_plus_one and p.added_by_id == user.id)
            elif choice.startswith('rm_guest:'):
                guest_id = choice.split(':', 1)[1]
                def targeted(p):
                    return p.guest_id == guest_id and p.added_by_id == user.id
            else:
                return

            removed = [p.username for p in players if targeted(p)]
            if not removed:
                await self._send_confirmation(
                    context, chat_id, "Nothing to remove - the list already changed."
                )
                return

            players = [p for p in players if not targeted(p)]
            await session.set_players(players)

            self.logger.info(
                f"{user.username} removed {removed} - Players remaining: {len(players)} in chat {chat_id}"
            )
            await self._send_confirmation(
                context, chat_id,
                f"Removed from in list ❌: {', '.join(removed)}"
            )
            await self._refresh_list_message(session, players, context)

        except Exception as e:
            self.logger.error(f"Error in handle_removal_choice: {e}", exc_info=True)
            try:
                await query.answer("An error occurred. Please try again.", show_alert=True)
            except TelegramError:
                pass

    async def _handle_full_list(self, session: PlaySession, players: List[Player],
                               context: Optional[ContextTypes.DEFAULT_TYPE] = None):
        """Handle full player list"""
        try:
            self.logger.info(f"Player list full in chat {session.chat_id} - List complete with {len(players)} players")

            state = await session.get_state()
            play_day = state.get('play_day')
            list_message_id = state.get('list_message_id')

            # Close session and drop any slot still reserved by an unanswered +1 prompt
            await session.set_open(False)
            if context:
                await self._discard_open_prompts(session, context)
            await session.clear_pending_plus_ones()

            # Create final player list message
            if play_day and play_day in self.play_details:
                details = self.play_details[play_day]

                # Escape special characters for MarkdownV2
                day = self.escape_markdown(details['day'])
                time = self.escape_markdown(details['time'])
                location = self.escape_markdown(details['location'])

                # Build player list
                list_lines = [
                    f"*{day} Play {time}*",
                    f"{location}\n",
                    f"*Players \\({len(players)}\\):*"
                ]

                for i, player in enumerate(players, 1):
                    list_lines.append(f"{i}\\. {self._format_player_line(player)}")

                list_lines.append("\n✅ Play list is full\\! Team generation in progress\\.\\.\\.")

                final_message = "\n".join(list_lines)
            else:
                final_message = "✅ Play list is full\\! Team generation in progress\\.\\.\\."

            # Close out the list message and strip its keyboard
            if context and list_message_id:
                try:
                    await context.bot.edit_message_text(
                        chat_id=session.chat_id,
                        message_id=list_message_id,
                        text="✅ Play list is full\\!",
                        reply_markup=None,
                        parse_mode='MarkdownV2'
                    )
                except TelegramError as e:
                    self.logger.warning(f"Could not update message: {e}")

            # Send the final player list as a new message
            if context:
                await context.bot.send_message(
                    chat_id=session.chat_id,
                    text=final_message,
                    parse_mode='MarkdownV2'
                )

            self.logger.info(f"Player list completed and announced in chat {session.chat_id}")

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
                    InlineKeyboardButton("✅+1", callback_data='join_play_plus_one'),
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

            await self._discard_open_prompts(session, context)
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
