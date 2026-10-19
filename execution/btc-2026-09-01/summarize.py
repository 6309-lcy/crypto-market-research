"""Summarize validated per-venue acquisition reports without assuming fill readiness."""
from pathlib import Path
import hashlib,json
import pandas as pd

HERE=Path(__file__).resolve().parent

def main():
    reports={venue:json.loads((HERE/f'{venue}_report.json').read_text(encoding='utf-8')) for venue in ('bybit','okx')}
    frames={venue:pd.read_csv(HERE/f'{venue}_minutes.csv') for venue in reports}
    for venue,frame in frames.items():
        if len(frame)!=1440:raise ValueError(f'{venue} minute grid is incomplete')
        key='minute_utc'
        if key not in frame:
            candidates=[c for c in frame if 'minute' in c and ('utc' in c or 'time' in c)]
            if len(candidates)!=1:raise ValueError(f'{venue} minute key unresolved: {list(frame.columns)}')
            key=candidates[0]
        frame['minute']=pd.to_datetime(frame[key],utc=True,format='mixed')
        if frame.minute.duplicated().any():raise ValueError(f'{venue} duplicate minute')
        expected=set(pd.date_range('2026-09-01T00:00:00Z','2026-09-02T00:00:00Z',freq='min',inclusive='left'))
        if set(frame.minute)!=expected:raise ValueError(f'{venue} wrong UTC minute grid')
        trade_column='trades' if venue=='bybit' else 'trade_records'
        if not frame.book_records.gt(0).all() or not frame[trade_column].gt(0).all():
            raise ValueError(f'{venue} missing minute-level event coverage')
    merged=frames['bybit'][['minute']].merge(frames['okx'][['minute']],on='minute',how='inner')
    if len(merged)!=1440:raise ValueError('Shared minute grid not complete')
    summary=dict(window_start_inclusive='2026-09-01T00:00:00Z',window_end_exclusive='2026-09-02T00:00:00Z',
        markets=['Bybit BTCUSDT','OKX BTC-USDT-SWAP'],shared_minute_grid_rows=len(merged),
        input_sha256={f'{v}_{kind}.{extension}':hashlib.sha256((HERE/f'{v}_{kind}.{extension}').read_bytes()).hexdigest()
            for v in reports for kind,extension in [('report','json'),('minutes','csv')]},
        scripts_sha256={name:hashlib.sha256((HERE/name).read_bytes()).hexdigest() for name in ['bybit_collect.py','okx_collect.py','summarize.py']},
        cache_policy='Original archives stay outside the public repository; archive hashes/URLs in each report.',
        interpretation='A common minute grid is not proof of uninterrupted sequence correctness or maker fills. Read venue reports before replay.',
        performance_claim=False)
    (HERE/'shared_report.json').write_text(json.dumps(summary,indent=2)+'\n',encoding='utf-8',newline='\n')
    print('DONE: 1,440 common UTC minute rows; report and script hashes recorded.')

if __name__=='__main__':main()
