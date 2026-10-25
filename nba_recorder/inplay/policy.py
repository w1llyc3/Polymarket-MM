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
    public_flow_pause: bool = False
    allow_manual_phase: bool = False
    manual_max_age_ms: int = 15000
    max_one_sided_shares: str = "15"
    min_top_depth: str = "5"
    exit_slippage: str = ".03"
    exit_notional: str = "5"
    inventory_skew: str = ".001"
    research_phase: bool = False
    research_phase_age_ms: int = 120000
    max_order_lifetime_ms: int = 60000
    quote_profile: str = "LEGACY"
    rotation_lookback_ms: int = 5000
    rotation_cooldown_ms: int = 5000
    rotation_jump: str = ".03"
    rotation_max_distance: str = ".05"
    rotation_retain_ticks: int = 1

    def __post_init__(self):
        super().__post_init__()
        from ..fair_value.maths import decimal
        if self.quote_profile not in {'LEGACY', 'INPLAY_ROTATION_V1'}:
            raise ValueError('unknown quote profile')
        if self.quote_profile != 'LEGACY' and not self.research_phase:
            raise ValueError('rotation is restricted to explicit Paper research')
        if not 1000 <= self.rotation_lookback_ms <= 60000 or not 1000 <= self.rotation_cooldown_ms <= 60000 or not 0 <= self.rotation_retain_ticks <= 3:
            raise ValueError('invalid rotation time/grid parameters')
        if not decimal('0') < decimal(self.rotation_jump) <= decimal('.2') or (self.quote_profile != 'LEGACY' and not decimal(self.quote_distance) <= decimal(self.rotation_max_distance) < decimal('.5')):
            raise ValueError('invalid rotation distances')
        if self.strategy_mode != "BOOK_DRIVEN" or self.initial_inventory != "0":
            raise ValueError("BookPolicy requires explicit BOOK_DRIVEN and cash-only initialization")
        if not 0 < self.close_remaining_s < self.reduce_remaining_s < 720 or self.sync_observations < 2:
            raise ValueError("invalid conservative phase parameters")
        if not 0 < float(self.quote_distance) < .5 or float(self.max_order_shares) <= 0:
            raise ValueError("invalid distance or share cap")
        if not 0 < self.manual_max_age_ms <= 15000:
            raise ValueError("manual authority must expire within 15 seconds")
        if not 1000 <= self.max_order_lifetime_ms <= 3600000 or not 1000 <= self.research_phase_age_ms <= 600000:
            raise ValueError('bounded research order/phase lifetime required')
