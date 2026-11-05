"""Price/time priority, midpoint pricing, and atomic internal energy allocation."""
import math
from uuid import uuid5, NAMESPACE_URL
from sqlalchemy import select
from .models import SimulationRun, Participant, MarketOrder, Trade


def settle_step(session_factory, run_id, step, proposals, *, available_kwh, permitted_buy_kwh, before_commit=None):
    """Serialize a run's order book with a SQL row lock; one transaction per step.

    Participant budgets are verified before settlement. This is internal allocation
    of existing physical renewable energy, never extra generation or grid imports.
    """
    if isinstance(step,bool) or not isinstance(step,int) or step<0:
        raise ValueError('Market step must be a nonnegative integer')
    if not all(isinstance(value,(float,int)) and not isinstance(value,bool) and math.isfinite(value) and value>=0
               for value in [available_kwh,permitted_buy_kwh]):
        raise ValueError('Available energy and purchasing budgets must be finite nonnegative numbers')
    for proposal in proposals:
        if not isinstance(proposal,dict) or not {'participant','side','quantity_kwh','limit_price'}<=proposal.keys():
            raise ValueError('Incomplete market proposal')
        if proposal['side'] not in {'buy','sell'} or not all(math.isfinite(float(proposal[key])) for key in ['quantity_kwh','limit_price']):
            raise ValueError('Invalid market side or non-finite numerical value')
        if proposal['quantity_kwh'] <= 0 or proposal['limit_price'] < 0:
            raise ValueError('Orders need positive quantities and nonnegative prices')
    sell_total = sum(p['quantity_kwh'] for p in proposals if p['side'] == 'sell')
    buy_total = sum(p['quantity_kwh'] for p in proposals if p['side'] == 'buy')
    if sell_total > available_kwh+1e-6 or buy_total > permitted_buy_kwh+1e-6:
        raise ValueError('Market proposals exceed available renewable energy or permitted served demand')
    identities = [(p['participant'],p['side']) for p in proposals]
    if len(identities) != len(set(identities)):
        raise ValueError('Duplicate participant order in the same step')
    with session_factory.begin() as session:
        run = session.scalar(select(SimulationRun).where(SimulationRun.id == run_id).with_for_update())
        if not run:
            raise ValueError('Unknown run reference')
        if step>=run.total_steps:
            raise ValueError('Market step is outside this simulation run')
        existing = list(session.scalars(select(MarketOrder).where(MarketOrder.run_id == run_id, MarketOrder.step == step)))
        participants = {p.key:p for p in session.scalars(select(Participant))}
        if existing:
            keys = {participant.id:key for key,participant in participants.items()}
            previous = {(keys[o.participant_id],o.side):(o.quantity_kwh,o.limit_price) for o in existing}
            incoming = {(p['participant'],p['side']):(p['quantity_kwh'],p['limit_price']) for p in proposals}
            if previous.keys()!=incoming.keys() or any(not all(math.isclose(a,b,rel_tol=1e-9,abs_tol=1e-9)
                for a,b in zip(previous[key],incoming[key])) for key in previous):
                raise ValueError('Idempotency conflict: this market step already committed different orders')
            return  # Already committed: idempotent whole-step replay.
        orders = []
        for index, proposal in enumerate(proposals):
            participant = participants.get(proposal['participant'])
            if not participant:
                raise ValueError('Unknown participant')
            # The solar cooperative owns the modelled renewable allocation.
            if proposal['side'] == 'sell' and participant.key != 'solar_coop':
                raise ValueError('This participant has no renewable seller allocation')
            order = MarketOrder(id=str(uuid5(NAMESPACE_URL,f'{run_id}:{step}:{participant.key}:{proposal["side"]}')),
                                run_id=run_id,step=step,participant_id=participant.id,side=proposal['side'],
                                quantity_kwh=proposal['quantity_kwh'],remaining_kwh=proposal['quantity_kwh'],
                                limit_price=proposal['limit_price'])
            session.add(order)
            orders.append((index,order))
        session.flush()
        buys = [o for _,o in sorted(orders,key=lambda pair:(-pair[1].limit_price,pair[0])) if o.side == 'buy']
        sells = [o for _,o in sorted(orders,key=lambda pair:(pair[1].limit_price,pair[0])) if o.side == 'sell']
        for buy in buys:
            for sell in sells:
                if buy.remaining_kwh <= 1e-9:
                    break
                if sell.remaining_kwh <= 1e-9 or buy.limit_price < sell.limit_price or buy.participant_id == sell.participant_id:
                    continue
                quantity = min(buy.remaining_kwh,sell.remaining_kwh)
                buy.remaining_kwh = max(0,buy.remaining_kwh-quantity)
                sell.remaining_kwh = max(0,sell.remaining_kwh-quantity)
                session.add(Trade(id=str(uuid5(NAMESPACE_URL,f'{buy.id}:{sell.id}')),run_id=run_id,step=step,
                                  buy_order_id=buy.id,sell_order_id=sell.id,quantity_kwh=quantity,
                                  price=(buy.limit_price+sell.limit_price)/2))
        for _, order in orders:
            order.status = 'filled' if order.remaining_kwh <= 1e-9 else ('partial' if order.remaining_kwh < order.quantity_kwh else 'open')
        session.flush()
        if before_commit:
            before_commit()  # Fault-injection seam used to verify rollback.
