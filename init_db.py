"""Standalone script to create Turso DB tables."""
import asyncio
from dotenv import load_dotenv

load_dotenv()

from services.db_service import db


async def main():
    await db.connect()
    await db.init_schema()
    print("Tables created successfully.")
    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
