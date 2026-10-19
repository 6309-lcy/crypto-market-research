"""Fixed low-turnover September research; run this file to regenerate five artifacts.

Accounting is copied from ../september/backtest.py; loader is imported unchanged.
Rates and settlement ownership use raw timestamps; nominal rounding is signal-only.
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
from itertools import combinations
from pathlib import Path
import argparse
import hashlib
import importlib.util
import json
import sys
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

HERE = Path(__file__).resolve().parent
ENGINE_PATH = HERE.parent / 'september' / 'backtest.py'
_spec = importlib.util.spec_from_file_location('september_accounting_engine', ENGINE_PATH)
engine = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = engine
_spec.loader.exec_module(engine)
FEES = engine.FEES
DATA = engine.DATA
START = pd.Timestamp('2026-09-15T00:00:00Z')
END = engine.END

@dataclass(frozen=True)
class Config:
    capital: float = 100_000.0
    leg_notional: float = 10_000.0
    max_pairs: int = 5
    reserve: float = 1_000.0
    fill_cost_bps: float = 3.0
    publication_delay_minutes: int = 5
    history_hours: int = 72
    horizon_hours: int = 72
    entry_buffer_bps: float = 5.0
    exit_buffer_bps: float = 2.0
    min_hold_hours: int = 24
    max_hold_hours: int = 168
    cooldown_hours: int = 24
    oracle_proxy_multiplier: float = 1.0


def forecast_pairs(funding, decision, cfg=Config()):
    """Both directions of exact aligned historical cash-rate differences, in bps."""
    decision = pd.Timestamp(decision)
    if cfg.history_hours != 72 or cfg.horizon_hours != 72:
        raise ValueError('This frozen policy requires 72h history and horizon')
    cutoff = decision - pd.Timedelta(minutes=cfg.publication_delay_minutes)
    bucket_end = cutoff.floor('8h')
    bucket_start = bucket_end - pd.Timedelta(hours=72)
    known = funding.loc[funding.settlement_time <= cutoff].copy()
    if known.empty:
        return []
    known['nominal'] = known.settlement_time.dt.round('h')
    if ((known.settlement_time - known.nominal).abs() > pd.Timedelta(seconds=2)).any():
        raise ValueError('Settlement timestamp exceeds nominal rounding tolerance')
    buckets = pd.date_range(bucket_start, bucket_end, freq='8h', inclusive='left')
    venues = {}
    for (base, exchange), group in known.groupby(['base', 'exchange'], sort=True):
        interval = 1 if exchange == 'hyperliquid' else 8
        selected = group[(group.nominal >= bucket_start) & (group.nominal < bucket_end)].sort_values('nominal')
        expected = pd.date_range(bucket_start, bucket_end, freq=f'{interval}h', inclusive='left')
        # Exact schedules enforce full backcoverage, beyond the minimum 6/48 events.
        if len(selected) < (48 if interval == 1 else 6):
            continue
        if len(selected) != len(expected) or list(selected.nominal) != list(expected):
            continue
        selected = selected.copy()
        selected['bucket'] = selected.nominal.dt.floor('8h')
        counts = selected.groupby('bucket').size().reindex(buckets, fill_value=0)
        if not counts.eq(8 // interval).all():
            continue
        sums = selected.groupby('bucket').funding_rate.sum().reindex(buckets).to_numpy(dtype=float)
        if not np.isfinite(sums).all():
            raise ValueError('Nonfinite historical rates')
        venues[(base, exchange)] = (sums, selected.settlement_time.max(), len(selected))
    result = []
    for base in sorted({key[0] for key in venues}):
        exchanges = sorted(ex for b, ex in venues if b == base)
        for first, second in combinations(exchanges, 2):
            for le, se in ((first, second), (second, first)):
                long, lt, ln = venues[(base, le)]
                short, st, sn = venues[(base, se)]
                differences = (short - long) * 1e4
                q25 = float(np.quantile(differences, .25, method='linear'))
                positive = int((differences > 0).sum())
                consistent = positive >= 8 and q25 > 0
                threshold = 2 * (FEES[le] + FEES[se]) + 4 * cfg.fill_cost_bps + cfg.entry_buffer_bps
                result.append(dict(base=base, long_exchange=le, short_exchange=se,
                    decision_time=decision, history_start=bucket_start, history_end=bucket_end,
                    bucket_available_time=bucket_end + pd.Timedelta(minutes=cfg.publication_delay_minutes),
                    long_signal_time=lt, short_signal_time=st,
                    long_available_time=lt + pd.Timedelta(minutes=cfg.publication_delay_minutes),
                    short_available_time=st + pd.Timedelta(minutes=cfg.publication_delay_minutes),
                    long_event_count=ln, short_event_count=sn, bucket_count=9,
                    long_mean_8h_bps=float(long.mean()*1e4), short_mean_8h_bps=float(short.mean()*1e4),
                    mean_spread_8h_bps=float(differences.mean()), q25_8h_bps=q25,
                    positive_buckets=positive, consistency_passed=consistent,
                    expected_72h_bps=q25 * 9, expected_24h_bps=q25 * 3,
                    entry_threshold_bps=threshold,
                    threshold_passed=bool(consistent and q25 * 9 >= threshold)))
    return sorted(result, key=lambda r: (-r['expected_72h_bps'], r['base'], r['long_exchange'], r['short_exchange']))


def cached_forecasts(funding, decision, cfg, cache):
    # Execution costs do not affect the historical forecast.
    key = (decision, cfg.history_hours, cfg.publication_delay_minutes)
    if key not in cache:
        cache[key] = forecast_pairs(funding, decision, cfg)
    result = []
    for original in cache[key]:
        row = original.copy()
        threshold = 2*(FEES[row['long_exchange']] + FEES[row['short_exchange']]) + 4*cfg.fill_cost_bps + cfg.entry_buffer_bps
        row['entry_threshold_bps'] = threshold
        row['threshold_passed'] = bool(row['consistency_passed'] and row['expected_72h_bps'] >= threshold)
        result.append(row)
    return result


def replay(funding,prices,config=Config(),start=START,end=END,rate_cache=None):
    if config.oracle_proxy_multiplier <= 0:
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
    cooldown={}; reversal={}
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
        cooldown[base]=time+pd.Timedelta(hours=config.cooldown_hours)
        reversal.pop(base,None)
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
                if base in opened or len(opened)>=config.max_pairs or time < cooldown.get(base,start):continue
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
        elif time+pd.Timedelta(hours=1)<=end:
            ranked=cached_forecasts(funding,time,config,rate_cache)
            current={(r['base'],r['long_exchange'],r['short_exchange']):r for r in ranked}
            planned=[]
            for base,p in opened.items():
                signal=current.get((base,p['long_exchange'],p['short_exchange']))
                age=(time+pd.Timedelta(hours=1)-p['entry_time']).total_seconds()/3600
                reversed_now=signal is not None and signal['mean_spread_8h_bps']<0
                reversal[base]=reversal.get(base,0)+1 if reversed_now else 0
                future_exit=FEES[p['long_exchange']]+FEES[p['short_exchange']]+2*config.fill_cost_bps+config.exit_buffer_bps
                if age>=config.max_hold_hours:reason='max_holding'
                elif age<config.min_hold_hours:continue
                elif signal is None:reason='history_unavailable'
                elif reversal[base]>=3:reason='direction_reversed_3_decisions'
                elif signal['expected_24h_bps']<future_exit:reason='insufficient_24h_cost_cover'
                else:continue
                planned.append((base,reason))
            closing={base for base,_ in planned}
            horizon_ok=time+pd.Timedelta(hours=1+config.horizon_hours)<=end
            orders=[]
            selected=set(opened)
            for r in ranked:
                base=r['base']
                state=('held' if base in opened else
                    'cooldown' if time+pd.Timedelta(hours=1)<cooldown.get(base,start) else
                    'insufficient_time' if not horizon_ok else
                    'forecast_rejected' if not r['threshold_passed'] else 'eligible')
                if state=='eligible':
                    if base in selected:state='other_pair_selected'
                    elif len(selected)>=config.max_pairs:state='capacity'
                    else:
                        state='scheduled_entry';orders.append(r.copy());selected.add(base)
                record={**r,'action':state,'exit_scheduled':base in closing,
                    'horizon_available':horizon_ok,'fill_time':time+pd.Timedelta(hours=1)}
                decisions.append(record)
            pending=dict(close=planned,open=orders)
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
        eligible_markets=sorted([list(k) for k in eligible]),
        mean_fee_per_fill_bps=None)
    payment_frame=pd.DataFrame(payments,columns=['base','exchange','settlement_time','entry_time','side',
        'quantity','funding_rate','reference_price','valuation_source','cash'])
    fill_notional=sum(float(p['quantity'])*(p['long_entry_price']+p['short_entry_price']+p['long_exit_price']+p['short_exit_price']) for p in trades)
    summary['mean_fee_per_fill_bps']=totals['fees']/fill_notional*1e4 if fill_notional else None
    summary['mean_hold_hours']=float(((ledger.exit_time-ledger.entry_time).dt.total_seconds()/3600).mean()) if len(ledger) else None
    summary['payment_rows']=len(payment_frame)
    assert abs(float(payment_frame.cash.sum())-totals['funding_pnl'])<1e-6
    assert not opened
    if len(ledger):
        assert ((ledger.exit_time-ledger.entry_time).dt.total_seconds()/3600<=config.max_hold_hours).all()
        for base,g in ledger.groupby('base'):
            g=g.sort_values('entry_time')
            assert all(g.entry_time.iloc[i]>=g.exit_time.iloc[i-1]+pd.Timedelta(hours=config.cooldown_hours) for i in range(1,len(g)))
    return ledger,curve,pd.DataFrame(decisions),payment_frame,summary


def strongest(rows):
    valid = [r for r in rows if r['consistency_passed']]
    chosen = max(valid or rows, key=lambda r: r['expected_72h_bps'], default=None)
    if chosen is None:
        return None
    return {**chosen, 'cost_cover_margin_bps': chosen['expected_72h_bps']-chosen['entry_threshold_bps'],
            'consistent_candidate_count': len(valid)}


def main(data_directory=DATA):
    print('Validating shared dataset SHA256, full price and settlement coverage...', flush=True)
    funding, prices, manifest = engine.load_inputs(data_directory)
    cfg = Config()
    cache = {}
    development = []
    evaluation_rows = []
    earliest = None
    for decision in pd.date_range(engine.START, END-pd.Timedelta(hours=1), freq='h'):
        rows = cached_forecasts(funding, decision, cfg, cache)
        if rows and earliest is None:
            earliest = decision
        if decision < START:
            development.extend(rows)
        else:
            evaluation_rows.extend(rows)
        if decision.hour == 0:
            print(f'Forecast cache {decision.date()}: {len(rows)} directional pairs', flush=True)
    summaries = []
    frames = {name: [] for name in ('trades', 'equity', 'decisions', 'payments')}
    curves = {}
    sensitivity = []
    for cost in (1.0, 3.0, 5.0):
        scenario = f'low_turnover_execution_{cost:g}bps'
        config = Config(fill_cost_bps=cost)
        ledger, curve, decisions, payments, summary = replay(funding, prices, config, rate_cache=cache)
        summary['scenario'] = scenario
        summary['strongest_forecast_vs_cost'] = strongest(decisions.to_dict('records'))
        summaries.append(summary)
        curves[cost] = curve
        for name, frame in zip(frames, (ledger, curve, decisions, payments)):
            frame = frame.copy()
            frame.insert(0, 'scenario', scenario)
            frames[name].append(frame)
        if cost == 3:
            cash = float(payments.loc[payments.valuation_source=='trade_open_oracle_proxy', 'cash'].sum())
            for multiplier in (.99, 1.0, 1.01):
                sensitivity.append(dict(oracle_proxy_multiplier=multiplier,
                    funding_cash_adjustment=cash*(multiplier-1),
                    net_pnl_fixed_trade_ledger=summary['net_pnl']+cash*(multiplier-1)))
        print(f'{scenario}: {len(ledger)} trades, funding ${summary["funding_pnl"]:.2f}, '
              f'fees ${summary["fees"]:.2f}, net ${summary["net_pnl"]:.2f}', flush=True)
    output_rows = {}
    for name, parts in frames.items():
        frame = pd.concat(parts, ignore_index=True)
        frame.to_csv(HERE/f'{name}.csv', index=False, lineterminator='\n')
        output_rows[name] = len(frame)
    comparison_path = ENGINE_PATH.parent/'results.json'
    comparison = json.loads(comparison_path.read_text(encoding='utf-8'))
    original = [r for r in comparison['results'] if r['config']['policy']=='original']
    report = dict(
        dataset_id=manifest['dataset_id'],
        dataset_sha256=hashlib.sha256((data_directory/'dataset.json').read_bytes()).hexdigest(),
        input_sha256=manifest['hashes'], code_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        accounting_engine_sha256=hashlib.sha256(ENGINE_PATH.read_bytes()).hexdigest(),
        accounting_provenance='Loader imported unchanged using importlib; raw-event cash, actual-notional fees, equal-base sizing, next-open execution, and liquidation accounting copied from September engine.',
        input_rows=dict(funding=len(funding), prices=len(prices)), output_rows=output_rows,
        common_bases=manifest['common_bases'], fees_bps=FEES, frozen_config=asdict(cfg),
        development=dict(start=engine.START, end=START-pd.Timedelta(hours=1),
            mode='Warmup and forecast diagnostics only; no development positions, no parameter selection or fitting.',
            earliest_forecast=earliest, directional_candidate_rows=len(development),
            consistent_rows=sum(r['consistency_passed'] for r in development),
            threshold_pass_rows=sum(r['threshold_passed'] for r in development),
            strongest_forecast_vs_cost=strongest(development)),
        evaluation=dict(start=START, end=END, flat_start=True,
            status='Already-seen September month; this is not a new holdout.',
            last_entry_decision=END-pd.Timedelta(hours=73), final_exit=END),
        rules=dict(
            forecast='Latest 9 fully published consecutive UTC 8h buckets: [start,end), available only at end+5min. Nominal event timestamps rounded to hour only within 2 seconds. Exact 8 hourly Hyperliquid events or 1 CEX event per bucket; no missing or duplicate nominal events. Full72h aligned bucket backcoverage; minimum48 hourly/6 eight-hour events is exceeded by exact72/9. Per-venue mean raw8h=sum(hourly rates)/9 or mean(CEX8h rates).',
            orientation='Evaluate both directions separately: short bucket sum minus long bucket sum. Require8/9 strictly positive differences and positive linear25th percentile. No median comparison or independent venue quantiles.',
            entry='Expected72h=9*q25 bps of ONE leg; require >=2*(feeLong+feeShort)+4*fillPenalty+5bps. Fill next hourly open; require at least72h from fill to final exit.',
            hold='Minimum24h, maximum168h measured at next fill. After24h exit if3*q25 <= feeLong+feeShort+2*fillPenalty+2bps, or mean directional spread is negative for3 consecutive hourly decisions. Missing full history after minimum hold also exits defensively. No rank exit.',
            switching='One position per coin. Retain until exit rule; cooldown24h from actual exit, no same-hour reentry. Ranking only allocates unused entry capacity.',
            sizing='Fixed100k initial capital,10k maximum per leg, at most5 pairs,1k reserve; equal base quantities and engine collateral budget including actual current liquidation costs. No net-liquidation stop.',
            settlement_ownership='Raw timestamps only: entry_time < settlement_time <= exit_time. Process settlements before scheduled exit/entry fills; entry-at-settlement owns no event, exit-at-settlement owns it.',
            execution='Adverse1/3/5bps on each of four fills plus fees on actual executed notional; no fitted or optimized parameters.',
            funding_valuation='Binance endpoint mark; Bybit/OKX hourly mark open; Hyperliquid trade-open oracle-notional proxy, estimated cash and not actual account receipts.'),
        results=summaries,
        original_comparison=dict(source=str(comparison_path.relative_to(HERE.parent)),
            sha256=hashlib.sha256(comparison_path.read_bytes()).hexdigest(),
            scope='Existing original strategy full September results; not rerun. Different evaluation window and warmup, so not a matched flat-start comparison.',
            results=original),
        oracle_proxy_fixed_ledger_sensitivity=sensitivity,
        verification=['Shared loader validated all dataset hashes and720 price hours per market and exact nominal funding schedules.',
            'Replay asserts unique raw events and prices, finite raw funding, cash-to-trade reconciliation, trade-to-terminal-equity reconciliation, maximum hold, cooldown and final flat holdings.',
            'Signal construction requires all9 bucket schedules and verifies every constituent event is published before the decision.'],
        limitations=comparison['limitations']+[
            'Conservative historical lower quartile is a forecast, not a guaranteed future funding receipt. Costs use one-leg bps approximations for selection; ledger fees use actual notionals.',
            'No fitted parameters or profit optimization. Three execution scenarios are frozen assumptions; combined CSVs identify each scenario.',
            'Development diagnostics do not represent an independent trading backtest; evaluation is previously observed data.'])
    (HERE/'results.json').write_text(json.dumps(report, indent=2, default=str, allow_nan=False)+'\n', encoding='utf-8', newline='\n')
    fig, axes = plt.subplots(3, 1, figsize=(12, 11), layout='constrained')
    for cost, curve in curves.items():
        axes[0].plot(curve.time, curve.equity-cfg.capital, label=f'{cost:g} bps/fill')
    axes[0].set(title='Fixed low-turnover strategy | Sep15–30, 2026 | already-seen data', ylabel='Estimated net PnL (USD)')
    axes[0].legend()
    labels = ['Funding estimate', 'Reference price PnL', 'Execution', 'Fees', 'Net']
    for i, summary in enumerate(summaries):
        axes[1].bar(np.arange(5)+(i-1)*.25,
            [summary['funding_pnl'], summary['reference_price_pnl'], -summary['execution_cost'], -summary['fees'], summary['net_pnl']],
            .25, label=f'{summary["config"]["fill_cost_bps"]:g} bps/fill')
    axes[1].set_xticks(range(5), labels)
    axes[1].set(ylabel='USD', title='Funding, basis moves and all trading costs')
    axes[1].legend()
    if evaluation_rows:
        diagnostics = pd.DataFrame(evaluation_rows)
        consistent = diagnostics[diagnostics.consistency_passed].copy()
        if len(consistent):
            consistent['margin'] = consistent.expected_72h_bps-consistent.entry_threshold_bps
            best = consistent.groupby('decision_time').margin.max()
            axes[2].plot(best.index, best.values, label='Best consistent72h forecast minus cost+buffer (3bps/fill)')
    axes[2].axhline(0, color='black', linewidth=.8)
    axes[2].set(title='Entry cost coverage; all values relative to one leg', ylabel='Basis points')
    if axes[2].lines and len(axes[2].lines)>1:
        axes[2].legend()
    for ax in axes:
        ax.grid(alpha=.2)
    fig.savefig(HERE/'performance.png', dpi=150, metadata={'Software':'Research'})
    plt.close(fig)
    print('DONE: five artifacts; three frozen execution cases; no parameter fitting.', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, default=DATA)
    args = parser.parse_args()
    main(args.data)
