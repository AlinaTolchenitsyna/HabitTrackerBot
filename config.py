from dataclasses import dataclass
from os import getenv
from dotenv import load_dotenv

load_dotenv() 

@dataclass
class Settings:
    bot_token: str

def get_settings() -> Settings:
    token = getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN is not set. Put it into .env as BOT_TOKEN=...")
    return Settings(bot_token=token)
