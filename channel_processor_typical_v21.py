import asyncio
from tools.channel_processor_typical_v21 import main, notify_program_result, PROGRAM_EXIT_INFO

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        notify_program_result(
            status="stopped",
            title="Программа остановлена вручную",
            details="Остановка через Ctrl+C или закрытие процесса.",
        )
        raise
    except Exception as error:
        notify_program_result(
            status="error",
            title="Программа завершилась с ошибкой",
            details="Скрипт упал. Ниже причина и traceback.",
            error=error,
        )
        raise
    else:
        notify_program_result(
            status=PROGRAM_EXIT_INFO["status"],
            title=PROGRAM_EXIT_INFO["title"],
            details=PROGRAM_EXIT_INFO["details"],
        )
