"""Execution: broker abstraction, paper-trade engine, order manager."""

from execution.broker import BrokerClient, get_broker
from execution.order_manager import OrderManager, StagedOrder
from execution.paper_trade import PaperTradeEngine

__all__ = [
    "BrokerClient",
    "OrderManager",
    "PaperTradeEngine",
    "StagedOrder",
    "get_broker",
]
