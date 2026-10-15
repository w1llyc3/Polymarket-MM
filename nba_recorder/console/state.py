"""Owner-thread projections: display reads never instantiate an OMS/writer."""
import json
from decimal import Decimal
from ..api import utc_ms
from ..fill_management.ledger import valuation
from ..inplay.report import execution_evidence

REASONS = {
    "disconnected": "当前连接不可用，禁止新增报价", "unreliable_sports_state": "体育阶段字段未通过核验",
    "sports_stale_or_future": "体育源时间缺失、过期或来自未来", "invalid_clock": "比赛时钟未知或无效",
    "phase_not_permitted": "当前阶段不允许报价", "stale_or_future_data": "盘口或元数据已过期",
    "processing_backlog": "事件处理积压", "late_close_threshold": "进入实验关闭窗口",
    "late_reduce_boundary": "进入实验减仓窗口", "closed_latch": "已关闭阶段持久锁定",
    "unverified_game_identity": "缺少可信比赛阶段与身份映射", "risk_control_HALTED": "风控已熔断，需要核对与人工处置",
    "public_one_sided_flow_diagnostic": "公共单边流量诊断，不代表自身成交", "insufficient_unreserved_cash": "可用现金不足",
    "insufficient_unreserved_inventory": "可用库存不足", "maker_only_reject": "报价可能立即成交，拒绝挂单",
    "intent_expired": "订单意图已过期", "runner_stopped_keep_halted": "会话停止并保持熔断",
}


def explain(code):
    return {"code": code, "zh": REASONS.get(code, "安全门未通过 / 诊断事件，请展开原始原因")}


def snapshot(control):
    runner = control.runner
    if not runner:
        return {"generated_ms": utc_ms(), "orders": [], "books": [], "account": None, "risk_state": "UNKNOWN", "phases": [], "decisions": [], "logs": [], "execution_evidence": None}
    om = runner.om
    frames = list(runner.latest.values())
    marks = {}
    from ..inplay.reference import observed_mid
    for f in frames:
        mid = observed_mid(f, om.policy, om.now)
        if mid is not None:
            marks[f["asset_id"]] = mid
    state = om.ledger.state()
    account = valuation(state, marks, om.orders())
    assets = list(account["positions"])
    quantities = [Decimal(account["positions"][a]) for a in assets]
    account["paired_shares"] = str(min(quantities)) if len(quantities) == 2 else None
    account["net_directional_shares"] = str(quantities[0]-quantities[1]) if len(quantities) == 2 else None
    account["directional_asset_order"] = assets
    books = []
    for f in frames:
        b = f.get("book") or {}
        label = next((o["label"] for o in f["market"]["outcomes"] if o["asset_id"] == f["asset_id"]), f["asset_id"])
        books.append({"asset_id": f["asset_id"], "label": label, "book_ref": f.get("book_ref"), "market_id": f["market_id"],
                      "book": b, "received_ms": f.get("book_received_ms"), "processed_ms": f.get("processed_ms"),
                      "connected_now": runner.authorize(f)["connected"], "runtime_permission": runner.authorize(f)["runtime_permission"],
                      "observed_connected": f.get("observed_connected", f.get("connected")), "max_age_ms": om.policy.book_max_age_ms})
    decisions = [json.loads(r[0]) for r in om.db.execute("SELECT payload FROM pe_quotes ORDER BY rowid DESC LIMIT 4")]
    logs = []
    for r in om.db.execute("SELECT seq,time_ms,kind,order_id,payload FROM pe_journal ORDER BY seq DESC LIMIT 150"):
        payload = json.loads(r[4])
        kind = r[2]
        logs.append({"seq": r[0], "time_ms": r[1], "kind": kind, "order_id": r[3], "detail": payload.get("detail"),
                     "severity": "ERROR" if kind in {"KILL_SWITCH", "SERVICE_RESTART_UNKNOWN"} else "WARN" if kind in {"RISK", "LOCAL_REJECTION", "DISPATCH_RISK_REJECTED"} else "INFO"})
    phases = [json.loads(r[0]) for r in om.db.execute("SELECT payload FROM ip_phase")]
    incomes = [dict(r) for r in om.db.execute("SELECT kind,stage,amount,market_id FROM ip_income ORDER BY rowid")]
    return {"generated_ms": utc_ms(), "paper_clock_ms": om.now, "risk_state": om.risk_state, "account": account,
            "books": books, "orders": [{**o, "remaining": str(Decimal(o["size"])-Decimal(o["filled"]))} for o in om.orders()],
            "phases": phases, "decisions": decisions, "logs": logs, "income_stages": incomes,
            "inventory_ages": [{"asset_id": l["asset_id"], "remaining": l["remaining"], "age_ms": om.now-l["time_ms"]} for l in state["lots"] if Decimal(l["remaining"]) > 0],
            "risk_limits": {"order_notional": om.policy.max_order_notional, "event_shares": om.policy.max_event_risk, "account_shares": om.policy.max_account_risk},
            "execution_evidence": execution_evidence(om), "watchdog_ticks": runner.ticks,
            "reason_dictionary": REASONS, "real_submission_calls": 0}
