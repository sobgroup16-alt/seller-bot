"""Точка входа. Bothost и run.sh запускают именно этот файл: python main.py"""
import asyncio

from bot import main

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
