import irc.client
import subprocess
import threading
import time
import os
from collections import Counter, defaultdict
import configparser

config = configparser.ConfigParser()
config.read("settings.txt")
settings = config["DEFAULT"]
CHANNEL = settings["CHANNEL"]
SERVER = settings["SERVER"]
PORT = int(settings["PORT"])
BOT_NICK = settings["BOT_NICK"]
GAME_DIR = settings["GAME_DIR"]
VOTE_INTERVAL = int(settings["VOTE_INTERVAL"])
DEBUG = settings.getboolean("DEBUG", fallback=False)
BUFFERLENGTH = int(settings["BUFFERLENGTH"])
ACTIVE_DECAY = int(settings["ACTIVE_DECAY"])
MAJORITY_RATIO = float(settings["MAJORITY_RATIO"])

def debug_print(msg):
    if DEBUG:
        print("[DEBUG]" + msg)

class InformGame:
    def __init__(self, game_path):
        debug_print(f"Starting game with path: {game_path}")
        self.process = subprocess.Popen(
            ["stdbuf", "-oL", "dfrotz", "-m", game_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )

    def send_command(self, command):
        debug_print(f"Sending command to game: {command}")
        self.process.stdin.write(command + "\n")
        self.process.stdin.flush()

    def stop(self):
        debug_print("Stopping game process")
        if self.process.poll() is None:
            self.process.terminate()
            self.process.wait()


class InformBot:
    def __init__(self):
        self.reactor = irc.client.Reactor()
        self.client = self.reactor.server()
        self.game = None
        self.gamename = "Unknown"
        self.users_in_channel = set()
        
        self.voter_choices = {}  # user -> command
        self.votes = defaultdict(set)  # command -> set of users

        self.game_votes = defaultdict(set)    # game -> set of users
        self.game_voter_choices = {}          # user -> game

        self.stopgame_voters = set()
        self.replay_buffer = []
        self.required_votes = 1
        self.users_last_activity = {}  # dict: nick -> last message timestamp
        self.command_vote_start = None
        self.load_vote_start = None
        self.stopgame_vote_start = None

    def get_active_user_count(self):
        now = time.time()
        cutoff = now - ACTIVE_DECAY
        # Remove inactive users
        inactive = [user for user, ts in self.users_last_activity.items() if ts < cutoff]
        for user in inactive:
            del self.users_last_activity[user]
        # Optionally exclude the bot itself
        active_users = [u for u in self.users_last_activity if u != BOT_NICK]
        return len(active_users)

    def get_required_votes(self):
        active_count = self.get_active_user_count()
        return max(1, int(active_count * MAJORITY_RATIO) + 1)

    def _handle_status(self):
        required_votes = self.get_required_votes()
        game_status = f"Active game: {self.gamename}" if self.game else "No game loaded."
        settings = (
            f"{game_status} | "
            f"Vote interval: {VOTE_INTERVAL}s | "
            f"Majority threshold: {required_votes} votes | "
            f"Replay buffer size: {len(self.replay_buffer)}/{BUFFERLENGTH}"
        )
        self.client.privmsg(CHANNEL, settings)

    def connect(self):
        try:
            self.client.connect(SERVER, PORT, BOT_NICK)
        except irc.client.ServerConnectionError as e:
            print(f"[ERROR] Connection failed: {e}")
            return

        self.client.add_global_handler("welcome", self.on_connect)
        self.client.add_global_handler("pubmsg", self.on_pubmsg)
        self.client.add_global_handler("namreply", self.on_names)
        self.client.add_global_handler("join", self.on_join)
        self.client.add_global_handler("part", self.on_part)
        self.client.add_global_handler("quit", self.on_quit)
        self.client.add_global_handler("kick", self.on_kick)
        self.client.add_global_handler("ping", lambda conn, event: conn.pong(event.target))
        threading.Thread(target=self._vote_loop, daemon=True).start()
        self.reactor.process_forever()

    def on_connect(self, conn, event):
        debug_print(f"Connected to {SERVER}, joining {CHANNEL}")
        conn.join(CHANNEL)

    def on_kick(self, conn, event):
        nick = event.arguments[0]
        self.users_in_channel.discard(nick)
        debug_print(f"{nick} was kicked from {CHANNEL}")

    def on_pubmsg(self, conn, event):
        msg = event.arguments[0].strip()
        user = event.source.nick
        now = time.time()
        self.users_last_activity[user] = now
        debug_print(f"Received message from {user}: {msg}")

        if msg.lower() == "!games":
            games = self.list_games()
            debug_print(f"Listing games: {games}")
            conn.privmsg(CHANNEL, "Available games: " + ", ".join(games) if games else "No games found.")

        elif msg.lower().startswith("!load "):
            game_name = msg[6:].strip()
            if game_name in self.list_games():

                if user in self.game_voter_choices:
                    old_game = self.game_voter_choices[user]
                    self.game_votes[old_game].discard(user)

                # Add new vote
                self.game_voter_choices[user] = game_name
                self.game_votes[game_name].add(user)
                if not self.load_vote_start:
                    self.load_vote_start = time.time()
            else:
                conn.privmsg(CHANNEL, f"{user}: game '{game_name}' not found.")

        elif msg.lower().startswith("!vote"):
                cmd = msg[5:].strip()

                if not cmd:
                    status_msgs = []

                    if self.game_votes:
                        game_vote_counts = Counter({g: len(users) for g, users in self.game_votes.items() if len(users) > 0})
                        parts = [f"{g}: {c} vote{'s' if c != 1 else ''}" for g, c in game_vote_counts.items()]
                        status_msgs.append("Load votes: " + ", ".join(parts))
                        if self.load_vote_start:
                            time_elapsed = now - self.load_vote_start
                            time_remaining = max(0, int(VOTE_INTERVAL - time_elapsed))
                            status_msgs.append(f"!! {time_remaining} second{'s' if time_remaining != 1 else ''} left to vote. !!")

                    if self.votes:
                        cmd_vote_counts = Counter({c: len(users) for c, users in self.votes.items() if len(users) > 0})
                        parts = [f"'{c}': {v} vote{'s' if v != 1 else ''}" for c, v in cmd_vote_counts.items()]
                        status_msgs.append("Command votes: " + ", ".join(parts))
                        if self.command_vote_start:
                            time_elapsed = now - self.command_vote_start
                            time_remaining = int(VOTE_INTERVAL - time_elapsed)
                            status_msgs.append(f"!! {time_remaining} second{'s' if time_remaining != 1 else ''} left to vote. !!")

                    if status_msgs:
                        for line in status_msgs:
                            conn.privmsg(CHANNEL, line)
                    else:
                        conn.privmsg(CHANNEL, "No active votes currently.")
                    return

                if self.game is None:
                    conn.privmsg(CHANNEL, "No game loaded. Vote to load a game first using: !load <gamefile>")
                    return

                # Remove old vote if they voted before
                if user in self.voter_choices:
                    old_cmd = self.voter_choices[user]
                    self.votes[old_cmd].discard(user)

                # Register new vote
                self.voter_choices[user] = cmd
                self.votes[cmd].add(user)

                if not self.command_vote_start:
                    self.command_vote_start = time.time()
                
        elif msg.lower() == "!stopgame":
            if not self.game:
                conn.privmsg(CHANNEL, "No game is currently running.")
                return

            if user not in self.stopgame_voters:
                self.stopgame_voters.add(user)
                if not self.stopgame_vote_start:
                    self.stopgame_vote_start = time.time()
            else:
                conn.privmsg(CHANNEL, f"{user}: you already voted to stop the game this round.")

        elif msg.lower() == "!replay":
            if self.replay_buffer:
                debug_print(f"Replaying last {len(self.replay_buffer)} lines")
                for replay_line in self.replay_buffer:
                    conn.privmsg(CHANNEL, replay_line)
                    time.sleep(1) 
            else:
                conn.privmsg(CHANNEL, "No lines to replay yet.")

        elif msg.lower() == "!status":
            self._handle_status()

        elif msg.lower() == "!help":
            conn.privmsg(CHANNEL, "Commands: !games, !load <gamefile>, !vote <command>, !stopgame, !replay, !status")

    def on_names(self, conn, event):
        if event.arguments[1] == CHANNEL:
            raw_users = event.arguments[2].split()
            clean_users = set()
            for nick in raw_users:
                # Strip IRC prefixes like @, +, etc.
                while nick and nick[0] in ('@', '+', '%', '&', '~'):
                    nick = nick[1:]
                if nick != BOT_NICK:
                    clean_users.add(nick)
            self.users_in_channel = clean_users
            debug_print(f"Names in channel (cleaned): {self.users_in_channel}")

    def on_join(self, conn, event):
        if event.target == CHANNEL:
            nick = event.source.nick
            if nick != BOT_NICK:
                self.users_in_channel.add(nick)
                debug_print(f"{nick} joined {CHANNEL}")

    def on_part(self, conn, event):
        if event.target == CHANNEL:
            nick = event.source.nick
            self.users_in_channel.discard(nick)
            debug_print(f"{nick} left {CHANNEL}")

    def on_quit(self, conn, event):
        nick = event.source.nick
        self.users_in_channel.discard(nick)
        debug_print(f"{nick} quit")

    def list_games(self):
        try:
            games = [f for f in os.listdir(GAME_DIR)]
            debug_print(f"Found games in directory: {games}")
            return games
        except:
            print("[ERROR] Game directory not found.")
            return []

    def _vote_loop(self):
        while True:
            time.sleep(1)
            now = time.time()
            debug_print(f"Vote loop triggered. Game loaded: {self.game is not None}")
            debug_print(f"Users online: {len(self.users_in_channel)}")
            active_count = self.get_active_user_count()
            debug_print(f"Active users: {active_count}")
            required_votes = self.get_required_votes()

            if self.load_vote_start and (now - self.load_vote_start >= VOTE_INTERVAL):
                if self.game_votes:
                    vote_counts = Counter({g: len(users) for g, users in self.game_votes.items()})
                    debug_print(f"Game load votes: {dict(vote_counts)}")
                    top_game, top_votes = vote_counts.most_common(1)[0]
                    if top_votes >= required_votes:
                        self.load_game(top_game)
                        self.client.privmsg(CHANNEL, f"Loading game: {top_game}")
                    else:
                        self.client.privmsg(CHANNEL, f"No majority to load game. Votes cleared.")
                        debug_print(f"Load game votes: {top_votes} / {required_votes}")
                    self.game_votes.clear()
                    self.game_voter_choices.clear()
                    self.votes.clear()
                    self.voter_choices.clear()
                    self.stopgame_voters.clear()
                    self.load_vote_start = None
                    continue

            if self.stopgame_vote_start and (now - self.stopgame_vote_start >= VOTE_INTERVAL):
                if self.game and self.stopgame_voters:
                    stop_votes = len(self.stopgame_voters)
                    if stop_votes >= required_votes:
                        self.game.stop()
                        self.game = None
                        self.client.privmsg(CHANNEL, "Game stopped by vote.")
                    else:
                        self.client.privmsg(CHANNEL, "No majority to stop game. Votes cleared.")
                        debug_print(f"Stop game votes: {stop_votes} / {required_votes}")
                    self.votes.clear()
                    self.voter_choices.clear()
                    self.game_votes.clear()
                    self.game_voter_choices.clear()
                    self.stopgame_voters.clear()
                    self.stopgame_vote_start = None
                    continue

            if self.game is None:
                self.votes.clear()
                self.voter_choices.clear()
                continue

            if self.command_vote_start and (now - self.command_vote_start >= VOTE_INTERVAL):
                if self.votes:
                    vote_counts = Counter({cmd: len(users) for cmd, users in self.votes.items()})
                    debug_print(f"Command votes: {dict(vote_counts)}")
                    top_cmd, top_votes = vote_counts.most_common(1)[0]
                    if top_votes >= required_votes:
                        self.game.send_command(top_cmd)
                        self.client.privmsg(CHANNEL, f"> {top_cmd}")
                    else:
                        self.client.privmsg(CHANNEL, "No majority for command. Votes cleared.")
                        debug_print(f"Command votes: {top_votes} / {required_votes}")
                    self.votes.clear()
                    self.voter_choices.clear()
                    self.command_vote_start = None


    def load_game(self, gamefile):
        if self.game:
            self.game.stop()
            self.client.privmsg(CHANNEL, "Previous game stopped.")

        path = os.path.join(GAME_DIR, gamefile)
        debug_print(f"Loading game from: {path}")
        self.game = InformGame(path)
        time.sleep(0.5)
        threading.Thread(target=self._relay_game_output, daemon=True).start()

    def _relay_game_output(self):
        first_line_set = False
        game_name_counter=0
        while self.game and self.game.process.poll() is None:
            try:
                line = self.game.process.stdout.readline()
                if line == '':
                    time.sleep(0.1)
                    continue

                line = line.rstrip('\r\n')
                if line:
                    if not first_line_set:
                        game_name_counter+=1
                        if game_name_counter==3:
                            self.gamename = line
                            first_line_set = True
                            debug_print(f"Game name set to: '{self.gamename}'")
                    debug_print(f"Sending line to IRC: '{line}'")
                    self.client.privmsg(CHANNEL, line)
                    self.replay_buffer.append(line)
                    if len(self.replay_buffer) > BUFFERLENGTH:
                        self.replay_buffer.pop(0)
                    time.sleep(1)

            except Exception as e:
                debug_print(f"[ERROR] Reading game output: {e}")
                break

if __name__ == "__main__":
    debug_print("[DEBUG] Starting InformBot...")
    bot = InformBot()
    bot.connect()
