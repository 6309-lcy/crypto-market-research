"""Independently validate the shared September 2026 dataset and freeze its universe.

Run: python datasets/2026-09/validate_data.py
Missing markets fail validation; source values are never filled or interpolated.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import numpy as np
import pandas as pd

HERE=Path(__file__).resolve().parent
START=pd.Timestamp('2026-09-01T00:00:00Z')
END=pd.Timestamp('2026-10-01T00:00:00Z')
VENUES=('binance','bybit','okx','hyperliquid')
BASES='ADA ARB ATOM AVAX BNB BTC DOGE DOT ETH LINK LTC OP SOL UNI XRP'.split()


def validate(directory=HERE):
    candles=pd.read_csv(directory/'candles.csv')
    funding=pd.read_csv(directory/'funding.csv')
    candles['open_time']=pd.to_datetime(candles.open_time,utc=True,format='mixed')
    funding['settlement_time']=pd.to_datetime(funding.settlement_time,utc=True,format='mixed')
    assert not candles.empty and not funding.empty
    assert set(candles.exchange)<=set(VENUES) and set(funding.exchange)<=set(VENUES)
    assert set(candles.base)<=set(BASES) and set(funding.base)<=set(BASES)
    assert not candles.duplicated(['exchange','base','open_time']).any(), 'Duplicate candle key'
    assert not funding.duplicated(['exchange','base','settlement_time']).any(), 'Duplicate funding key'
    assert candles.open_time.between(START,END,inclusive='left').all()
    assert funding.settlement_time.between(START,END,inclusive='left').all()
    assert np.isfinite(funding.funding_rate).all()
    assert np.isfinite(candles[['open','high','low','close']].values).all()
    assert (candles[['open','high','low','close']]>0).all().all()
    assert (candles.high>=candles[['open','close','low']].max(axis=1)).all()
    assert (candles.low<=candles[['open','close','high']].min(axis=1)).all()
    assert candles.open_time.eq(candles.open_time.dt.floor('h')).all()
    expected=pd.date_range(START,END,freq='h',inclusive='left')
    rows=[]
    for base in BASES:
        for exchange in VENUES:
            c=candles[(candles.base==base)&(candles.exchange==exchange)]
            f=funding[(funding.base==base)&(funding.exchange==exchange)].sort_values('settlement_time')
            missing_hours=sorted(set(expected)-set(c.open_time))
            complete_candles=not missing_hours and len(c)==720
            gaps=f.settlement_time.diff().dt.total_seconds().dropna()/3600
            interval=None;missing_settlements=[];irregular=[]
            if len(gaps):
                rounded=gaps.map(lambda h:min((1,2,4,8),key=lambda x:abs(x-h)))
                interval=int(rounded.mode().iloc[0])
                irregular=[float(g) for g in gaps if abs(g-interval)>2/3600]
                expected_settlements=pd.date_range(START,END,freq=f'{interval}h',inclusive='left')
                observed_nominal=set(f.settlement_time.dt.round('min'))
                missing_settlements=sorted(set(expected_settlements)-observed_nominal)
            complete_funding=bool(interval) and not missing_settlements and not irregular
            rows.append(dict(base=base,exchange=exchange,candles=len(c),funding_events=len(f),
                complete_candles=complete_candles,interval_hours=interval,
                missing_candle_hours=len(missing_hours),missing_settlements=len(missing_settlements),
                irregular_settlement_gaps=len(irregular),complete_funding=complete_funding,
                mark_open_rows=int(c.mark_open.notna().sum()) if 'mark_open' in c else 0,
                eligible=bool(complete_candles and complete_funding)))
    coverage=pd.DataFrame(rows)
    common=[base for base in BASES if coverage[coverage.base==base].eligible.all()]
    assert common, 'No complete common four-venue coin universe'
    coverage.to_csv(directory/'coverage.csv',index=False,lineterminator='\n')
    report=dict(dataset_id='crypto-perpetuals-2026-09-v1',start_inclusive=START.isoformat(),
        end_exclusive=END.isoformat(),timezone='UTC',venues=list(VENUES),candidate_bases=BASES,
        common_bases=common,excluded_bases=[b for b in BASES if b not in common],
        candle_frequency='1h',expected_candles_per_market=720,
        common_market_count=len(common)*4,
        common_candle_rows=int(candles[candles.base.isin(common)].shape[0]),
        common_funding_rows=int(funding[funding.base.isin(common)].shape[0]),
        hashes={name:hashlib.sha256((directory/name).read_bytes()).hexdigest()
            for name in ['candles.csv','funding.csv','coverage.csv']},
        validator_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        rule='Use only common_bases at all four venues and only the shared [start,end) window. No forward fill or synthetic rates.',
        limitations=['Settled rates are outcomes. They are not the pre-settlement predictions visible historically.',
            'Trade candles do not provide executable BBO or historical spread. Mark/oracle data availability differs by venue.',
            'A stable observed settlement interval validates this sample; future schedule changes require fresh metadata.'])
    (directory/'dataset.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8',newline='\n')
    print(coverage.to_string(index=False))
    print(f'DONE: {len(common)} common coins, {len(common)*4} complete markets, {report["common_candle_rows"]:,} common candles.',flush=True)
    return report

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',type=Path,default=HERE)
    validate(parser.parse_args().directory)
