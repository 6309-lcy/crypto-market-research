"""Fetch public instrument metadata; current snapshots do not prove September rules.

Writes supplements/ only. The frozen September dataset is unchanged.
"""
from pathlib import Path
from datetime import datetime, timezone
import json,hashlib,time
import requests

HERE=Path(__file__).resolve().parent
BASES='ADA ARB ATOM AVAX BNB BTC DOGE DOT ETH LINK LTC OP SOL UNI XRP'.split()

def main():
    output=HERE/'supplements';output.mkdir(exist_ok=True)
    requests_log=[];rows=[]
    def fetch(name,url,params=None,body=None):
        response=requests.post(url,json=body,timeout=30) if body is not None else requests.get(url,params=params,timeout=30)
        response.raise_for_status();data=response.json()
        raw=response.content
        (output/(name+'.json')).write_bytes((json.dumps(data,indent=2,ensure_ascii=True)+'\n').encode())
        requests_log.append(dict(name=name,url=url,parameters=params,body=body,
            fetched_at=datetime.now(timezone.utc).isoformat(),response_sha256=hashlib.sha256(raw).hexdigest()))
        time.sleep(.3)
        return data
    bn=fetch('binance_exchange_info','https://fapi.binance.com/fapi/v1/exchangeInfo')
    for item in bn['symbols']:
        if item.get('baseAsset') in BASES and item.get('quoteAsset')=='USDT' and item.get('contractType')=='PERPETUAL':
            filters={f['filterType']:f for f in item['filters']}
            rows.append(dict(exchange='binance',base=item['baseAsset'],symbol=item['symbol'],quote=item['quoteAsset'],
                status=item.get('status'),tick_size=filters.get('PRICE_FILTER',{}).get('tickSize'),
                quantity_step=filters.get('LOT_SIZE',{}).get('stepSize'),minimum_quantity=filters.get('LOT_SIZE',{}).get('minQty'),
                minimum_notional=filters.get('MIN_NOTIONAL',{}).get('notional'),contract_size_base=1))
    by=fetch('bybit_instruments','https://api.bybit.com/v5/market/instruments-info',{'category':'linear','limit':1000})
    if by.get('retCode')!=0:raise ValueError('Bybit instruments error')
    for item in by['result']['list']:
        if item.get('baseCoin') in BASES and item.get('quoteCoin')=='USDT' and item.get('contractType')=='LinearPerpetual':
            rows.append(dict(exchange='bybit',base=item['baseCoin'],symbol=item['symbol'],quote=item['quoteCoin'],status=item.get('status'),
                tick_size=item['priceFilter']['tickSize'],quantity_step=item['lotSizeFilter']['qtyStep'],
                minimum_quantity=item['lotSizeFilter']['minOrderQty'],minimum_notional=item['lotSizeFilter'].get('minNotionalValue'),
                contract_size_base=1,funding_interval_minutes=item.get('fundingInterval')))
    if by['result'].get('nextPageCursor'):raise ValueError('Bybit instrument pagination required')
    ok=fetch('okx_instruments','https://www.okx.com/api/v5/public/instruments',{'instType':'SWAP'})
    if ok.get('code')!='0':raise ValueError('OKX instruments error')
    for item in ok['data']:
        if item.get('instId') in [b+'-USDT-SWAP' for b in BASES]:
            rows.append(dict(exchange='okx',base=item['instId'].split('-')[0],symbol=item['instId'],quote='USDT',status=item.get('state'),
                tick_size=item['tickSz'],quantity_step=item['lotSz'],minimum_quantity=item['minSz'],
                minimum_notional=None,contract_size_base=item.get('ctVal') if item.get('ctValCcy')==item['instId'].split('-')[0] else None,
                contract_value=item.get('ctVal'),contract_value_currency=item.get('ctValCcy'),contract_multiplier=item.get('ctMult')))
    hl=fetch('hyperliquid_meta','https://api.hyperliquid.xyz/info',body={'type':'meta'})
    for item in hl['universe']:
        if item['name'] in BASES:
            rows.append(dict(exchange='hyperliquid',base=item['name'],symbol=item['name'],quote='USD',status='delisted' if item.get('isDelisted') else 'listed',
                quantity_step=str(10**(-item['szDecimals'])),minimum_quantity=None,minimum_notional=None,contract_size_base=1,
                size_decimals=item['szDecimals'],max_leverage=item.get('maxLeverage'),tick_size=None))
    counts={venue:len([r for r in rows if r['exchange']==venue]) for venue in ('binance','bybit','okx','hyperliquid')}
    if set(counts.values())!={15}:raise ValueError(f'Incomplete current metadata {counts}')
    snapshot=dict(as_of=datetime.now(timezone.utc).isoformat(),temporal_scope='CURRENT metadata snapshot; not validated historical September specifications',
        historical_usage='Do not retroactively impose current sizes, margins, funding schedules or fee tiers on September.',
        rows=rows,requests=requests_log,market_counts=counts)
    (output/'instrument_metadata.json').write_text(json.dumps(snapshot,indent=2)+'\n',encoding='utf-8',newline='\n')
    manifest=dict(temporal_scope=snapshot['temporal_scope'],files={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in output.glob('*.json') if p.name!='manifest.json'})
    (output/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8',newline='\n')
    print('DONE: current metadata for 60 markets. Historical rule validity explicitly unverified.',flush=True)

if __name__=='__main__':main()
