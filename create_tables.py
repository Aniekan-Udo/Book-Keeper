# create_tables.py
import asyncio
from db import init_models

if __name__ == "__main__":
    asyncio.run(init_models())