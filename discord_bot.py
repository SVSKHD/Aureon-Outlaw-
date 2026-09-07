"""Entry point for the read-only Discord command bot. Run alongside supervisor.py:  py discord_bot.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from xau_mt5_bot.discord_bot import main  # noqa: E402

if __name__ == "__main__":
    main(Path(__file__).resolve().parent)
