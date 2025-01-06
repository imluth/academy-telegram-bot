import os
import logging
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, filters
from dotenv import load_dotenv
import random

class FootballPlayBot:
    def __init__(self, token):
        self.token = token
        self.players = []
        self.max_players = 12
        self.play_open = False
        self.current_play_day = None
        self.last_team_message = None  # Track the last team message
        
        # Create logs directory if it doesn't exist
        self.logs_dir = 'logs'
        os.makedirs(self.logs_dir, exist_ok=True)
        
        # Configure logging
        self.setup_logging()
        
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
        """Set up logging with date-based log files"""
        # Create a logger
        self.logger = logging.getLogger('FootballPlayBot')
        self.logger.setLevel(logging.INFO)
        
        # Create a file handler with today's date
        today = datetime.now().strftime('%Y-%m-%d')
        log_file = os.path.join(self.logs_dir, f'{today}_football_play_bot.log')
        
        # Create file handler
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(logging.INFO)
        
        # Create console handler
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        
        # Create formatter and add it to the handlers
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        file_handler.setFormatter(formatter)
        console_handler.setFormatter(formatter)
        
        # Add the handlers to the logger
        self.logger.addHandler(file_handler)
        self.logger.addHandler(console_handler)
    
    def format_player_list(self):
        """Format the player list in the specified format"""
        if not self.current_play_day:
            return "No play day selected"
        
        details = self.play_details[self.current_play_day]
        
        # Create list lines
        list_lines = [
            f"**{details['day']} Play {details['time']} at {details['location']}**",
            "In List :"
        ]
        
        # Add players to the list, numbered
        for i in range(1, self.max_players + 1):
            if i <= len(self.players):
                player = self.players[i-1]
                # Display username with +1 tag if applicable
                player_display = player['username']
                if player.get('is_plus_one'):
                    player_display += " (+1)"
                list_lines.append(f"{i}. {player_display}")
            else:
                list_lines.append(f"{i}.")
        
        return "\n".join(list_lines)
    
    async def start_play(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Initialize a new play signup list with admin check"""
        # Log the start play attempt
        self.logger.info(f"Start play attempt by {update.effective_user.username}")
        
        # Check if the command is in a group
        if update.message.chat.type in ['group', 'supergroup']:
            # Get the user who sent the command
            user = update.effective_user
            
            try:
                # Get chat member info to check admin status
                chat_member = await update.message.chat.get_member(user.id)
                
                # Check if user is an admin or creator
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
        
        # If there's an active play list, warn user to cancel it first
        if self.play_open:
            self.logger.warning("Attempt to start play when a list is already in progress")
            await update.message.reply_text(
                "A play list is already in progress! Please use /cancel_play first before starting a new list."
            )
            return
        
        # Determine play day from command
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
        
        # Reset players and set play details
        self.players = []
        self.play_open = True
        self.current_play_day = play_day
        
        self.logger.info(f"Play list started for {play_day}")
        
        # Create inline keyboard
        keyboard = [
            [
                InlineKeyboardButton("✅ In", callback_data='join_play'),
                InlineKeyboardButton("✅+1", callback_data='join_play_plus_one'),
                InlineKeyboardButton("❌ Out", callback_data='cancel_join')
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # Send message with formatted list and inline keyboard
        message = await update.message.reply_text(
            self.format_player_list(),
            reply_markup=reply_markup
        )
    
    async def handle_play_response(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle inline keyboard responses for play signup"""
        query = update.callback_query
        await query.answer()
        
        if not self.play_open:
            # If no active list, show message to start new list
            self.logger.warning("No active play list")
            await query.edit_message_text("No active play list. Use /play Wed or /play Sat")
            return
        
        user = query.from_user
        username = user.username or f"{user.first_name} {user.last_name or ''}".strip()
        
        if query.data == 'join_play':
            # Check if user is already in the list (not as +1)
            if any(p['username'] == username and not p.get('is_plus_one', False) for p in self.players):
                self.logger.info(f"{username} already in the list")
                await query.answer(text=f"{username}, you're already on the list!", show_alert=True)
                return
            
            # Check if list is full
            if len(self.players) >= self.max_players:
                self.logger.warning("Attempt to join a full play list")
                await query.answer(text="Play list is full!", show_alert=True)
                return
            
            # Add user to the list
            self.players.append({
                'username': username, 
                'rating': 5,  # Default rating
                'is_plus_one': False
            })
            self.logger.info(f"{username} joined the play list")
        
        elif query.data == 'join_play_plus_one':
            # Check if list is full
            if len(self.players) >= self.max_players:
                self.logger.warning("Attempt to join a full play list as +1")
                await query.answer(text="Play list is full!", show_alert=True)
                return
            
            # Add user as +1
            self.players.append({
                'username': username, 
                'rating': 5,  # Default rating
                'is_plus_one': True
            })
            self.logger.info(f"{username} joined the play list as +1")
        
        elif query.data == 'cancel_join':
            # Remove user from the list (both regular and +1 entries)
            self.players = [p for p in self.players if p['username'] != username]
            self.logger.info(f"{username} left the play list")
        
        # Create inline keyboard
        keyboard = [
            [
                InlineKeyboardButton("✅ In", callback_data='join_play'),
                InlineKeyboardButton("✅+1", callback_data='join_play_plus_one'),
                InlineKeyboardButton("❌ Out", callback_data='cancel_join')
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # Check if list is full
        if len(self.players) >= self.max_players:
            self.play_open = False
            teams = self.create_balanced_teams()
            
            # Verify team sizes
            if len(teams[0]) != 6 or len(teams[1]) != 6:
                self.logger.error(f"Unbalanced teams created: Team Black: {len(teams[0])}, Team White: {len(teams[1])}")
                await query.edit_message_text("Error: Unable to create balanced teams. Please contact admin.")
                return
            
            # Store only the teams part in last_team_message
            teams_message = (
                f"Team Black ⚫️:\n" + "\n".join(
                    f"- {p['username']}{' (+1)' if p.get('is_plus_one') else ''}"
                    for p in teams[0]
                ) + "\n\n" +
                f"Team White ⚪️:\n" + "\n".join(
                    f"- {p['username']}{' (+1)' if p.get('is_plus_one') else ''}"
                    for p in teams[1]
                )
            )
            self.last_team_message = teams_message
            
            # Full response includes the "Play list is full!" prefix
            response = f"Play list is full! Teams have been created:\n\n{teams_message}"
            
            self.logger.info("Teams created: " + 
                            f"Team Black ({len(teams[0])} players): {[p['username'] for p in teams[0]]}, " +
                            f"Team White ({len(teams[1])} players): {[p['username'] for p in teams[1]]}")
            
            await query.edit_message_text(response)
        else:
            # Update message with formatted list
            await query.edit_message_text(
                self.format_player_list(),
                reply_markup=reply_markup
            )
    
    def create_balanced_teams(self):
        """
        Create two balanced teams with exactly 6 players each,
        ensuring fair distribution of both main and +1 players
        """
        # Ensure we have exactly 12 players
        if len(self.players) != 12:
            self.logger.warning(f"Unexpected number of players: {len(self.players)}")
            return [self.players[:6], self.players[6:]]
        
        # Separate main players and +1 players
        main_players = [p for p in self.players if not p.get('is_plus_one', False)]
        plus_one_players = [p for p in self.players if p.get('is_plus_one', False)]
        
        # Sort main players by rating in descending order
        sorted_main_players = sorted(main_players, key=lambda x: x['rating'], reverse=True)
        
        # Initialize teams
        team_black = []
        team_white = []
        
        # First, distribute main players in snake draft order
        for i, player in enumerate(sorted_main_players):
            if i % 2 == 0:
                if len(team_black) < 6:  # Check team black capacity
                    team_black.append(player)
            else:
                if len(team_white) < 6:  # Check team white capacity
                    team_white.append(player)
        
        # Then distribute +1 players to balance teams
        random.shuffle(plus_one_players)  # Randomize +1 players order
        for player in plus_one_players:
            if len(team_black) < 6:
                team_black.append(player)
            elif len(team_white) < 6:
                team_white.append(player)
        
        # Final check to ensure 6 players per team
        while len(team_black) < 6 and len(team_white) > 6:
            team_black.append(team_white.pop())
        while len(team_white) < 6 and len(team_black) > 6:
            team_white.append(team_black.pop())
        
        # Log team sizes for debugging
        self.logger.info(f"Team Black size: {len(team_black)}, Team White size: {len(team_white)}")
        
        return [team_black, team_white]
    
    async def cancel_play(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Cancel the current play signup with admin check"""
        # Log cancel play attempt
        self.logger.info(f"Cancel play attempt by {update.effective_user.username}")
        
        # Check if the command is in a group
        if update.message.chat.type in ['group', 'supergroup']:
            # Get the user who sent the command
            user = update.effective_user
            
            try:
                # Get chat member info to check admin status
                chat_member = await update.message.chat.get_member(user.id)
                
                # Check if user is an admin or creator
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
        self.play_open = False
        self.players = []
        self.current_play_day = None
        
        self.logger.info("Play list cancelled")
        
        # Simple cancellation message without showing last teams
        await update.message.reply_text("⛔️ Play cancelled for today.") 
    
    def run(self):
        """Start the Telegram bot"""
        # Log bot startup
        self.logger.info("Starting Football Play Bot")
        
        app = Application.builder().token(self.token).build()
        
        # Register handlers
        app.add_handler(CommandHandler("play", self.start_play))
        app.add_handler(CommandHandler("cancel_play", self.cancel_play))
        app.add_handler(CallbackQueryHandler(self.handle_play_response))
        
        # Start the bot
        app.run_polling(allowed_updates=Update.ALL_TYPES)


# Load environment variables from .env file
load_dotenv()

def main():
    # Retrieve token from environment variable
    TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
    
    # Add error handling
    if not TOKEN:
        raise ValueError("No Telegram Bot Token found. Please set TELEGRAM_BOT_TOKEN in .env file.")
    
    bot = FootballPlayBot(TOKEN)
    bot.run()

if __name__ == '__main__':
    main()