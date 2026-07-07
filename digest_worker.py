import os

from dotenv import load_dotenv

load_dotenv()
os.environ["APP_ROLE"] = "digest_worker"

from app.runners.digest_worker_runner import main


if __name__ == "__main__":
    main()
