"""September shared-data, four-venue funding strategy replay.

Run from repository root: python funding/september/backtest.py
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
START=pd.Timestamp('2026-09-01T00:00:00Z')
END=pd.Timestamp('2026-09-30T23:00:00Z')
DATA=HERE.parents[1]/'datasets'/'2026-09'
FEES={'binance':5.0,'bybit':5.5,'okx':5.0,'hyperliquid':4.5}

@dataclass(frozen=True)
class Config:
    policy: str='original'
    oracle_proxy_multiplier: float=1.0
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
            threshold=2*(FEES[low.name]+FEES[high.name])+config.entry_buffer_bps if config.policy=='original' else 1.0
            item=dict(base=base,long_exchange=low.name,short_exchange=high.name,
                decision_time=decision,long_signal_time=low.source_time,short_signal_time=high.source_time,
                long_available_time=low.available_time,short_available_time=high.available_time,
                edge_8h_bps=float(edge),entry_threshold_bps=threshold,threshold_passed=bool(edge>threshold))
            # Prefer the largest qualifying gap, or retain the largest diagnostic gap.
            if best is None or (item['threshold_passed'],edge)>(best['threshold_passed'],best['edge_8h_bps']):best=item
        if best:result.append(best)
    return sorted(result,key=lambda r:(-r['edge_8h_bps'],r['base']))


def replay(funding,prices,config=Config(),start=START,end=END,rate_cache=None):
    if config.policy not in ('original','august_frozen'):
        raise ValueError('Unknown policy')
    if config.oracle_proxy_multiplier<=0:
        raise ValueError('Oracle proxy multiplier must be positive')
    rate_cache={} if rate_cache is None else rate_cache
    assert not funding.duplicated(['exchange','base','settlement_time']).any()
    assert not prices.duplicated(['exchange','base','time']).any()
    assert np.isfinite(funding.funding_rate).all()
    prices=prices.sort_values('time')
    funding=funding.sort_values('settlement_time')
    lookup={(r.time,r.exchange,r.base):r for r in prices.itertuples()}
    # Universe is fixed at the evaluation start, without full-window survival screening.
    initial_prices=prices[prices.time==start]
    eligible=set(zip(initial_prices.exchange,initial_prices.base))
    eligible_mask=np.array([(ex,base) in eligible for ex,base in zip(funding.exchange,funding.base)],dtype=bool)
    funding=funding.loc[eligible_mask].copy()
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
        if row.exchange=='hyperliquid':
            if candle is None or not np.isfinite(candle.trade_open) or candle.trade_open<=0:
                raise ValueError(f'Missing Hyperliquid notional proxy for {row.base} {row.settlement_time}')
            return float(candle.trade_open)*config.oracle_proxy_multiplier,'trade_open_oracle_proxy'
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
                exit_fee_per_unit=(lm*(1-slip)*FEES[le]+sm*(1+slip)*FEES[se])/1e4
                liquidation_cost_per_unit=2*slip*(lm+sm)+unit_fee+exit_fee_per_unit
                q=min(config.leg_notional/max(lm,sm),max(0,budget)/(lm+sm+liquidation_cost_per_unit))
                if q*(lm+sm)<100:continue
                opened[base]={**order,'entry_time':time,'quantity':q,
                    'long_entry_price':lp,'short_entry_price':sp,
                    'long_entry_reference':lm,'short_entry_reference':sm,
                    'entry_fees':q*unit_fee,'funding_pnl':0.0,'settlement_payments':0}
            pending=None
        if time==end:
            for base in list(opened):close(base,time,'sample_end')
        elif (config.policy!='original' or time.hour%8==0) and time+pd.Timedelta(hours=1)<end:
            cache_key=(time,config.publication_delay_minutes)
            if cache_key not in rate_cache:
                rate_cache[cache_key]=known_rates(funding,time,config)
            rates=rate_cache[cache_key]
            ranked=rank_pairs(rates,time,config)
            decisions.extend(ranked)
            current={(r.exchange,r.base):r.normalized_8h for r in rates.itertuples()} if not rates.empty else {}
            planned=[]
            top={r['base'] for r in ranked[:config.max_pairs]}
            by_base={r['base']:r for r in ranked}
            for base,p in opened.items():
                a=current.get((p['long_exchange'],base));b=current.get((p['short_exchange'],base))
                age=(time+pd.Timedelta(hours=1)-p['entry_time']).total_seconds()/3600
                if a is None or b is None:reason='signal_missing'
                elif config.policy!='original' and age<8:continue
                elif (b-a)*1e4<(config.exit_bps if config.policy=='original' else 1/3):reason='spread_narrowed'
                elif age>=config.max_hold_hours:reason='max_holding'
                elif config.policy!='original' and base not in top:reason='left_top_five'
                elif config.policy!='original' and base in by_base and (by_base[base]['long_exchange'],by_base[base]['short_exchange'])!=(p['long_exchange'],p['short_exchange']):reason='pair_changed'
                else:continue
                planned.append((base,reason))
            pending=dict(close=planned,open=[r for r in ranked if r['threshold_passed'] and (config.policy=='original' or r['base'] in top)])
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
        max_open_pairs=int(curve.open_pairs.max()),max_gross_exposure=float(curve.gross_exposure.max()),
        oracle_proxy_payments=sum(p['valuation_source']=='trade_open_oracle_proxy' for p in payments),
        candidate_count=len(decisions),
        threshold_pass_count=sum(r['threshold_passed'] for r in decisions),
        eligible_markets=sorted([list(k) for k in eligible]))
    payment_frame=pd.DataFrame(payments,columns=['base','exchange','settlement_time','entry_time','side',
        'quantity','funding_rate','reference_price','valuation_source','cash'])
    return ledger,curve,pd.DataFrame(decisions),payment_frame,summary



def load_inputs(directory=DATA):
    manifest=json.loads((directory/'dataset.json').read_text(encoding='utf-8'))
    if manifest['dataset_id']!='crypto-perpetuals-2026-09-v1':
        raise ValueError('Unexpected shared dataset version')
    for name,digest in manifest['hashes'].items():
        if hashlib.sha256((directory/name).read_bytes()).hexdigest()!=digest:
            raise ValueError(f'Shared data hash mismatch: {name}')
    if set(manifest['venues'])!=set(FEES) or manifest['excluded_bases']:
        raise ValueError('Unexpected common universe')
    funding=pd.read_csv(directory/'funding.csv')
    prices=pd.read_csv(directory/'candles.csv').rename(columns={'open_time':'time','open':'trade_open'})
    funding['settlement_time']=pd.to_datetime(funding.settlement_time,utc=True,format='mixed')
    prices['time']=pd.to_datetime(prices.time,utc=True,format='mixed')
    bases=manifest['common_bases']
    if set(funding.base)!=set(bases) or set(prices.base)!=set(bases):
        raise ValueError('CSV universe mismatch')
    expected=set(pd.date_range(START,END,freq='h'))
    for exchange in FEES:
        for base in bases:
            c=prices[(prices.exchange==exchange)&(prices.base==base)]
            f=funding[(funding.exchange==exchange)&(funding.base==base)]
            if len(c)!=720 or set(c.time)!=expected or not np.isfinite(c.trade_open).all() or not c.trade_open.gt(0).all():
                raise ValueError(f'Incomplete shared prices: {exchange}/{base}')
            hours=1 if exchange=='hyperliquid' else 8
            schedule=set(pd.date_range(START,pd.Timestamp(manifest['end_exclusive']),freq=f'{hours}h',inclusive='left'))
            if len(f)!=len(schedule) or set(f.settlement_time.dt.round('min'))!=schedule:
                raise ValueError(f'Incomplete shared funding: {exchange}/{base}')
    return funding,prices,manifest


def main(directory=HERE,data_directory=DATA):
    funding,prices,manifest=load_inputs(data_directory)
    directory.mkdir(parents=True,exist_ok=True)
    summaries=[];curves={};baselines={};rate_cache={}
    for policy in ('original','august_frozen'):
        for cost in (1.0,3.0,5.0):
            ledger,curve,decisions,payments,summary=replay(funding,prices,Config(policy=policy,fill_cost_bps=cost),rate_cache=rate_cache)
            summary['scenario']=f'{policy}_execution_{cost:g}bps'
            summaries.append(summary)
            if cost==3:
                for name,frame in [('trades',ledger),('equity',curve),('decisions',decisions),('payments',payments)]:
                    frame.to_csv(directory/f'{policy}_{name}.csv',index=False,lineterminator='\n')
                curves[policy]=curve;baselines[policy]=(decisions,payments,summary)
            print(f'{policy}, execution {cost:g}bps: {summary["trades"]} trades, net ${summary["net_pnl"]:.2f}',flush=True)
    sensitivities=[]
    for policy,(_,payments,summary) in baselines.items():
        hl=payments[payments.valuation_source=='trade_open_oracle_proxy'] if len(payments) else payments
        cash=float(hl.cash.sum()) if len(hl) else 0.0
        for multiplier in (.99,1.0,1.01):
            sensitivities.append(dict(policy=policy,oracle_proxy_multiplier=multiplier,
                funding_cash_adjustment=cash*(multiplier-1),net_pnl_fixed_trade_ledger=summary['net_pnl']+cash*(multiplier-1)))
    report=dict(dataset_id=manifest['dataset_id'],dataset_sha256=hashlib.sha256((data_directory/'dataset.json').read_bytes()).hexdigest(),
        input_sha256=manifest['hashes'],code_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        common_bases=manifest['common_bases'],venues=list(FEES),fees_bps=FEES,
        start_inclusive=START.isoformat(),data_end_exclusive=manifest['end_exclusive'],
        final_liquidation=END.isoformat(),results=summaries,oracle_proxy_fixed_ledger_sensitivity=sensitivities,
        warmup='Only September data; each venue/base requires two published past settlements. No prior-month backfill.',
        original='Every 8h; enter above roundtrip fees +2bps; exit below0.5bps or48h. Equal base quantities, next-hour fills.',
        august_frozen='Pre-existing comparison, not tuned on September: hourly top5; entry1bps/8h, minhold8h, exit below1/3bps or outside top5 or pair change, max48h.',
        signal='Latest settled funding available >=5m before decision. Native rate *8/observed interval used once for ranking; future actual rates excluded.',
        execution='Next hourly trade open plus adverse1/3/5bps per fill and taker fees; actual historical BBO unavailable.',
        funding_valuation='Binance endpoint settlement mark; Bybit/OKX hourly mark open; Hyperliquid hourly trade open as explicitly estimated oracle-notional proxy.',
        limitations=['Hyperliquid oracle is absent. Funding amounts and net PnL are estimates, not exact real-account performance; raw rates and traded prices are actual.',
            'Oracle sensitivity changes funding cash on the fixed3bps trade ledger; it does not rerun sizing or model dynamic oracle basis.',
            'Same-hour candles close/high/low/volume are never used for signals or fills. Trade open is an execution proxy, not a bid/ask quote.',
            'Universe completeness is known ex post; it defines this shared sample and is not a point-in-time universe construction.',
            'Stablecoins assumed at parity; liquidation, margin fragmentation, order minimums, impact and transfers unmodelled.',
            'Final exit uses September30 23:00 open; remaining last-hour events after exit are excluded.',
            'Publication latency assumed5min; actual historical API receipt times unavailable. No annualized performance claim.'])
    (directory/'results.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8',newline='\n')
    fig,axes=plt.subplots(3,1,figsize=(12,11),layout='constrained')
    for policy,curve in curves.items():
        axes[0].plot(pd.to_datetime(curve.time),curve.equity-100000,label=policy.replace('_',' '))
    axes[0].set(title='September 2026: four venues, same frozen data',ylabel='Estimated net PnL (USD)')
    axes[0].legend();axes[0].grid(alpha=.2)
    labels=['Funding (estimated)','Trade-price PnL','Execution penalty','Fees','Net']
    for i,(policy,(_,_,s)) in enumerate(baselines.items()):
        axes[1].bar(np.arange(5)+(i-.5)*.36,[s['funding_pnl'],s['reference_price_pnl'],-s['execution_cost'],-s['fees'],s['net_pnl']],.36,label=policy.replace('_',' '))
    axes[1].set_xticks(range(5),labels);axes[1].set(ylabel='USD',title='Base scenario: 3 bps penalty per fill + taker fees')
    axes[1].axhline(0,color='#555',linewidth=.8);axes[1].legend();axes[1].grid(axis='y',alpha=.2)
    original=baselines['original'][0]
    strongest=original.groupby('decision_time').edge_8h_bps.max()
    axes[2].plot(strongest.index,strongest.values,label='Largest known 8h rate gap',color='#2b8c7e')
    axes[2].axhline(21,color='#b65c60',linestyle='--',label='Lowest original entry threshold: 21 bps')
    axes[2].set(title='Original rule: entry-gap check',ylabel='8h-equivalent basis points')
    axes[2].legend();axes[2].grid(alpha=.2)
    fig.savefig(directory/'performance.png',dpi=150,metadata={'Software':'Research'})
    plt.close(fig)
    print('DONE: September shared-data replay, six scenarios, oracle-proxy accounting sensitivity.',flush=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data',type=Path,default=DATA)
    parser.add_argument('--output',type=Path,default=HERE)
    args=parser.parse_args()
    main(args.output,args.data)
