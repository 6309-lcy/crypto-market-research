"""January-March real-data, lagged-settlement funding strategy.

Run from repository root: python funding/jan_mar/backtest.py
Signals use only past settlements with a five-minute publication allowance.
Execution uses the next hourly trade OPEN plus an explicit cost scenario.
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
import argparse
from itertools import combinations
import hashlib
import json
from pathlib import Path
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

HERE=Path(__file__).resolve().parent
START=pd.Timestamp('2026-01-24T08:00:00Z')
END=pd.Timestamp('2026-03-31T16:00:00Z')
FEES={'binance':5.0,'bybit':5.5,'okx':5.0}

@dataclass(frozen=True)
class Config:
    capital: float=100_000.0
    leg_notional: float=10_000.0
    max_pairs: int=5
    reserve: float=1_000.0
    entry_buffer_bps: float=2.0
    exit_bps: float=.5
    max_hold_hours: int=48
    publication_delay_minutes: int=5
    fill_cost_bps: float=3.0


def known_rates(funding,decision,config):
    """Infer each interval from consecutive already-known settlement events."""
    known=funding[funding.settlement_time<=decision-pd.Timedelta(minutes=config.publication_delay_minutes)]
    rows=[]
    for (exchange,base),g in known.groupby(['exchange','base'],sort=True):
        g=g.sort_values('settlement_time')
        if len(g)<2:continue
        latest,previous=g.iloc[-1],g.iloc[-2]
        elapsed=(latest.settlement_time-previous.settlement_time).total_seconds()/3600
        interval=min([1,2,4,8],key=lambda h:abs(h-elapsed))
        if abs(elapsed-interval)>2/3600:continue
        age=(decision-latest.settlement_time).total_seconds()/3600
        if age>interval+config.publication_delay_minutes/60:continue
        rows.append(dict(exchange=exchange,base=base,funding_rate=float(latest.funding_rate),
            normalized_8h=float(latest.funding_rate)*8/interval,interval_hours=interval,
            source_time=latest.settlement_time,
            available_time=latest.settlement_time+pd.Timedelta(minutes=config.publication_delay_minutes)))
    return pd.DataFrame(rows)


def rank_pairs(rates,decision,config):
    if rates.empty:return []
    result=[]
    for base,g in rates.groupby('base',sort=True):
        g=g.set_index('exchange')
        best=None
        for ea,eb in combinations(sorted(g.index),2):
            a,b=g.loc[ea],g.loc[eb]
            low,high=(a,b) if a.normalized_8h<=b.normalized_8h else (b,a)
            edge=(high.normalized_8h-low.normalized_8h)*1e4
            threshold=2*(FEES[low.name]+FEES[high.name])+config.entry_buffer_bps
            item=dict(base=base,long_exchange=low.name,short_exchange=high.name,
                decision_time=decision,long_signal_time=low.source_time,short_signal_time=high.source_time,
                long_available_time=low.available_time,short_available_time=high.available_time,
                edge_8h_bps=float(edge),entry_threshold_bps=threshold,threshold_passed=bool(edge>threshold))
            # Prefer the largest qualifying gap, or retain the largest diagnostic gap.
            if best is None or (item['threshold_passed'],edge)>(best['threshold_passed'],best['edge_8h_bps']):best=item
        if best:result.append(best)
    return sorted(result,key=lambda r:(-r['edge_8h_bps'],r['base']))


def validate_coverage(funding,prices,start=START,end=END):
    """Reject incomplete retained contracts; never silently create zero funding."""
    first=prices[prices.time==start]
    for exchange,base in sorted(set(zip(first.exchange,first.base))):
        g=prices[(prices.exchange==exchange)&(prices.base==base)&prices.time.between(start,end)]
        expected=pd.date_range(start,end,freq='h')
        if set(g.time)!=set(expected) or not np.isfinite(g[['trade_open','mark_open']].values).all():
            raise ValueError(f'Incomplete hourly prices: {exchange}/{base}')
        f=funding[(funding.exchange==exchange)&(funding.base==base)].sort_values('settlement_time')
        gaps=f.settlement_time.diff().dt.total_seconds().dropna()/3600
        if len(f)<3 or not gaps.map(lambda h:min(abs(h-x) for x in (1,2,4,8))<2/3600).all():
            raise ValueError(f'Unverified or missing settlements: {exchange}/{base}')
        boundary=f.settlement_time.dt.floor('min')
        # Eight-hour boundaries are common to the observed 1/2/4/8h schedules.
        required=pd.date_range(start,end,freq='8h')
        if not set(required).issubset(set(boundary)):
            raise ValueError(f'Missing settlement boundary: {exchange}/{base}')


def replay(funding,prices,config=Config(),start=START,end=END):
    assert not funding.duplicated(['exchange','base','settlement_time']).any()
    assert not prices.duplicated(['exchange','base','time']).any()
    assert np.isfinite(funding.funding_rate).all()
    prices=prices.sort_values('time')
    funding=funding.sort_values('settlement_time')
    lookup={(r.time,r.exchange,r.base):r for r in prices.itertuples()}
    # Universe is fixed at the evaluation start, without full-window survival screening.
    initial_prices=prices[prices.time==start]
    initial_funding=funding[funding.settlement_time<start]
    eligible=set(zip(initial_prices.exchange,initial_prices.base)) & set(zip(initial_funding.exchange,initial_funding.base))
    funding=funding[[(ex,base) in eligible for ex,base in zip(funding.exchange,funding.base)]]
    def price_at(time,ex,base):
        row=lookup.get((time,ex,base))
        if row is None or not np.isfinite(row.trade_open) or row.trade_open<=0:
            raise ValueError(f'Missing trade-price proxy: {ex}/{base} {time}')
        return float(row.trade_open)
    def funding_price(row):
        mark=getattr(row,'settlement_mark_price',np.nan)
        if pd.notna(mark) and np.isfinite(mark) and mark>0:
            return float(mark),'endpoint_settlement_mark'
        candle=lookup.get((row.settlement_time.floor('h'),row.exchange,row.base))
        if candle is None or not np.isfinite(candle.mark_open) or candle.mark_open<=0:
            raise ValueError(f'Missing funding mark proxy: {row.exchange}/{row.base} {row.settlement_time}')
        if row.settlement_time-row.settlement_time.floor('h')>pd.Timedelta(seconds=2):
            raise ValueError('Settlement not close enough to a mark-price candle open')
        return float(candle.mark_open),'hourly_mark_open_proxy'
    opened={};pending=None;trades=[];payments=[];decisions=[];equity=[]
    realized=0.0
    slip=config.fill_cost_bps/1e4
    def liquidation(p,time):
        lm=price_at(time,p['long_exchange'],p['base']);sm=price_at(time,p['short_exchange'],p['base'])
        lp=lm*(1-slip);sp=sm*(1+slip)
        pnl=p['quantity']*((lp-p['long_entry_price'])+(p['short_entry_price']-sp))
        exit_fee=p['quantity']*(lp*FEES[p['long_exchange']]+sp*FEES[p['short_exchange']])/1e4
        return p['funding_pnl']+pnl-p['entry_fees']-exit_fee,lm+sm
    def close(base,time,reason):
        nonlocal realized
        p=opened.pop(base)
        lm=price_at(time,p['long_exchange'],base);sm=price_at(time,p['short_exchange'],base)
        lp=lm*(1-slip);sp=sm*(1+slip);q=p['quantity']
        price_pnl=q*((lp-p['long_entry_price'])+(p['short_entry_price']-sp))
        reference_pnl=q*((lm-p['long_entry_reference'])+(p['short_entry_reference']-sm))
        fees=p['entry_fees']+q*(lp*FEES[p['long_exchange']]+sp*FEES[p['short_exchange']])/1e4
        row={**p,'exit_time':time,'long_exit_price':lp,'short_exit_price':sp,
            'reference_price_pnl':reference_pnl,'execution_cost':reference_pnl-price_pnl,
            'price_pnl':price_pnl,'fees':fees,'net_pnl':p['funding_pnl']+price_pnl-fees,'exit_reason':reason}
        realized+=row['net_pnl'];trades.append(row)
    previous=start
    for time in pd.date_range(start,end,freq='h'):
        # Old holdings receive only actual events strictly after the preceding valuation.
        settled=funding[(funding.settlement_time>previous)&(funding.settlement_time<=time)]
        for row in settled.itertuples():
            p=opened.get(row.base)
            if p is None:continue
            if row.exchange==p['long_exchange']:side=1
            elif row.exchange==p['short_exchange']:side=-1
            else:continue
            if row.settlement_time<=p['entry_time']:continue
            mark,source=funding_price(row)
            cash=-side*p['quantity']*mark*row.funding_rate
            p['funding_pnl']+=cash;p['settlement_payments']+=1
            payments.append(dict(base=row.base,exchange=row.exchange,settlement_time=row.settlement_time,
                entry_time=p['entry_time'],side=side,quantity=p['quantity'],funding_rate=row.funding_rate,
                reference_price=mark,valuation_source=source,cash=cash))
        if pending is not None:
            for base,reason in pending['close']:
                if base in opened:close(base,time,reason)
            for order in pending['open']:
                base=order['base'];le=order['long_exchange'];se=order['short_exchange']
                if base in opened or len(opened)>=config.max_pairs:continue
                lm=price_at(time,le,base);sm=price_at(time,se,base)
                liquid=0.0;gross=0.0
                for p in opened.values():
                    value,price_sum=liquidation(p,time);liquid+=value;gross+=p['quantity']*price_sum
                lp=lm*(1+slip);sp=sm*(1-slip)
                unit_fee=(lp*FEES[le]+sp*FEES[se])/1e4
                budget=config.capital+realized+liquid-config.reserve-gross
                q=min(config.leg_notional/max(lm,sm),max(0,budget)/(max(lm+sm,lp+sp)+unit_fee))
                if q*(lm+sm)<100:continue
                opened[base]={**order,'entry_time':time,'quantity':q,
                    'long_entry_price':lp,'short_entry_price':sp,
                    'long_entry_reference':lm,'short_entry_reference':sm,
                    'entry_fees':q*unit_fee,'funding_pnl':0.0,'settlement_payments':0}
            pending=None
        if time==end:
            for base in list(opened):close(base,time,'sample_end')
        elif time.hour%8==0 and time+pd.Timedelta(hours=1)<end:
            rates=known_rates(funding,time,config)
            ranked=rank_pairs(rates,time,config)
            decisions.extend(ranked)
            current={(r.exchange,r.base):r.normalized_8h for r in rates.itertuples()} if not rates.empty else {}
            planned=[]
            for base,p in opened.items():
                a=current.get((p['long_exchange'],base));b=current.get((p['short_exchange'],base))
                age=(time+pd.Timedelta(hours=1)-p['entry_time']).total_seconds()/3600
                if a is None or b is None:reason='signal_missing'
                elif (b-a)*1e4<config.exit_bps:reason='spread_narrowed'
                elif age>=config.max_hold_hours:reason='max_holding'
                else:continue
                planned.append((base,reason))
            pending=dict(close=planned,open=[r for r in ranked if r['threshold_passed']])
        value=0.0;gross=0.0
        for p in opened.values():
            v,price_sum=liquidation(p,time);value+=v;gross+=p['quantity']*price_sum
        equity.append(dict(time=time,equity=config.capital+realized+value,realized_pnl=realized,
            unrealized_liquidation_pnl=value,open_pairs=len(opened),gross_exposure=gross))
        previous=time
    columns=['base','long_exchange','short_exchange','decision_time','entry_time','quantity',
        'long_entry_price','short_entry_price','long_entry_reference','short_entry_reference',
        'entry_fees','funding_pnl','settlement_payments','exit_time','long_exit_price','short_exit_price',
        'reference_price_pnl','execution_cost','price_pnl','fees','net_pnl','exit_reason']
    ledger=pd.DataFrame(trades) if trades else pd.DataFrame(columns=columns)
    curve=pd.DataFrame(equity)
    totals={c:float(ledger[c].sum()) for c in ['funding_pnl','reference_price_pnl','execution_cost','price_pnl','fees','net_pnl']}
    assert abs(totals['funding_pnl']+totals['price_pnl']-totals['fees']-totals['net_pnl'])<1e-6
    assert abs(curve.iloc[-1].equity-config.capital-totals['net_pnl'])<1e-6
    peak=curve.equity.cummax().clip(lower=config.capital)
    summary=dict(config=asdict(config),start=start.isoformat(),end=end.isoformat(),trades=len(ledger),
        **totals,return_pct=totals['net_pnl']/config.capital*100,
        max_drawdown_pct=float((curve.equity/peak-1).min()*100),
        max_open_pairs=int(curve.open_pairs.max()),candidate_count=len(decisions),
        threshold_pass_count=sum(r['threshold_passed'] for r in decisions),
        eligible_markets=sorted([list(k) for k in eligible]))
    payment_frame=pd.DataFrame(payments,columns=['base','exchange','settlement_time','entry_time','side',
        'quantity','funding_rate','reference_price','valuation_source','cash'])
    return ledger,curve,pd.DataFrame(decisions),payment_frame,summary


def main(directory=HERE):
    funding=pd.read_csv(directory/'funding.csv');prices=pd.read_csv(directory/'prices.csv')
    funding['settlement_time']=pd.to_datetime(funding.settlement_time,unit='ms',utc=True) if pd.api.types.is_numeric_dtype(funding.settlement_time) else pd.to_datetime(funding.settlement_time,utc=True,format='mixed')
    prices['time']=pd.to_datetime(prices.time,utc=True,format='mixed')
    if 'settlement_mark_price' not in funding:funding['settlement_mark_price']=np.nan
    validate_coverage(funding,prices)
    summaries=[];curves={}
    for cost in [1.0,3.0,5.0]:
        ledger,curve,decisions,payments,summary=replay(funding,prices,Config(fill_cost_bps=cost))
        summaries.append(summary);curves[cost]=curve
        if cost==3:
            for name,df in [('trades',ledger),('equity',curve),('decisions',decisions),('payments',payments)]:
                df.to_csv(directory/f'{name}.csv',index=False,lineterminator='\n')
        print(f'Execution penalty {cost:g}bps: {summary["trades"]} trades, net ${summary["net_pnl"]:.2f}',flush=True)
    report=dict(model='Actual-data lagged-settlement funding strategy; original thresholds with explicit public-data limitations.',
        venues=list(FEES),fees_bps=FEES,results=summaries,
        input_sha256={name:hashlib.sha256((directory/name).read_bytes()).hexdigest() for name in ['funding.csv','prices.csv']},
        code_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        signal='Latest actual settlement available at least 5m before decision; interval inferred from two past events. No historical predicted-rate archive.',
        execution='Next hourly traded-price open, one hour after each decision, plus 1/3/5bps adverse penalty per fill; no historical BBO claim.',
        limitations=['Binance, Bybit and OKX actual data. Hyperliquid price/oracle history is absent; the earlier four-venue portfolio is not reproduced.',
            'Candle opens are price proxies; spread/impact modeled by sensitivity, not observed fills.',
            'Bybit funding valuation uses hourly mark open; Binance uses the endpoint mark when supplied.',
            'Publication delay is assumed at 5m; historical response receipt times are unavailable.',
            'USD stablecoins at parity; no venue liquidation, lot constraints or transfer model.'])
    (directory/'results.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8',newline='\n')
    fig,axes=plt.subplots(3,1,figsize=(12,11),layout='constrained')
    for cost,curve in curves.items():
        axes[0].plot(pd.to_datetime(curve.time),curve.equity-100000,label=f'{cost:g} bps per-fill penalty')
    axes[0].set(title='Jan 24-Mar 31, 2026: actual funding, Binance + Bybit + OKX',ylabel='Net PnL (USD)')
    axes[0].legend();axes[0].grid(alpha=.2)
    s=summaries[1]
    axes[1].bar(['Funding','Reference-price PnL','Execution penalty','Fees','Net'],
        [s['funding_pnl'],s['reference_price_pnl'],-s['execution_cost'],-s['fees'],s['net_pnl']],
        color=['#2b8c7e','#5279a7','#cc8751','#b65c60','#394e70'])
    axes[1].set(title='Base scenario: 3 bps per fill, plus taker fees',ylabel='USD')
    axes[1].axhline(0,color='#555',linewidth=.8);axes[1].grid(axis='y',alpha=.2)
    if not decisions.empty:
        strongest=decisions.groupby('decision_time').edge_8h_bps.max()
        axes[2].plot(strongest.index,strongest.values,label='Largest known 8h gap across 15 coins',color='#2b8c7e')
        axes[2].axhline(22,color='#b65c60',linestyle='--',label='Minimum entry threshold: 22 bps (pair dependent)')
        axes[2].set(title='Observed funding gaps versus entry threshold',ylabel='8h-equivalent basis points')
        axes[2].legend();axes[2].grid(alpha=.2)
    fig.savefig(directory/'performance.png',dpi=150,metadata={'Software':'Research'})
    plt.close(fig)
    print('DONE: real-data January-March replay.',flush=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',type=Path,default=HERE)
    main(parser.parse_args().directory)
