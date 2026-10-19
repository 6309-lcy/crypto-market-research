"""Causal August funding replay using observed signals and actual settlements.

Run: python funding/august_backtest.py
Funding normalization is used ONLY for ranking; settled payments use raw rates.
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
from itertools import combinations
import hashlib
import json
from pathlib import Path
import argparse
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

HERE = Path(__file__).resolve().parent
SPLIT = pd.Timestamp('2026-08-13T00:00:00Z')
FEES = {'binance':5.0, 'bybit':5.5, 'okx':5.0, 'hyperliquid':4.5}

@dataclass(frozen=True)
class Config:
    policy: str = 'original'
    capital: float = 100_000.0
    leg_notional: float = 10_000.0
    max_positions: int = 5
    slippage_bps: float = 1.0
    entry_buffer_8h_bps: float = 2.0
    exit_8h_bps: float = 0.5
    max_hold_hours: int = 48
    reserve: float = 1_000.0


def timestamp(value):
    return pd.Timestamp(value)


def normalize_8h(rate, interval_hours):
    if not np.isfinite(interval_hours) or interval_hours <= 0:
        raise ValueError('An observed positive funding interval is required')
    return float(rate) * 8.0 / float(interval_hours)


def candidates(signals, decision, config):
    """Only information available at the decision may influence selection."""
    frame = signals[(signals.observed_time <= decision) &
                    (signals.observed_time >= decision-pd.Timedelta(minutes=5))].copy()
    frame = frame.dropna(subset=['funding_rate','interval_hours'])
    frame = frame[frame.interval_hours > 0]
    rows=[]
    for base,g in frame.groupby('base'):
        best=None
        for (_,a),(_,b) in combinations(g.sort_values('exchange').iterrows(),2):
            ra=normalize_8h(a.funding_rate,a.interval_hours)
            rb=normalize_8h(b.funding_rate,b.interval_hours)
            long,short=(a,b) if ra<=rb else (b,a)
            edge=abs(rb-ra)*1e4
            item=dict(base=base,long_exchange=long.exchange,short_exchange=short.exchange,
                      edge_8h_bps=edge,signal_time=decision.isoformat(),
                      long_signal_time=long.observed_time.isoformat(),
                      short_signal_time=short.observed_time.isoformat())
            if best is None or edge>best['edge_8h_bps']:
                best=item
        if best:
            rows.append(best)
    return sorted(rows,key=lambda r:(-r['edge_8h_bps'],r['base'],r['long_exchange'],r['short_exchange']))


def settlement_cash(events, base, exchange, side, quantity, entry_time, exit_time, valuations):
    """Pay raw rates on base quantity times the venue's settlement reference price."""
    selected=events[(events.base==base)&(events.exchange==exchange)&
                    (events.settlement_time>entry_time)&(events.settlement_time<=exit_time)]
    total=0.0
    for row in selected.itertuples():
        mark=getattr(row,'mark_price',np.nan)
        if not pd.notna(mark) or mark<=0:
            # Funding endpoints do not always include historical mark prices.
            # Use the archived mark or midpoint observed just before this event.
            eligible=valuations[(valuations.base==base)&(valuations.exchange==exchange)&
                (valuations.observed_time<=row.settlement_time)&
                (valuations.observed_time>=row.settlement_time-pd.Timedelta(minutes=5))]
            if exchange=='hyperliquid' and 'oracle_price' in eligible.columns:
                prices=eligible.oracle_price
            else:
                prices=eligible['mark_price'].where(eligible['mark_price'].gt(0),eligible['mid'])
            eligible=eligible.assign(settlement_price=prices)
            eligible=eligible[np.isfinite(eligible.settlement_price) & eligible.settlement_price.gt(0)]
            if eligible.empty:
                raise ValueError(f'Missing settlement valuation: {exchange}/{base} {row.settlement_time}')
            mark=eligible.sort_values('observed_time').iloc[-1].settlement_price
        if not np.isfinite(mark) or mark<=0:
            raise ValueError('Invalid settlement valuation')
        amount=-side*quantity*float(mark)*float(row.funding_rate)
        total+=amount
    return total,int(len(selected))


def price_components(quantity, long_entry, short_entry, long_exit, short_exit):
    return quantity*((long_exit-long_entry)+(short_entry-short_exit))


def quote(books, exchange, base, decision):
    subset=books[(books.exchange==exchange)&(books.base==base)]
    if subset.empty:
        return None
    row=subset.sort_values('observed_time').iloc[0]
    delay=(row.observed_time-decision).total_seconds()
    if not 60<=delay<=180 or not 0<row.best_bid<=row.best_ask:
        return None
    return row


def validate_inputs(observations, events):
    if observations.empty or events.empty:
        raise ValueError('Observations and actual settlement outcomes are required')
    signal=observations[observations.role=='signal']
    assert (signal.observed_time<=signal.decision_time).all(), 'Future signal in observations'
    assert not observations.duplicated(['decision_time','exchange','base','role']).any()
    assert not events.duplicated(['exchange','base','settlement_time']).any()
    assert np.isfinite(events.funding_rate).all()
    assert observations.decision_time.min()>=pd.Timestamp('2026-08-01T00:00:00Z')
    assert observations.decision_time.max()<pd.Timestamp('2026-09-01T00:00:00Z')
    # An archived scheduled settlement must have an actual endpoint record.
    existing=set((r.exchange,r.base,int(r.settlement_time.timestamp()//60)) for r in events.itertuples())
    lo=observations.decision_time.min(); hi=observations.decision_time.max()
    missing=set()
    signal=signal.sort_values(['exchange','base','decision_time']).copy()
    signal['next_observation']=signal.groupby(['exchange','base']).decision_time.shift(-1).fillna(hi)
    for r in signal.itertuples():
        if r.exchange=='hyperliquid':
            expected=r.decision_time.ceil('h')
            if expected==r.decision_time: expected+=pd.Timedelta(hours=1)
        elif pd.notna(r.next_settlement_ms):
            expected=pd.Timestamp(r.next_settlement_ms,unit='ms',tz='UTC')
        else:
            continue
        interval=1.0 if r.exchange=='hyperliquid' else r.interval_hours
        cutoff=min(r.next_observation,hi)
        while True:
            if lo<expected<=hi and (r.exchange,r.base,int(expected.timestamp()//60)) not in existing:
                missing.add((r.exchange,r.base,expected.isoformat()))
            if not pd.notna(interval) or interval<=0:
                break
            expected+=pd.Timedelta(hours=float(interval))
            if expected>cutoff:
                break
    if missing:
        raise ValueError(f'Actual settlements missing ({len(missing)}), examples: {sorted(missing)[:8]}')


def run(observations, events, config=Config(), start=SPLIT, validate=True):
    if validate:
        validate_inputs(observations,events)
    end=observations.decision_time.max()
    signals=observations[observations.role=='signal']
    books=observations[observations.role=='execution']
    valuations=observations[observations.role.isin(['valuation','signal'])].copy()
    # A signal observation can supply a historical mark price, with book midpoint fallback.
    open_positions={}; trades=[]; decisions=[]; equity=[]; realized=0.0
    counters=dict(candidate_opportunities=0,entry_threshold_passed=0,missing_quotes=0,capacity_rejections=0)
    def mark_position(p, time, lb, sb):
        lc,ln=settlement_cash(events,p['base'],p['long_exchange'],1,p['quantity'],p['long_entry_time'],time,valuations)
        sc,sn=settlement_cash(events,p['base'],p['short_exchange'],-1,p['quantity'],p['short_entry_time'],time,valuations)
        long_exit=lb.best_bid*(1-config.slippage_bps/1e4)
        short_exit=sb.best_ask*(1+config.slippage_bps/1e4)
        price=price_components(p['quantity'],p['long_entry_price'],p['short_entry_price'],long_exit,short_exit)
        exit_fees=p['quantity']*(long_exit*FEES[p['long_exchange']]+short_exit*FEES[p['short_exchange']])/1e4
        return lc+sc+price-p['entry_fees']-exit_fees,ln+sn
    def close(p,decision,lb,sb,reason):
        nonlocal realized
        q=p['quantity']; slip=config.slippage_bps/1e4
        long_exit=lb.best_bid*(1-slip); short_exit=sb.best_ask*(1+slip)
        lc,ln=settlement_cash(events,p['base'],p['long_exchange'],1,q,p['long_entry_time'],lb.observed_time,valuations)
        sc,sn=settlement_cash(events,p['base'],p['short_exchange'],-1,q,p['short_entry_time'],sb.observed_time,valuations)
        fees=p['entry_fees']+q*(long_exit*FEES[p['long_exchange']]+short_exit*FEES[p['short_exchange']])/1e4
        price=price_components(q,p['long_entry_price'],p['short_entry_price'],long_exit,short_exit)
        mid_pnl=price_components(q,p['long_entry_mid'],p['short_entry_mid'],lb.mid,sb.mid)
        record={**p,'exit_decision':decision.isoformat(),'long_exit_time':lb.observed_time.isoformat(),
                'short_exit_time':sb.observed_time.isoformat(),'long_exit_price':long_exit,'short_exit_price':short_exit,
                'funding_pnl':lc+sc,'mid_price_pnl':mid_pnl,'execution_cost':mid_pnl-price,
                'price_pnl':price,'fees':fees,'net_pnl':lc+sc+price-fees,
                'settlement_payments':ln+sn,'exit_reason':reason}
        trades.append(record); realized+=record['net_pnl']
        del open_positions[p['base']]
    for decision in sorted(observations.decision_time.unique()):
        decision=pd.Timestamp(decision)
        if decision<start: continue
        frame=signals[(signals.decision_time==decision) &
                      (signals.observed_time<=decision) &
                      (signals.observed_time>=decision-pd.Timedelta(minutes=5))]
        fills=books[books.decision_time==decision]
        ranked=candidates(frame,decision,config)
        by_base={r['base']:r for r in ranked}
        final=decision==end
        evaluate=config.policy!='original' or decision.hour%8==0
        top={r['base'] for r in ranked[:config.max_positions]}
        for base,p in list(open_positions.items()):
            lb=quote(fills,p['long_exchange'],base,decision);sb=quote(fills,p['short_exchange'],base,decision)
            reason=None
            age=(decision-p['entry_decision']).total_seconds()/3600
            current=frame[frame.base==base].set_index('exchange')
            edge=None
            if all(ex in current.index and pd.notna(current.loc[ex,'interval_hours']) for ex in (p['long_exchange'],p['short_exchange'])):
                edge=(normalize_8h(current.loc[p['short_exchange'],'funding_rate'],current.loc[p['short_exchange'],'interval_hours'])-
                      normalize_8h(current.loc[p['long_exchange'],'funding_rate'],current.loc[p['long_exchange'],'interval_hours']))*1e4
            if final:reason='sample_end'
            elif evaluate:
                if edge is None: reason='signal_missing'
                elif config.policy=='original':
                    if edge<config.exit_8h_bps:reason='spread_narrowed'
                    elif age>=config.max_hold_hours:reason='max_holding'
                elif age>=8:
                    best=by_base.get(base)
                    if edge*3<1:reason='spread_narrowed'
                    elif base not in top:reason='left_top_five'
                    elif best and (best['long_exchange'],best['short_exchange'])!=(p['long_exchange'],p['short_exchange']):reason='pair_changed'
                    elif age>=config.max_hold_hours:reason='max_holding'
            if reason and lb is not None and sb is not None:
                close(p,decision,lb,sb,reason)
            elif reason:
                counters['missing_quotes']+=1
                if final:raise ValueError(f'Cannot liquidate {base} at sample end')
        if evaluate and not final:
            for item in ranked:
                counters['candidate_opportunities']+=1
                base=item['base'];le=item['long_exchange'];se=item['short_exchange']
                rt=2*(FEES[le]+FEES[se])
                threshold=rt+config.entry_buffer_8h_bps if config.policy=='original' else 1.0
                accepted=item['edge_8h_bps']>threshold if config.policy=='original' else item['edge_8h_bps']>=threshold
                decisions.append({**item,'policy':config.policy,'round_trip_fee_bps':rt,'threshold_8h_bps':threshold,'threshold_passed':accepted})
                if not accepted or base in open_positions:continue
                counters['entry_threshold_passed']+=1
                if config.policy!='original' and base not in top:continue
                if len(open_positions)>=config.max_positions:
                    counters['capacity_rejections']+=1;continue
                lb=quote(fills,le,base,decision);sb=quote(fills,se,base,decision)
                if lb is None or sb is None:
                    counters['missing_quotes']+=1;continue
                available=config.capital+realized-config.reserve-sum(p['entry_gross'] for p in open_positions.values())
                q=min(config.leg_notional/max(lb.mid,sb.mid),max(0,available)/(lb.mid+sb.mid))
                # Use realized losses already incurred; never expand budget with hypothetical funding.
                existing_gross=0.0
                existing_liquidation_pnl=0.0
                for existing in open_positions.values():
                    existing_long=quote(fills,existing['long_exchange'],existing['base'],decision)
                    existing_short=quote(fills,existing['short_exchange'],existing['base'],decision)
                    if existing_long is None or existing_short is None:
                        raise ValueError(f'Missing hourly valuation for open {existing["base"]} at {decision}')
                    existing_gross+=existing['quantity']*(existing_long.mid+existing_short.mid)
                    marked,_=mark_position(existing,min(existing_long.observed_time,existing_short.observed_time),existing_long,existing_short)
                    existing_liquidation_pnl+=marked
                fill_notional_per_unit=lb.best_ask*(1+config.slippage_bps/1e4)+sb.best_bid*(1-config.slippage_bps/1e4)
                entry_fee_per_unit=(lb.best_ask*(1+config.slippage_bps/1e4)*FEES[le]+
                                    sb.best_bid*(1-config.slippage_bps/1e4)*FEES[se])/1e4
                budget=config.capital+realized+existing_liquidation_pnl-config.reserve-existing_gross
                q=min(q,max(0,budget)/(max(lb.mid+sb.mid,fill_notional_per_unit)+entry_fee_per_unit))
                if q*(lb.mid+sb.mid)<100:
                    counters['capacity_rejections']+=1;continue
                slip=config.slippage_bps/1e4
                lep=lb.best_ask*(1+slip);sep=sb.best_bid*(1-slip)
                fees=q*(lep*FEES[le]+sep*FEES[se])/1e4
                p={**item,'entry_decision':decision,'long_entry_time':lb.observed_time,
                   'short_entry_time':sb.observed_time,'quantity':q,'long_entry_price':lep,'short_entry_price':sep,
                   'long_entry_mid':lb.mid,'short_entry_mid':sb.mid,'entry_fees':fees,'entry_gross':q*(lb.mid+sb.mid)}
                open_positions[base]=p
        unrealized=0.0;gross=0.0
        for base,p in open_positions.items():
            lb=quote(fills,p['long_exchange'],base,decision);sb=quote(fills,p['short_exchange'],base,decision)
            if lb is None or sb is None:
                raise ValueError(f'Missing hourly valuation for open {base} at {decision}')
            mark_time=min(lb.observed_time,sb.observed_time)
            amount,_=mark_position(p,mark_time,lb,sb)
            unrealized+=amount;gross+=p['quantity']*(lb.mid+sb.mid)
        equity.append(dict(time=(decision+pd.Timedelta(minutes=3)).isoformat(),
            realized_pnl=realized,unrealized_pnl=unrealized,equity=config.capital+realized+unrealized,
            open_pairs=len(open_positions),gross_exposure=gross))
    assert not open_positions
    result=pd.DataFrame(trades)
    if result.empty:
        result=pd.DataFrame(columns=['base','long_exchange','short_exchange','entry_decision',
            'long_entry_time','short_entry_time','quantity','long_entry_price','short_entry_price',
            'exit_decision','long_exit_time','short_exit_time','funding_pnl','mid_price_pnl',
            'execution_cost','price_pnl','fees','net_pnl','settlement_payments','exit_reason'])
    curve=pd.DataFrame(equity)
    components={c:float(result[c].sum()) if len(result) else 0.0 for c in ['funding_pnl','mid_price_pnl','execution_cost','price_pnl','fees','net_pnl']}
    assert abs(components['funding_pnl']+components['price_pnl']-components['fees']-components['net_pnl'])<1e-6
    assert abs(curve.iloc[-1].equity-config.capital-components['net_pnl'])<1e-6
    peak=curve.equity.cummax().clip(lower=config.capital)
    summary=dict(policy=config.policy,config=asdict(config),start=pd.Timestamp(start).isoformat(),
        last_decision=end.isoformat(),trades=len(result),**components,
        return_pct=components['net_pnl']/config.capital*100,
        max_drawdown_pct=float(((curve.equity/peak)-1).min()*100),
        max_open_pairs=int(curve.open_pairs.max()),max_gross_exposure=float(curve.gross_exposure.max()),
        **counters)
    return result,curve,pd.DataFrame(decisions),summary


def load_inputs(directory=HERE):
    obs=pd.read_csv(directory/'august_observations.csv')
    events=pd.read_csv(directory/'august_settlements.csv')
    for c in ['decision_time','observed_time','collected_time']:obs[c]=pd.to_datetime(obs[c],utc=True,format='mixed')
    events['settlement_time']=pd.to_datetime(events.settlement_time,utc=True,format='mixed')
    if 'mark_price' not in events:events['mark_price']=np.nan
    # Freeze the executable market universe using books received before evaluation.
    warmup=obs[(obs.role=='execution') & (obs.observed_time<SPLIT)]
    eligible=set(zip(warmup.exchange,warmup.base))
    keep=[(exchange,base) in eligible for exchange,base in zip(obs.exchange,obs.base)]
    excluded=sorted(set(zip(obs.exchange,obs.base))-eligible)
    print(f'Pre-evaluation book-universe exclusions: {excluded}',flush=True)
    obs=obs[keep].copy()
    validate_inputs(obs,events)
    return obs,events


def main(directory=HERE):
    obs,events=load_inputs(directory)
    summaries=[]; curves={}
    for policy in ['original','august_frozen']:
        for slippage in [0.0,1.0,3.0]:
            config=Config(policy=policy,slippage_bps=slippage)
            trades,curve,decisions,summary=run(obs,events,config)
            summary['scenario']=f'{policy}_slip_{slippage:g}bps'
            summaries.append(summary)
            if slippage==1:
                prefix=directory/('august_original' if policy=='original' else 'august_frozen')
                trades.to_csv(str(prefix)+'_trades.csv',index=False,lineterminator='\n')
                curve.to_csv(str(prefix)+'_equity.csv',index=False,lineterminator='\n')
                decisions.to_csv(str(prefix)+'_decisions.csv',index=False,lineterminator='\n')
                curves[policy]=curve
            print(f'{policy}, slippage {slippage:g}bps: {summary["trades"]} trades, net ${summary["net_pnl"]:.2f}',flush=True)
    hashes={name:hashlib.sha256((directory/name).read_bytes()).hexdigest() for name in ['august_observations.csv','august_settlements.csv']}
    report=dict(input_sha256=hashes,code_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        split_time=SPLIT.isoformat(),fees_bps=FEES,results=summaries,
        settlement_policy='Actual raw rates; q * settlement reference price. Hyperliquid requires archived spot oracle; CEX use endpoint mark or archived pre-settlement mark/mid within 5m.',
        execution_policy='Observed BBO 60-180s after signal, plus per-fill adverse slippage scenario; equal base quantity; taker fees on actual fill notionals.',
        limitations=['BBO execution plus assumed slippage; queue, market impact and historical contract lot constraints are unmodelled.',
            'USDT, USDC and USD assumed at parity. No exchange insolvency or collateral liquidation model.',
            'Funding notional uses actual historical mark when supplied; otherwise a fresh archived mark/mid proxy.',
            'Eight-day holdout already existed in earlier research; this rerun is a chronological replay, not a new unseen holdout.',
            'Gross capital cap is an allocation constraint; venue-specific margin rules and transfers are unmodelled.'])
    (directory/'august_results.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    fig,axes=plt.subplots(2,1,figsize=(12,8),layout='constrained')
    for policy,curve in curves.items():
        axes[0].plot(pd.to_datetime(curve.time),curve.equity-100000,label=policy.replace('_',' '))
    axes[0].set(title='August actual-settlement replay: net PnL',ylabel='USD, $100,000 initial capital')
    axes[0].legend();axes[0].grid(alpha=.2)
    base=[s for s in summaries if s['config']['slippage_bps']==1]
    labels=['Funding','Mid-price PnL','Spread + slippage','Fees','Net']
    for i,s in enumerate(base):
        values=[s['funding_pnl'],s['mid_price_pnl'],-s['execution_cost'],-s['fees'],s['net_pnl']]
        axes[1].bar(np.arange(5)+(i-.5)*.36,values,.36,label=s['policy'].replace('_',' '))
    axes[1].set_xticks(range(5),labels);axes[1].set(ylabel='USD',title='Out-of-sample accounting, 1 bps adverse slippage per fill')
    axes[1].axhline(0,color='#555',linewidth=.8);axes[1].legend();axes[1].grid(axis='y',alpha=.2)
    fig.savefig(directory/'august_backtest_results.png',dpi=150,metadata={'Software':'Research'})
    plt.close(fig)
    print('DONE: actual-settlement August replay and sensitivity scenarios completed.',flush=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',type=Path,default=HERE)
    main(parser.parse_args().directory)
