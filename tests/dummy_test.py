"""Dummy tests. Might be removed once there are proper ones."""
import pytest
from aiohttp import ClientSession
import asyncio

from volkswagencarnet import vw_connection


@pytest.mark.asyncio
async def async_main():
    """Dummy test to ensure logged in status is false by default."""
    async with ClientSession() as session:
        connection = vw_connection.Connection(session, "jasondanieladams@gmail.com", "Bonzai1957!")
        # if await connection._login():
        #assert connection.logged_in is False
        
        login = await connection._login()

        print(login)

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(async_main())