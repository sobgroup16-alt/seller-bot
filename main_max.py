"""Точка входа бота MAX. Bothost запускает этот файл, если указать его как главный.

Telegram-бот по-прежнему запускается через main.py. Один токен — один процесс:
этот файл и main.py не должны работать с одним и тем же ботом одновременно.
"""
import asyncio

from max_bot import main

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
