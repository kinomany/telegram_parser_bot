import asyncio
import os

from dotenv import load_dotenv

load_dotenv()
os.environ["APP_ROLE"] = "bot"

from app.runners.bot_runner import run_bot_polling


async def main() -> None:
    await run_bot_polling()


if __name__ == "__main__":
    asyncio.run(main())
