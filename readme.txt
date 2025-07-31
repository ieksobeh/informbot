You will need:
- dfrotz
- python
- pip install irc

maybe other py modules. settings are as follows:
CHANNEL = # your irc channel name
SERVER = your irc server's address
PORT = your irc server's port
BOT_NICK = your bot's name
GAME_DIR = games (name of the local folder or abs path where games are)
VOTE_INTERVAL = 60 (seconds for a round of voting)
DEBUG = False (set it to True for cli verbose output)
BUFFERLENGTH = 5 (lines to replay from the game)
ACTIVE_DECAY = 300 (seconds to drop users from being considered active)
MAJORITY_RATIO = 0.5 (float for a percentage to determine majority from active users)