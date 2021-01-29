from decimal import Decimal
import logging
import asyncio
from typing import Dict, List, Set
import pandas as pd
import numpy as np
import time
from hummingbot.core.clock import Clock
from hummingbot.logger import HummingbotLogger
from hummingbot.strategy.strategy_py_base import StrategyPyBase
from hummingbot.connector.exchange_base import ExchangeBase
from hummingbot.strategy.market_trading_pair_tuple import MarketTradingPairTuple
from .data_types import Proposal, PriceSize
from hummingbot.core.event.events import OrderType, TradeType
from hummingbot.core.data_type.limit_order import LimitOrder
from hummingbot.core.utils.estimate_fee import estimate_fee
from hummingbot.core.utils.market_price import usd_value
from hummingbot.strategy.pure_market_making.inventory_skew_calculator import (
    calculate_bid_ask_ratios_from_base_asset_ratio
)
NaN = float("nan")
s_decimal_zero = Decimal(0)
s_decimal_nan = Decimal("NaN")
lms_logger = None
INVENTORY_RANGE_MULTIPLIER = 1


class LiquidityMiningStrategy(StrategyPyBase):
    """
    This is a basic arbitrage strategy which can be used for most types of connectors (CEX, DEX or AMM).
    For a given order amount, the strategy checks both sides of the trade (market_1 and market_2) for arb opportunity.
    If presents, the strategy submits taker orders to both market.
    """

    @classmethod
    def logger(cls) -> HummingbotLogger:
        global lms_logger
        if lms_logger is None:
            lms_logger = logging.getLogger(__name__)
        return lms_logger

    def __init__(self,
                 exchange: ExchangeBase,
                 market_infos: Dict[str, MarketTradingPairTuple],
                 token: str,
                 order_size: Decimal,
                 spread: Decimal,
                 target_base_pct: Decimal,
                 order_refresh_time: float,
                 order_refresh_tolerance_pct: Decimal,
                 status_report_interval: float = 900):
        super().__init__()
        self._exchange = exchange
        self._market_infos = market_infos
        self._token = token
        self._order_size = order_size
        self._spread = spread
        self._order_refresh_time = order_refresh_time
        self._order_refresh_tolerance_pct = order_refresh_tolerance_pct
        self._target_base_pct = target_base_pct
        self._ev_loop = asyncio.get_event_loop()
        self._last_timestamp = 0
        self._status_report_interval = status_report_interval
        self._ready_to_trade = False
        self._refresh_times = {market: 0 for market in market_infos}
        self._token_balances = {}
        self._sell_budgets = {}
        self._buy_budgets = {}

        self.add_markets([exchange])

    @property
    def active_orders(self):
        limit_orders = self.order_tracker.active_limit_orders
        return [o[1] for o in limit_orders]

    def tick(self, timestamp: float):
        """
        Clock tick entry point, is run every second (on normal tick setting).
        :param timestamp: current tick timestamp
        """
        if not self._ready_to_trade:
            self._ready_to_trade = self._exchange.ready
            if not self._exchange.ready:
                self.logger().warning(f"{self._exchange.name} is not ready. Please wait...")
                return
            else:
                self.logger().info(f"{self._exchange.name} is ready. Trading started.")
                self.create_budget_allocation()

        proposals = self.create_base_proposals()
        self._token_balances = self.adjusted_available_balances()
        self.apply_volatility_adjustment(proposals)
        self.apply_inventory_skew(proposals)
        self.apply_budget_constraint(proposals)
        self.cancel_active_orders(proposals)
        self.execute_orders_proposal(proposals)

        # if self.c_to_create_orders(proposal):
        #     self.c_execute_orders_proposal(proposal)

        self._last_timestamp = timestamp

    async def active_orders_df(self) -> pd.DataFrame:
        columns = ["Market", "Side", "Price", "Spread", "Size", "Size ($)", "Age"]
        data = []
        for order in self.active_orders:
            mid_price = self._market_infos[order.trading_pair].get_mid_price()
            spread = 0 if mid_price == 0 else abs(order.price - mid_price) / mid_price
            size_usd = await usd_value(order.trading_pair.split("-")[0], order.quantity)
            age = "n/a"
            # // indicates order is a paper order so 'n/a'. For real orders, calculate age.
            if "//" not in order.client_order_id:
                age = pd.Timestamp(int(time.time()) - int(order.client_order_id[-16:]) / 1e6,
                                   unit='s').strftime('%H:%M:%S')
            data.append([
                order.trading_pair,
                "buy" if order.is_buy else "sell",
                float(order.price),
                f"{spread:.2%}",
                float(order.quantity),
                round(size_usd),
                age
            ])

        return pd.DataFrame(data=data, columns=columns)

    def market_status_df(self) -> pd.DataFrame:
        data = []
        columns = ["Exchange", "Market", "Mid Price", "Base Balance", "Quote Balance", " Base / Quote %"]
        balances = self.adjusted_available_balances()
        for market, market_info in self._market_infos.items():
            base, quote = market.split("-")
            mid_price = market_info.get_mid_price()
            adj_base_bal = balances[base] * mid_price
            total_bal = adj_base_bal + balances[quote]
            base_pct = adj_base_bal / total_bal if total_bal > 0 else s_decimal_zero
            quote_pct = balances[quote] / total_bal if total_bal > 0 else s_decimal_zero
            data.append([
                self._exchange.display_name,
                market,
                float(mid_price),
                float(balances[base]),
                float(balances[quote]),
                f"{f'{base_pct:.0%}':>5s} /{f'{quote_pct:.0%}':>5s}"
            ])
        return pd.DataFrame(data=data, columns=columns).replace(np.nan, '', regex=True)

    async def format_status(self) -> str:
        if not self._ready_to_trade:
            return "Market connectors are not ready."
        lines = []
        warning_lines = []
        warning_lines.extend(self.network_warning(list(self._market_infos.values())))

        lines.extend(["", "  Markets:"] + ["    " + line for line in
                                           self.market_status_df().to_string(index=False).split("\n")])

        # See if there're any open orders.
        if len(self.active_orders) > 0:
            df = await self.active_orders_df()
            lines.extend(["", "  Orders:"] + ["    " + line for line in df.to_string(index=False).split("\n")])
        else:
            lines.extend(["", "  No active maker orders."])

        warning_lines.extend(self.balance_warning(list(self._market_infos.values())))
        if len(warning_lines) > 0:
            lines.extend(["", "*** WARNINGS ***"] + warning_lines)
        return "\n".join(lines)

    def start(self, clock: Clock, timestamp: float):
        pass

    def stop(self, clock: Clock):
        pass

    def create_base_proposals(self):
        proposals = []
        for market, market_info in self._market_infos.items():
            mid_price = market_info.get_mid_price()
            buy_price = mid_price * (Decimal("1") - self._spread)
            buy_price = self._exchange.quantize_order_price(market, buy_price)
            buy_size = self.base_order_size(market, buy_price)
            sell_price = mid_price * (Decimal("1") + self._spread)
            sell_price = self._exchange.quantize_order_price(market, sell_price)
            sell_size = self.base_order_size(market, sell_price)
            proposals.append(Proposal(market, PriceSize(buy_price, buy_size), PriceSize(sell_price, sell_size)))
        return proposals

    def apply_volatility_adjustment(self, proposals):
        # Todo: apply volatility spread adjustment, the volatility can be calculated from OHLC
        #  (maybe ATR or hourly candle?) or from the mid price movement (see
        #  scripts/spreads_adjusted_on_volatility_script.py).
        return

    def create_budget_allocation(self):
        # Equally assign buy and sell budgets to all markets
        self._sell_budgets = {m: s_decimal_zero for m in self._market_infos}
        self._buy_budgets = {m: s_decimal_zero for m in self._market_infos}
        if self._token == list(self._market_infos.keys())[0].split("-")[0]:
            base_markets = [m for m in self._market_infos if m.split("-")[0] == self._token]
            sell_size = self._exchange.get_available_balance(self._token) / len(base_markets)
            for market in base_markets:
                self._sell_budgets[market] = sell_size
                self._buy_budgets[market] = self._exchange.get_available_balance(market.split("-")[1])
        else:
            quote_markets = [m for m in self._market_infos if m.split("-")[1] == self._token]
            buy_size = self._exchange.get_available_balance(self._token) / len(quote_markets)
            for market in quote_markets:
                self._buy_budgets[market] = buy_size
                self._sell_budgets[market] = self._exchange.get_available_balance(market.split("-")[0])

    def assign_balanced_budgets(self):
        # Equally assign buy and sell budgets to all markets
        base_tokens = self.all_base_tokens()
        self._sell_budgets = {m: s_decimal_zero for m in self._market_infos}
        for base in base_tokens:
            base_markets = [m for m in self._market_infos if m.split("-")[0] == base]
            sell_size = self._exchange.get_available_balance(base) / len(base_markets)
            for market in base_markets:
                self._sell_budgets[market] = sell_size
        # Then assign all the buy order size based on the quote token balance available
        quote_tokens = self.all_quote_tokens()
        self._buy_budgets = {m: s_decimal_zero for m in self._market_infos}
        for quote in quote_tokens:
            quote_markets = [m for m in self._market_infos if m.split("-")[1] == quote]
            buy_size = self._exchange.get_available_balance(quote) / len(quote_markets)
            for market in quote_markets:
                self._buy_budgets[market] = buy_size

    def base_order_size(self, trading_pair: str, price: Decimal = s_decimal_zero):
        base, quote = trading_pair.split("-")
        if self._token == base:
            return self._order_size
        if price == s_decimal_zero:
            price = self._market_infos[trading_pair].get_mid_price()
        return self._order_size / price

    def apply_budget_constraint(self, proposals: List[Proposal]):
        balances = self._token_balances.copy()
        for proposal in proposals:
            if balances[proposal.base()] < proposal.sell.size:
                proposal.sell.size = balances[proposal.base()]
            proposal.sell.size = self._exchange.quantize_order_amount(proposal.market, proposal.sell.size)
            balances[proposal.base()] -= proposal.sell.size

            quote_size = proposal.buy.size * proposal.buy.price
            quote_size = balances[proposal.quote()] if balances[proposal.quote()] < quote_size else quote_size
            buy_fee = estimate_fee(self._exchange.name, True)
            buy_size = quote_size / (proposal.buy.price * (Decimal("1") + buy_fee.percent))
            proposal.buy.size = self._exchange.quantize_order_amount(proposal.market, buy_size)
            balances[proposal.quote()] -= quote_size

    def is_within_tolerance(self, cur_orders: List[LimitOrder], proposal: Proposal):
        cur_buy = [o for o in cur_orders if o.is_buy]
        cur_sell = [o for o in cur_orders if not o.is_buy]
        if (cur_buy and proposal.buy.size <= 0) or (cur_sell and proposal.sell.size <= 0):
            return False
        if cur_buy and \
                abs(proposal.buy.price - cur_buy[0].price) / cur_buy[0].price > self._order_refresh_tolerance_pct:
            return False
        if cur_sell and \
                abs(proposal.sell.price - cur_sell[0].price) / cur_sell[0].price > self._order_refresh_tolerance_pct:
            return False
        return True

    def cancel_active_orders(self, proposals: List[Proposal]):
        for proposal in proposals:
            if self._refresh_times[proposal.market] > self.current_timestamp:
                continue
            cur_orders = [o for o in self.active_orders if o.trading_pair == proposal.market]
            if not cur_orders or self.is_within_tolerance(cur_orders, proposal):
                continue
            for order in cur_orders:
                self.cancel_order(self._market_infos[proposal.market], order.client_order_id)
                # To place new order on the next tick
                self._refresh_times[order.trading_pair] = self.current_timestamp + 0.1

    def execute_orders_proposal(self, proposals: List[Proposal]):
        for proposal in proposals:
            cur_orders = [o for o in self.active_orders if o.trading_pair == proposal.market]
            if cur_orders or self._refresh_times[proposal.market] > self.current_timestamp:
                continue
            if proposal.buy.size > 0:
                self.logger().info(f"({proposal.market}) Creating a bid order {proposal.buy} value: "
                                   f"{proposal.buy.size * proposal.buy.price:.2f} {proposal.quote()}")
                self.buy_with_specific_market(
                    self._market_infos[proposal.market],
                    proposal.buy.size,
                    order_type=OrderType.LIMIT_MAKER,
                    price=proposal.buy.price
                )
            if proposal.sell.size > 0:
                self.logger().info(f"({proposal.market}) Creating an ask order at {proposal.sell}")
                self.sell_with_specific_market(
                    self._market_infos[proposal.market],
                    proposal.sell.size,
                    order_type=OrderType.LIMIT_MAKER,
                    price=proposal.sell.price
                )
            if proposal.buy.size > 0 or proposal.sell.size > 0:
                self._refresh_times[proposal.market] = self.current_timestamp + self._order_refresh_time

    def all_base_tokens(self) -> Set[str]:
        tokens = set()
        for market in self._market_infos:
            tokens.add(market.split("-")[0])
        return tokens

    def all_quote_tokens(self) -> Set[str]:
        tokens = set()
        for market in self._market_infos:
            tokens.add(market.split("-")[1])
        return tokens

    def all_tokens(self) -> Set[str]:
        tokens = set()
        for market in self._market_infos:
            tokens.update(market.split("-"))
        return tokens

    def adjusted_available_balances(self) -> Dict[str, Decimal]:
        """
        Calculates all available balances, account for amount attributed to orders and reserved balance.
        :return: a dictionary of token and its available balance
        """
        tokens = self.all_tokens()
        adjusted_bals = {t: s_decimal_zero for t in tokens}
        total_bals = {t: s_decimal_zero for t in tokens}
        total_bals.update(self._exchange.get_all_balances())
        for token in tokens:
            adjusted_bals[token] = self._exchange.get_available_balance(token)
        for order in self.active_orders:
            base, quote = order.trading_pair.split("-")
            if order.is_buy:
                adjusted_bals[quote] += order.quantity * order.price
            else:
                adjusted_bals[base] += order.quantity
        # for token in tokens:
        #     adjusted_bals[token] = min(adjusted_bals[token], total_bals[token])
        #     reserved = self._reserved_balances.get(token, s_decimal_zero)
        #     adjusted_bals[token] -= reserved
        # self.logger().info(f"token balances: {adjusted_bals}")
        return adjusted_bals

    def apply_inventory_skew(self, proposals: List[Proposal]):
        # balances = self.adjusted_available_balances()
        for proposal in proposals:
            buy_budget = self._buy_budgets[proposal.market]
            sell_budget = self._sell_budgets[proposal.market]
            mid_price = self._market_infos[proposal.market].get_mid_price()
            total_order_size = proposal.sell.size + proposal.buy.size
            bid_ask_ratios = calculate_bid_ask_ratios_from_base_asset_ratio(
                float(sell_budget),
                float(buy_budget),
                float(mid_price),
                float(self._target_base_pct),
                float(total_order_size * INVENTORY_RANGE_MULTIPLIER)
            )
            proposal.buy.size *= Decimal(bid_ask_ratios.bid_ratio)
            # proposal.buy.size = self._exchange.quantize_order_amount(proposal.market, proposal.buy.size)

            proposal.sell.size *= Decimal(bid_ask_ratios.ask_ratio)
            # proposal.sell.size = self._exchange.quantize_order_amount(proposal.market, proposal.sell.size)

    def did_fill_order(self, order_filled_event):
        order_id = order_filled_event.order_id
        market_info = self.order_tracker.get_shadow_market_pair_from_order_id(order_id)
        if market_info is not None:
            if order_filled_event.trade_type is TradeType.BUY:
                self.log_with_clock(
                    logging.INFO,
                    f"({market_info.trading_pair}) Maker buy order of "
                    f"{order_filled_event.amount} {market_info.base_asset} filled."
                )
                self._buy_budgets[market_info.trading_pair] -= (order_filled_event.amount * order_filled_event.price)
                self._sell_budgets[market_info.trading_pair] += order_filled_event.amount
            else:
                self.log_with_clock(
                    logging.INFO,
                    f"({market_info.trading_pair}) Maker sell order of "
                    f"{order_filled_event.amount} {market_info.base_asset} filled."
                )
                self._sell_budgets[market_info.trading_pair] -= order_filled_event.amount
                self._buy_budgets[market_info.trading_pair] += (order_filled_event.amount * order_filled_event.price)