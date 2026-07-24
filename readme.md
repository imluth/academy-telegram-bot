# Football Play Management Telegram Bot

A robust Telegram bot designed to manage football game scheduling, team organization, and player management. Built with Python 3.9 and Redis, this bot helps coordinate regular football matches with automated team formation and comprehensive player management.

## Features

### Play Management
- **Schedule Management**
  - Supports two regular play schedules:
    - Saturday Night (10pm to 11pm)
    - Wednesday Night (11pm to 12am)
  - Location tracking and display ("Teenage Ground")
  - Easy game initiation with `/play sat` or `/play wed` command

### Player Management
- Maximum 12 players per game
- Player registration options:
  - Regular join (✅ In)
  - Bring a named guest (✅+1)
  - Leave game (❌ Out)
- Real-time player list updates
- Automatic team formation when full

### Named Guests (+1)
The `+1` button is for bringing a player who isn't in the Telegram group. Because Telegram
bots cannot open a dialog box, the bot posts a `ForceReply` prompt instead:

1. Tap `✅+1` — the bot asks for a name and opens the reply box for you only
2. Type the guest's name and send
3. The list shows `2. Ahmed (+1 by @im_root)` and the bot confirms
   `User @im_root added Ahmed to in list as +1 ✅`

- The prompt and your typed reply are deleted afterwards, so only the confirmation remains
  (requires the bot to be a group admin; skipped silently otherwise)
- Tapping `+1` **reserves** your slot for 2 minutes while you type, so nobody can take it
- Reply `cancel` to release the slot; unanswered prompts expire on their own
- Limit of 2 guests per member (`GUEST_LIMIT_PER_USER`)
- Names are 2–32 characters, must contain a letter or number, and cannot duplicate a name
  already on the list
- Pressing `❌ Out` when you have guests asks what to remove: just you, a specific guest,
  or everyone you added. Only the member who opened that menu can use it.

### Team Formation
- Automated balanced team creation
- Smart player distribution algorithm:
  - Separates regular and "+1" players
  - Uses player rating system
  - Implements snake draft ordering
  - Creates balanced "Black" and "White" teams

### Admin Controls
- Admin-only commands in group chats:
  - `/play` - Start new game session
  - `/cancel_play` - Cancel ongoing session
- Rate limiting and cooldown periods
- Spam prevention mechanisms

### Security Features
- Rate limiting for all actions
- Admin permission verification
- Thread-safe operations
- Comprehensive error handling

## Technical Stack

- Python 3.9
- Redis for state management
- Docker and Docker Compose
- AsyncIO for asynchronous operations
- python-telegram-bot library
- Timezone support (Indian/Maldives)

## Prerequisites

- Docker and Docker Compose
- Telegram Bot Token
- Redis instance (provided via Docker Compose)
- Traefik proxy (optional)

## Environment Variables

Required environment variables as specified in `.env.example`:

```env
TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}
REDIS_URL=redis://redis:6379
LOG_DIR=/app/logs
```

## Installation & Deployment

1. Clone the repository:
```bash
git clone <repository-url>
cd football-play-bot
```

2. Create `.env` file with required environment variables:
```bash
cp .env.example .env
# Edit .env with your Telegram Bot Token
```

3. Deploy using Docker Compose:
```bash
docker-compose up -d
```

## Docker Configuration

The application is containerized using a multi-stage build process for optimized image size and security:

- Base image: python:3.9-slim
- Two-stage build process:
  1. Builder stage for installing dependencies
  2. Runtime stage for minimal production image
- Includes timezone configuration (Indian/Maldives)
- Volume mounts for logs and Redis data
- Resource limits configured in docker-compose.yml
- External traefik-public network support

### Resource Limits
- Redis:
  - Memory Limit: 256MB
  - Memory Reservation: 128MB
- Bot Application:
  - Memory Limit: 512MB
  - Memory Reservation: 256MB

## Logging

- Comprehensive logging system
- Daily rotating log files
- JSON format logging in Docker
- Log retention configuration:
  - Maximum file size: 10MB
  - Maximum files: 3
- Timezone-aware logging (Indian/Maldives)

## Error Handling

- Automatic retry mechanism with exponential backoff
- Graceful shutdown handling
- Redis connection management with retry logic
- Telegram API rate limit handling
- Comprehensive error logging
- Debounced message updates to avoid API limits

## Health Checks

- Redis health monitoring
  ```yaml
  healthcheck:
    test: ["CMD", "redis-cli", "ping"]
    interval: 5s
    timeout: 3s
    retries: 3
  ```
- Connection state verification
- Automatic reconnection handling

## Contributing

1. Fork the repository
2. Create your feature branch
3. Commit your changes
4. Push to the branch
5. Create a new Pull Request

## License

Not under any license

## Author

Program developed with help of AI
Infra and CI/CD setup done by me.