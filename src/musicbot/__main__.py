from dotenv import load_dotenv

# Load .env BEFORE importing any musicbot modules — several of them read
# os.environ at module-load time (e.g. presence.PLAYER_FILTER), and would
# otherwise miss values that live only in .env.
load_dotenv()

from musicbot.bot import run

if __name__ == "__main__":
    run()
