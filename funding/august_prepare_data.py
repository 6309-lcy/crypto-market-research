"""Extract compact, causal hourly observations from a local live JSONL archive.

Usage: python funding/august_prepare_data.py --archive /path/to/export
The public CSV retains original observation times; no future rates are backfilled.
"""
from __future__ import annotations
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import pandas as pd

HERE = Path(__file__).resolve().parent
HOUR = 3_600_000
START = int(pd.Timestamp('2026-08-11T11:00:00Z').timestamp() * 1000)
END = int(pd.Timestamp('2026-08-20T05:00:00Z').timestamp() * 1000)

def iso(ms):
    return pd.Timestamp(ms, unit='ms', tz='UTC').isoformat()

def extract(archive: Path, output: Path = HERE):
    selected = {}
    counts = Counter()
    bad = Counter()
    hashes = {}
    for kind in ('funding', 'l2'):
        for path in sorted((archive / kind).rglob('*.jsonl')):
            digest = hashlib.sha256()
            with path.open('rb') as stream:
                for line in stream:
                    digest.update(line)
                    try:
                        row = json.loads(line)
                        local = int(row['collected_at_ms'])
                        remote = row.get('funding_ts_ms' if kind == 'funding' else 'exchange_ts_ms')
                        # A venue timestamp slightly ahead of the collector is available only then.
                        observed = max(local, int(remote or 0))
                        if remote and abs(int(remote) - local) > HOUR:
                            # Some funding timestamps describe a settlement, not observation time.
                            observed = local
                        base, exchange = row['base'], row['exchange']
                        if row.get('source') != ('live' if kind == 'funding' else 'l2'):
                            continue
                    except (ValueError, KeyError, TypeError):
                        bad[kind] += 1
                        continue
                    counts[kind] += 1
                    if kind == 'funding':
                        decision = ((observed + HOUR - 1) // HOUR) * HOUR
                        if not START <= decision <= END or decision - observed > 300_000:
                            continue
                        key = (decision, exchange, base, 'signal')
                        if key in selected and selected[key]['observed_ms'] >= observed:
                            continue
                        rate = row.get('funding_rate')
                        if rate is None:
                            continue
                        selected[key] = dict(decision_ms=decision, exchange=exchange, base=base,
                            symbol=row.get('symbol', base), role='signal', observed_ms=observed,
                            collected_ms=local, funding_rate=float(rate),
                            interval_hours=row.get('funding_interval_hours'),
                            next_settlement_ms=row.get('next_funding_ts_ms'),
                            mark_price=row.get('mark_price'), oracle_price=row.get('oracle_px'))
                    else:
                        before = ((observed + HOUR - 1) // HOUR) * HOUR
                        after = ((observed - 60_000) // HOUR) * HOUR
                        candidates = []
                        if 0 <= before - observed <= 120_000:
                            candidates.append((before, 'valuation'))
                        if 60_000 <= observed - after <= 180_000:
                            candidates.append((after, 'execution'))
                        for decision, role in candidates:
                            if not START <= decision <= END:
                                continue
                            key = (decision, exchange, base, role)
                            old = selected.get(key)
                            if old and ((role == 'valuation' and old['observed_ms'] >= observed) or
                                        (role == 'execution' and old['observed_ms'] <= observed)):
                                continue
                            bid, ask = row.get('best_bid'), row.get('best_ask')
                            if not bid or not ask or not 0 < bid <= ask:
                                continue
                            selected[key] = dict(decision_ms=decision, exchange=exchange, base=base,
                                symbol=row.get('symbol', base), role=role, observed_ms=observed,
                                collected_ms=local, best_bid=float(bid), best_ask=float(ask),
                                mid=(float(bid)+float(ask))/2,
                                bids=json.dumps(row.get('bids', []), separators=(',', ':')),
                                asks=json.dumps(row.get('asks', []), separators=(',', ':')))
            hashes[str(path.relative_to(archive)).replace('\\', '/')] = digest.hexdigest()
        print(f'Extracted {kind}: {counts[kind]:,} raw records', flush=True)
    frame = pd.DataFrame(selected.values()).sort_values(['decision_ms','exchange','base','role'])
    for column in ('decision_ms','observed_ms','collected_ms'):
        frame[column.replace('_ms','_time')] = frame[column].map(iso)
    # CEX intervals are learned from observed next-settlement schedule changes.
    # Unverifiable initial intervals stay missing. HL explicitly stores its 1h interval.
    for (_, _), group in frame[frame.role == 'signal'].groupby(['exchange','base']):
        previous_next = None
        known_interval = None
        for index, row in group.sort_values('observed_ms').iterrows():
            nxt = row.next_settlement_ms
            if pd.notna(nxt):
                if previous_next is not None and nxt > previous_next:
                    hours = (nxt - previous_next) / HOUR
                    # A gap in archived observations is not evidence of a longer contract interval.
                    if hours in (1,2,4,8):
                        known_interval = hours
                previous_next = nxt
            if pd.notna(row.interval_hours):
                known_interval = row.interval_hours
            if known_interval is not None:
                frame.at[index, 'interval_hours'] = known_interval
    output.mkdir(parents=True, exist_ok=True)
    destination = output / 'august_observations.csv'
    frame.to_csv(destination, index=False, float_format='%.12g', lineterminator='\n')
    manifest = dict(window_start=iso(START), window_end=iso(END),
        raw_rows=dict(counts), malformed_rows=dict(bad), exported_rows=len(frame),
        decision_count=int(frame.decision_ms.nunique()),
        csv_sha256=hashlib.sha256(destination.read_bytes()).hexdigest(),
        selection='Last funding within 5m before decision; last valuation book within 2m; first execution book 60-180s after decision.',
        funding_interval='Explicit archived metadata, or causal changes in observed next settlement timestamps. Unknown intervals excluded from signals.',
        raw_file_sha256=hashes)
    (output / 'august_observations_manifest.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
    print(f'DONE: {len(frame):,} observations, {frame.decision_ms.nunique()} hourly decisions',flush=True)

if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive',type=Path,required=True)
    parser.add_argument('--output',type=Path,default=HERE)
    args=parser.parse_args()
    extract(args.archive,args.output)
