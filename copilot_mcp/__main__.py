"""Entry point — allows `python -m copilot_mcp`"""
from .server import main
import asyncio

asyncio.run(main())
