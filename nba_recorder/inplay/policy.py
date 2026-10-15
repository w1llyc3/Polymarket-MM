from dataclasses import dataclass
from ..paper_execution.risk import ExecutionPolicy


@dataclass(frozen=True)
class BookPolicy(ExecutionPolicy):
    strategy_mode: str = "BOOK_DRIVEN"
    initial_cash: str = "100"
    initial_inventory: str = "0"
    target_inventory: str = "0"
    order_size: str = "5"
    max_order_shares: str = "5"
    max_order_notional: str = "5"
    max_event_risk: str = "30"
    max_account_risk: str = "40"
    max_asset_inventory: str = "20"
    quote_distance: str = ".02"
    book_max_age_ms: int = 3000
    sports_max_age_ms: int = 3000
    metadata_max_age_ms: int = 60000
    max_backlog_ms: int = 500
    reduce_remaining_s: int = 180
    close_remaining_s: int = 60
    sync_observations: int = 2
    enhanced: bool = True
    max_one_sided_shares: str = "15"
    min_top_depth: str = "5"
    exit_slippage: str = ".03"
    exit_notional: str = "5"
    inventory_skew: str = ".001"

    def __post_init__(self):
        super().__post_init__()
        if self.strategy_mode != "BOOK_DRIVEN" or self.initial_inventory != "0":
            raise ValueError("BookPolicy requires explicit BOOK_DRIVEN and cash-only initialization")
        if not 0 < self.close_remaining_s < self.reduce_remaining_s < 720 or self.sync_observations < 2:
            raise ValueError("invalid conservative phase parameters")
        if not 0 < float(self.quote_distance) < .5 or float(self.max_order_shares) <= 0:
            raise ValueError("invalid distance or share cap")
