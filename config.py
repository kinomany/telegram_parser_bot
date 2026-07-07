import os
from dotenv import load_dotenv

load_dotenv()


def env_str(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError as error:
        raise ValueError(f"{name} должен быть числом, сейчас: {value!r}") from error


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "да", "on"}

def env_int_list(name: str, default: list[int] | None = None) -> list[int]:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return list(default or [])

    result: list[int] = []
    for part in value.replace(";", ",").split(","):
        item = part.strip()
        if not item:
            continue
        try:
            result.append(int(item))
        except ValueError as error:
            raise ValueError(f"{name} должен содержать Telegram ID через запятую, сейчас: {value!r}") from error
    return result




                           
               
                           

APP_ROLE = env_str("APP_ROLE", "bot")

                           
                         
                           

BOT_TOKEN = env_str("BOT_TOKEN")
PHONE_NUMBER = env_str("PHONE_NUMBER")

API_ID = env_int("API_ID", 0)
API_HASH = env_str("API_HASH", "")

SESSIONS_DIR = env_str("SESSIONS_DIR", "sessions")
TELETHON_SESSION_NAME = env_str("TELETHON_SESSION_NAME", f"{SESSIONS_DIR}/user_session")

DIGEST_WORKER_TELETHON_SESSION_NAME = env_str(
    "DIGEST_WORKER_TELETHON_SESSION_NAME",
    TELETHON_SESSION_NAME,
)

RUNTIME_TELETHON_SESSION_NAME = (
    DIGEST_WORKER_TELETHON_SESSION_NAME
    if str(APP_ROLE or "").lower() in {"worker", "digest_worker", "autodigest_worker"}
    else TELETHON_SESSION_NAME
)

CHANNEL_PROCESSOR_SESSION_NAME = env_str("CHANNEL_PROCESSOR_SESSION_NAME", "channel_processor_session")


                           
             
                           

DB_HOST = env_str("DB_HOST", "localhost")
DB_PORT = env_int("DB_PORT", 5432)
DB_NAME = env_str("DB_NAME", "telegram_parser_bot")
DB_USER = env_str("DB_USER", "postgres")
DB_PASSWORD = env_str("DB_PASSWORD", "")

DB_POOL_MIN_SIZE = env_int("DB_POOL_MIN_SIZE", 1)
DB_POOL_MAX_SIZE = env_int("DB_POOL_MAX_SIZE", 5)


                           
       
                           

CHANNELS_TXT_PATH = env_str("CHANNELS_TXT_PATH", "channels.txt")
LOG_FILE = env_str("LOG_FILE", "parser.log")


                           
                       
                           

MAX_CUSTOM_RANGE_DAYS = env_int("MAX_CUSTOM_RANGE_DAYS", 31)
MAX_CUSTOM_LOOKBACK_DAYS = env_int("MAX_CUSTOM_LOOKBACK_DAYS", 90)

MAX_MESSAGES_PER_CHANNEL = env_int("MAX_MESSAGES_PER_CHANNEL", 40)
MAX_TELEGRAM_CHECK_LIMIT_PER_CHANNEL = env_int("MAX_TELEGRAM_CHECK_LIMIT_PER_CHANNEL", 120)
MAX_CHANNELS_PER_PARSE = env_int("MAX_CHANNELS_PER_PARSE", 30)

TOP_RELEVANT_MESSAGES_LIMIT = env_int("TOP_RELEVANT_MESSAGES_LIMIT", 10)
MIN_RELEVANT_MESSAGE_SCORE = env_int("MIN_RELEVANT_MESSAGE_SCORE", 2)


                           
                     
                           

GEMINI_API_KEY = env_str("GEMINI_API_KEY")
OPENAI_API_KEY = env_str("OPENAI_API_KEY")

OPENAI_KEYWORD_MODEL = env_str("OPENAI_KEYWORD_MODEL", "gpt-4.1-nano")

GEMINI_KEYWORD_MODEL = env_str("GEMINI_KEYWORD_MODEL", "gemini-2.5-flash-lite")
GEMINI_QUERY_MODEL = env_str("GEMINI_QUERY_MODEL", GEMINI_KEYWORD_MODEL)
GEMINI_REPORT_MODEL = env_str("GEMINI_REPORT_MODEL", "gemini-2.5-flash")

GEMINI_KEYWORD_MODELS = list(dict.fromkeys([
    GEMINI_KEYWORD_MODEL,
    "gemini-2.5-flash",
]))

GEMINI_QUERY_MODELS = list(dict.fromkeys([
    GEMINI_QUERY_MODEL,
    "gemini-2.5-flash",
]))

GEMINI_REPORT_MODELS = list(dict.fromkeys([
    GEMINI_REPORT_MODEL,
    "gemini-2.5-flash-lite",
]))

                                                                     
AI_MAX_MESSAGES_TO_SCAN = env_int("AI_MAX_MESSAGES_TO_SCAN", 80)

                                                        
AI_SHORT_MESSAGE_MIN_CHARS = env_int("AI_SHORT_MESSAGE_MIN_CHARS", 150)
AI_SHORT_MESSAGE_MAX_CHARS = env_int("AI_SHORT_MESSAGE_MAX_CHARS", 250)
AI_SHORT_MESSAGE_BUCKETS = [
    (0, 10, 6),
    (10, 30, 8),
    (30, 80, 10),
]
AI_MIN_SHORT_MESSAGES_DESIRED = env_int("AI_MIN_SHORT_MESSAGES_DESIRED", 12)

                                                  
AI_LONG_MESSAGE_MIN_CHARS = env_int("AI_LONG_MESSAGE_MIN_CHARS", 150)
AI_LONG_MESSAGE_MAX_CHARS = env_int("AI_LONG_MESSAGE_MAX_CHARS", 600)
AI_LONG_MESSAGE_BUCKETS = [
    (0, 5, 1),
    (5, 15, 1),
    (15, 30, 2),
    (30, 50, 2),
    (50, 80, 2),
]
AI_MIN_LONG_MESSAGES = env_int("AI_MIN_LONG_MESSAGES", 6)

                                             
AI_TOTAL_MESSAGES_MAX_CHARS = env_int("AI_TOTAL_MESSAGES_MAX_CHARS", 9000)
AI_BUCKET_NEIGHBOR_RADIUS = env_int("AI_BUCKET_NEIGHBOR_RADIUS", 5)

                                                           
PROCESSING_STALE_MINUTES = env_int("PROCESSING_STALE_MINUTES", 30)

                                                      
CHANNELS_TO_PROCESS_PER_RUN = env_int("CHANNELS_TO_PROCESS_PER_RUN", 2)

                                                                          
CHANNELS_TO_ADD_FROM_TXT_PER_RUN = env_int(
    "CHANNELS_TO_ADD_FROM_TXT_PER_RUN",
    CHANNELS_TO_PROCESS_PER_RUN,
)

                                                        
CHANNEL_PROCESS_DELAY_SECONDS = env_int("CHANNEL_PROCESS_DELAY_SECONDS", 30)
CHANNEL_PROCESS_DELAY_JITTER_SECONDS = env_int("CHANNEL_PROCESS_DELAY_JITTER_SECONDS", 20)
FLOOD_WAIT_SLEEP_CAP_SECONDS = env_int("FLOOD_WAIT_SLEEP_CAP_SECONDS", 900)


                           
                          
                           

TOP_CHANNELS_LIMIT = env_int("TOP_CHANNELS_LIMIT", 10)
MIN_CHANNEL_SCORE = env_int("MIN_CHANNEL_SCORE", 12)


                           
          
                           

REPORT_MAX_MESSAGE_CHARS = env_int("REPORT_MAX_MESSAGE_CHARS", 1200)
REPORT_TOTAL_MAX_CHARS = env_int("REPORT_TOTAL_MAX_CHARS", 12000)


                           
                            
                           

DIGEST_MAX_CHANNELS = env_int("DIGEST_MAX_CHANNELS", 10)

                                                                   
                                                                                     
DIGEST_COLLECT_MESSAGES_PER_CHANNEL = env_int("DIGEST_COLLECT_MESSAGES_PER_CHANNEL", 60)

                                                           
DIGEST_MAX_MESSAGES_PER_CHANNEL = env_int("DIGEST_MAX_MESSAGES_PER_CHANNEL", 60)
DIGEST_TOTAL_MAX_MESSAGES = env_int("DIGEST_TOTAL_MAX_MESSAGES", 300)
DIGEST_MAX_MESSAGE_CHARS = env_int("DIGEST_MAX_MESSAGE_CHARS", 900)
DIGEST_CHANNEL_TOTAL_MAX_CHARS = env_int("DIGEST_CHANNEL_TOTAL_MAX_CHARS", 12000)
DIGEST_FINAL_TOTAL_MAX_CHARS = env_int("DIGEST_FINAL_TOTAL_MAX_CHARS", 16000)

                                                                                  
DIGEST_TOTAL_MAX_CHARS = env_int("DIGEST_TOTAL_MAX_CHARS", 14000)

                                                                   
                                                                  
DIGEST_AI_PREVIEW_MESSAGES_PER_CHANNEL = env_int("DIGEST_AI_PREVIEW_MESSAGES_PER_CHANNEL", 5)
DIGEST_AI_PREVIEW_TOTAL_MESSAGES = env_int("DIGEST_AI_PREVIEW_TOTAL_MESSAGES", 30)
DIGEST_AI_PREVIEW_MESSAGE_CHARS = env_int("DIGEST_AI_PREVIEW_MESSAGE_CHARS", 500)



                           
                                   
                           

DIGEST_SUBSCRIPTION_DEFAULT_HOUR = env_int("DIGEST_SUBSCRIPTION_DEFAULT_HOUR", 9)
DIGEST_SUBSCRIPTION_DEFAULT_MINUTE = env_int("DIGEST_SUBSCRIPTION_DEFAULT_MINUTE", 0)
DIGEST_SUBSCRIPTION_DEFAULT_TIMEZONE = env_str("DIGEST_SUBSCRIPTION_DEFAULT_TIMEZONE", "Asia/Tbilisi")
DIGEST_SUBSCRIPTION_SCHEDULER_INTERVAL_SECONDS = env_int("DIGEST_SUBSCRIPTION_SCHEDULER_INTERVAL_SECONDS", 60)
DIGEST_SUBSCRIPTION_DUE_LIMIT = env_int("DIGEST_SUBSCRIPTION_DUE_LIMIT", 2)
DIGEST_SUBSCRIPTION_LOCK_MINUTES = env_int("DIGEST_SUBSCRIPTION_LOCK_MINUTES", 60)
DIGEST_SUBSCRIPTION_DEBUG = env_bool("DIGEST_SUBSCRIPTION_DEBUG", True)

                                                             
                                                                                       
                                                                                     
DIGEST_SUBSCRIPTION_STARTUP_RECOVERY = env_bool("DIGEST_SUBSCRIPTION_STARTUP_RECOVERY", True)
DIGEST_SUBSCRIPTION_RELEASE_LOCKS_ON_STARTUP = env_bool("DIGEST_SUBSCRIPTION_RELEASE_LOCKS_ON_STARTUP", True)
DIGEST_SUBSCRIPTION_STALE_RUN_MINUTES = env_int("DIGEST_SUBSCRIPTION_STALE_RUN_MINUTES", 60)
DIGEST_SUBSCRIPTION_FAILED_BACKOFF_MINUTES = env_int("DIGEST_SUBSCRIPTION_FAILED_BACKOFF_MINUTES", 30)
DIGEST_SUBSCRIPTION_LOCK_HEARTBEAT_SECONDS = env_int("DIGEST_SUBSCRIPTION_LOCK_HEARTBEAT_SECONDS", 60)

                           
                             
                           

                                                                                 
                                                                                    
AI_CONCURRENT_REQUESTS = env_int("AI_CONCURRENT_REQUESTS", 1)


                           
                       
                           

ADMIN_TELEGRAM_IDS = set(env_int_list("ADMIN_TELEGRAM_IDS", []))
ADMIN_ERRORS_LIMIT = env_int("ADMIN_ERRORS_LIMIT", 10)
ADMIN_ERROR_TRACEBACK_CHARS = env_int("ADMIN_ERROR_TRACEBACK_CHARS", 1200)


                           
         
                           

DEBUG = env_bool("DEBUG", False)
PRINT_OPERATION_TIME = env_bool("PRINT_OPERATION_TIME", True)
PRINT_AI_RESPONSE = env_bool("PRINT_AI_RESPONSE", False)
PRINT_FILTER_REASONS = env_bool("PRINT_FILTER_REASONS", True)