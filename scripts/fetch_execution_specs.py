"""Download official public interface documents only; no credentials or account requests."""
import hashlib
import json
from pathlib import Path
import httpx

SOURCES={
    'migration.md':'https://docs.polymarket.com/migrate/data-api-v1-to-v2.md',
    'orders.md':'https://docs.polymarket.com/trading/manage-orders.md',
    'user-stream.md':'https://docs.polymarket.com/trading/realtime-order-updates.md',
    'wallet.md':'https://docs.polymarket.com/trading/wallet-activity.md',
    'data-v2.json':'https://data-api.polymarket.com/v2/openapi.json',
    'clob.yaml':'https://docs.polymarket.com/api-spec/clob-openapi.yaml',
    'user-asyncapi.json':'https://docs.polymarket.com/asyncapi-user.json',
}


if __name__=='__main__':
    root=Path('data/execution-specs');root.mkdir(parents=True,exist_ok=True);index=[]
    with httpx.Client(timeout=20,follow_redirects=True) as client:
        for name,url in SOURCES.items():
            try:
                response=client.get(url);response.raise_for_status()
                (root/name).write_bytes(response.content)
                index.append({'url':url,'file':name,'http_status':response.status_code,'sha256':hashlib.sha256(response.content).hexdigest()})
            except httpx.HTTPError as error:index.append({'url':url,'file':name,'error':type(error).__name__})
    (root/'index.json').write_text(json.dumps(index,indent=2),encoding='utf-8')
    print(json.dumps(index))
