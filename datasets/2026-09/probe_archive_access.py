"""Small anonymous HEAD probes; does not download archives or use paid access."""
from pathlib import Path
from datetime import datetime,timezone
from concurrent.futures import ThreadPoolExecutor
import json
import requests

HERE=Path(__file__).resolve().parent
URLS={
 'hyperliquid_asset_contexts_sep01':'https://hyperliquid-archive.s3.amazonaws.com/asset_ctxs/20260901.csv.lz4',
 'hyperliquid_asset_contexts_sep30':'https://hyperliquid-archive.s3.amazonaws.com/asset_ctxs/20260930.csv.lz4',
 'hyperliquid_btc_l2_sep01':'https://hyperliquid-archive.s3.amazonaws.com/market_data/20260901/0/l2Book/BTC.lz4',
 'binance_btc_aggtrades_sep01':'https://data.binance.vision/data/futures/um/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2026-09-01.zip',
 'binance_btc_bookticker_sep01':'https://data.binance.vision/data/futures/um/daily/bookTicker/BTCUSDT/BTCUSDT-bookTicker-2026-09-01.zip',
 'bybit_btc_trades_sep01':'https://public.bybit.com/trading/BTCUSDT/BTCUSDT2026-09-01.csv.gz',
}

def probe(item):
    name,url=item
    result=dict(name=name,url=url,method='HEAD',access='anonymous',checked_at=datetime.now(timezone.utc).isoformat())
    try:
        response=requests.head(url,timeout=15,allow_redirects=True)
        result.update(http_status=response.status_code,content_length=response.headers.get('Content-Length'),
                      content_type=response.headers.get('Content-Type'),final_url=response.url)
        if response.status_code==403:result['interpretation']='Anonymous access denied; does not prove object absence.'
        elif response.status_code==404:result['interpretation']='This exact object path was not found; other archive paths may exist.'
        elif response.status_code==200:result['interpretation']='Object accessible by HEAD; content/schema/month completeness not downloaded or verified.'
    except requests.RequestException as exc:
        result.update(error_type=type(exc).__name__,interpretation='Probe failed; availability unresolved.')
    return result

def main():
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(probe,URLS.items()))
    report=dict(scope='Small BTC/date source-access probes only; not complete archive inventory.',
        paid_access_used=False,archive_bodies_downloaded=False,results=results,
        documentation={'hyperliquid':'https://hyperliquid.gitbook.io/hyperliquid-docs/historical-data',
            'binance':'https://github.com/binance/binance-public-data',
            'bybit':'https://public.bybit.com/trading/',
            'okx':'https://www.okx.com/historical-data'})
    out=HERE/'supplements'/'source_access.json'
    out.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8',newline='\n')
    for result in results:print(result['name'],result.get('http_status',result.get('error_type')),result.get('content_length'),flush=True)

if __name__=='__main__':main()
