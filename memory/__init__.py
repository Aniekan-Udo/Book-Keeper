"""Graph memory: what the agents remember about a trader's business, in FalkorDB.

    GRAPH_MEMORY        falkordb (default) / off
    FALKORDB_HOST       default 127.0.0.1 (docker-compose sets falkordb). Not "localhost":
                        on Windows that tries IPv6 first and Docker only listens on IPv4
    FALKORDB_PORT       default 6379
    FALKORDB_PASSWORD   optional
    FALKORDB_TIMEOUT_SECONDS  default 2

Layout (ports and adapters):
    ports.py    GraphMemory, the interface the app depends on
    falkor.py   the FalkorDB adapter; every Cypher statement lives here
    null.py     the switched-off adapter
    sync.py     keeps the graph in step with the Postgres ledger
    recall.py   finds the people a message mentions and what's remembered about them
    curator.py  after each reply, decides what to remember
    models.py   plain data passed through the port

`get_graph_memory()` / `get_curator()` return what was set up at startup (main.py
lifespan); until then, and in tests, the null adapter and a curator that does nothing.
"""
import os

from memory.models import GraphMemoryError
from memory.null import NullGraphMemory
from memory.ports import GraphMemory
from utils import logger

_current: GraphMemory = NullGraphMemory()
_curator = None  # memory.curator.Curator, set at startup


def build_graph_memory() -> GraphMemory:
    """Factory: the adapter the environment asks for."""
    choice = os.getenv("GRAPH_MEMORY", "falkordb").strip().lower()
    if choice in ("off", "none", "false", "0"):
        logger.info("graph_memory.off")
        return NullGraphMemory()
    if choice != "falkordb":
        logger.warning("graph_memory.unknown_choice", choice=choice, using="off")
        return NullGraphMemory()

    from memory.falkor import FalkorGraphMemory  # only import the driver when it's used
    host = os.getenv("FALKORDB_HOST", "127.0.0.1")
    port = int(os.getenv("FALKORDB_PORT", "6379"))
    timeout = float(os.getenv("FALKORDB_TIMEOUT_SECONDS", "2"))
    logger.info("graph_memory.falkordb", host=host, port=port)
    return FalkorGraphMemory(host=host, port=port, password=os.getenv("FALKORDB_PASSWORD"),
                             timeout_seconds=timeout)


def get_graph_memory() -> GraphMemory:
    return _current


def set_graph_memory(memory: GraphMemory) -> None:
    global _current
    _current = memory


def get_curator():
    """The curator set at startup, or one that does nothing (graph memory off)."""
    global _curator
    if _curator is None:
        from memory.curator import Curator
        from db import get_db
        _curator = Curator(NullGraphMemory(), get_db)
    return _curator


def set_curator(curator) -> None:
    global _curator
    _curator = curator


__all__ = ["GraphMemory", "GraphMemoryError", "build_graph_memory", "get_curator", "get_graph_memory",
           "set_curator", "set_graph_memory"]
