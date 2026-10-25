"""Charts and independent, causally re-decoded historical Paper experiments."""
import argparse
import copy
import csv
import hashlib
import html
import json
import time
from datetime import timezone,timedelta
from pathlib import Path

from ..fair_value.store import read_connection
from ..inplay.policy import BookPolicy
from ..inplay.observations import price_point
from ..inplay.sports import normalize
from ..console.research import markouts
from .catalog import inspect
from .engine import freeze_recording,observed_events

CST=timezone(timedelta(hours=8))

from ..inplay.session import BookSession


class IndexedHistoricalSession(BookSession):
    """Exclude provably ineligible terminal orders before loading large frames.

    Equal-time orders remain for the existing ambiguity diagnostic. Late public
    prints still see cancelled orders whose exchange cancellation was later.
    """
    def __init__(self,manager,model):
        super().__init__(manager,model)
        manager.db.executescript("""
        CREATE INDEX IF NOT EXISTS study_exchange_state ON pe_exchange(json_extract(payload,'$.state'));
        CREATE INDEX IF NOT EXISTS study_exchange_cancel ON pe_exchange(json_extract(payload,'$.cancelled_ms'));
        CREATE INDEX IF NOT EXISTS study_exchange_submit ON pe_exchange(json_extract(payload,'$.submit_ms'));
        CREATE INDEX IF NOT EXISTS study_pending ON pe_pending(done,due_ms,id);
        """)
        table = 'ip_input_refs' if manager.db.execute("SELECT 1 FROM sqlite_master WHERE name='ip_input_refs'").fetchone() else 'ip_inputs'
        manager.db.execute('CREATE INDEX IF NOT EXISTS study_input_time ON '+table+'(time_ms DESC)')

    def execution_orders(self,exchange_ms):
        ids=[r[0] for r in self.om.db.execute("""SELECT id FROM pe_exchange
            WHERE json_extract(payload,'$.state') IN ('OPEN','PARTIALLY_FILLED')
            OR json_extract(payload,'$.cancelled_ms')>=?
            OR json_extract(payload,'$.submit_ms')=?""",(exchange_ms,exchange_ms))]
        return [self.om.order(i) for i in ids]


def redecode(event, authorize_paper=False):
    """Only use the raw sports message already attached to this callback."""
    result=copy.deepcopy(event)
    for f in result['frames']:
        if authorize_paper:
            f['market']['research_authorized']=True
            f['runtime_permission']={**f.get('runtime_permission',{}),'quotes_enabled':True}
            f['posthoc_permission']='USER_AUTHORIZED_INDEPENDENT_PAPER_ONLY; recorded transport unchanged'
        old=f.get('sports') or {};raw=old.get('raw');m=f['market'];stamp=old.get('received_ms')
        if not isinstance(raw,dict) or type(stamp) is not int or stamp>f['time_ms']:continue
        if any(type(old.get(k)) is int and old[k]>f['time_ms'] for k in ('processed_ms','strategy_available_ms')):continue
        cooked=normalize(raw,stamp,{'event_id':m['event_id'],'game_id':str(m.get('polymarket_game_id') or ''),
            'home_team':m.get('home_team'),'away_team':m.get('away_team'),
            'identity_evidence':'Recorded Gamma game ID + exact team roles; retrospective case-insensitive decoder'})
        for k in ('processed_ms','strategy_available_ms'):
            if k in old:cooked[k]=old[k]
        cooked['retrospective_decoder']='nba-league-case-v1; original raw and receive time retained'
        f['sports']=cooked
    result['posthoc']='REDECODED_RECORDED_SPORTS_NOT_ORIGINAL_EXECUTION'
    return result


def extract(path,bucket_s=10):
    import numpy as np
    db=read_connection(path)
    try:
        policy=BookPolicy(**json.loads(db.execute('SELECT config FROM pe_control WHERE id=1').fetchone()[0]))
        start,end=db.execute('SELECT min(time_ms),max(time_ms) FROM ip_inputs').fetchone()
        n=(end-start)//(bucket_s*1000)+1;assets={};prices={};market=None;observations={};raw_hash=hashlib.sha256();print_times={}
        for row in db.execute('SELECT time_ms,payload FROM ip_inputs ORDER BY time_ms,rowid'):
            raw_hash.update((str(row['time_ms'])+'\t'+row['payload']+'\n').encode())
            e=json.loads(row['payload']);i=(row['time_ms']-start)//(bucket_s*1000)
            for t in e.get('trades',[]):
                if t.get('event_id'):print_times.setdefault(t['event_id'],row['time_ms'])
            for f in e['frames']:
                market=market or f['market'];a=f['asset_id'];b=f.get('book') or {};p=price_point(f,policy)
                prices.setdefault(a,[]).append(p)
                if a not in assets:
                    assets[a]={'label':next((o['label'] for o in f['market']['outcomes'] if o['asset_id']==a),a),
                        'bid':np.full(n,np.nan),'ask':np.full(n,np.nan),'depth_bid':np.full(n,np.nan),'depth_ask':np.full(n,np.nan),
                        'heat_bid':np.full((100,n),np.nan),'heat_ask':np.full((100,n),np.nan),'prints':[]}
                    observations[a]=set()
                x=assets[a];good=p['mid'] is not None
                # The last observation in a bucket wins, including invalidation.
                x['bid'][i]=float(b['bid']) if good else np.nan;x['ask'][i]=float(b['ask']) if good else np.nan
                x['depth_bid'][i]=x['depth_ask'][i]=np.nan
                x['heat_bid'][:,i]=np.nan;x['heat_ask'][:,i]=np.nan
                if not good:continue
                observations[a].add((f.get('book_ref'),f.get('book_received_ms')))
                bid,ask=float(b['bid']),float(b['ask'])
                for side,near,best in [('bids','bid',bid),('asks','ask',ask)]:
                    levels=b.get(side,[])
                    x['depth_'+near][i]=sum(float(l['size']) for l in levels if abs(float(l['price'])-best)<=.020000001)
                    for l in levels:
                        price=float(l['price']);index=min(99,max(0,int(price*100)))
                        old=x['heat_'+near][index,i]
                        x['heat_'+near][index,i]=(0 if np.isnan(old) else old)+float(l['size'])
        # ip_prints is the original recorder's deduplicated public print stream.
        for row in db.execute('SELECT payload FROM ip_prints'):
            t=json.loads(row[0]);a=t.get('asset_id')
            at=t.get('strategy_available_ms',t.get('received_ms',print_times.get(t.get('event_id'))))
            if a in assets and type(at) is int and start<=at<=end:
                assets[a]['prints'].append({**t,'t':at})
        return {'start':start,'end':end,'bucket_s':bucket_s,'assets':assets,'prices':prices,'market':market,
            'input_sha256':raw_hash.hexdigest(),'observations':{a:len(s) for a,s in observations.items()}}
    finally:db.close()


def draw(data,folder):
    import numpy as np
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.dates as dates
    from matplotlib.colors import LogNorm
    from datetime import datetime
    plt.rcParams.update({'font.size':9,'axes.spines.top':False,'axes.spines.right':False})
    start,end=data['start'],data['end'];step=data['bucket_s']*1000
    clock=lambda t:datetime.fromtimestamp(t/1000,CST)
    assets=list(data['assets'].values());n=len(assets[0]['bid']);times=[clock(start+i*step) for i in range(n)]
    title=data['market'].get('question',str(data['market']['market_id']));period=f"{clock(start):%Y-%m-%d} Beijing time | recorded {clock(start):%H:%M}--{clock(end):%H:%M}"
    for kind in ('price-trades','depth-heatmap','liquidity-activity'):
        rows=3 if kind=='liquidity-activity' else 2 if kind=='depth-heatmap' else 1
        fig,axes=plt.subplots(rows,len(assets),figsize=(14,3.3*rows+1),squeeze=False,sharex=True,layout='constrained')
        fig.get_layout_engine().set(rect=(0,.055,1,.945))
        for col,x in enumerate(assets):
            ax=axes[0,col];ax.set_title(x['label'])
            if kind=='price-trades':
                ax.plot(times,x['bid']*100,label='Best bid',lw=.7,color='teal');ax.plot(times,x['ask']*100,label='Best ask',lw=.7,color='firebrick')
                ax.plot(times,(x['bid']+x['ask'])*50,label='Mid (not fair value)',lw=.6,color='black',alpha=.6)
                ts=x['prints'];ax.scatter([clock(t['t']) for t in ts],[float(t['price'])*100 for t in ts],
                    s=[min(60,2+np.sqrt(float(t['size']))) for t in ts],alpha=.25,label='Public prints (size = shares)',color='darkorange',rasterized=True)
                ax.set_ylabel('Price (cents)');ax.set_ylim(0,100);ax.legend(fontsize=7)
            elif kind=='depth-heatmap':
                # Separate panels preserve both sides even in the same 1-cent
                # price bin (e.g. 0.991 bid / 0.999 ask). Never net or overwrite.
                for row,side,cmap in ((0,'bid','Blues'),(1,'ask','Reds')):
                    panel=axes[row,col];heat=x['heat_'+side]
                    positive=heat[np.isfinite(heat)&(heat>0)]
                    vmax=max(1,float(np.percentile(positive,99))) if len(positive) else 1
                    im=panel.imshow(np.ma.masked_where(~np.isfinite(heat)|(heat<=0),heat),aspect='auto',origin='lower',extent=[dates.date2num(clock(start)),dates.date2num(clock(start+n*step)),0,100],
                        cmap=cmap,norm=LogNorm(vmin=.01,vmax=vmax),interpolation='none')
                    panel.set_title(x['label']+' / '+side);panel.set_ylabel('Price (cents; 1-cent bins)')
                    fig.colorbar(im,ax=panel,label='Visible '+side+' shares; logarithmic; color clipped p99')
            else:
                ax.plot(times,(x['ask']-x['bid'])*100,color='black');ax.set_ylabel('Spread (cents)')
                axes[1,col].plot(times,x['depth_bid'],label='Bid',color='teal');axes[1,col].plot(times,x['depth_ask'],label='Ask',color='firebrick')
                axes[1,col].set_ylabel('Visible shares within 2 cents of BBO');axes[1,col].legend(fontsize=7)
                bins={}
                for t in x['prints']:
                    minute=(t['t']-start)//60000;bins[minute]=bins.get(minute,0)+float(t['size'])
                axes[2,col].bar([clock(start+k*60000) for k in bins],list(bins.values()),width=1/1440,color='slategray')
                axes[2,col].set_ylabel('Recorded print shares / minute (not full market volume)')
            for axis in axes[:,col]:
                axis.set_xlim(clock(start),clock(end));axis.xaxis.set_major_formatter(dates.DateFormatter('%H:%M',tz=CST));axis.grid(alpha=.12)
                if start<=data['market'].get('game_start_ms',-1)<=end:
                    axis.axvline(clock(data['market']['game_start_ms']),color='gray',ls=':',lw=.8,label='Scheduled tip, not observed phase')
            axes[-1,col].set_xlabel('Beijing time (UTC+8)')
        fig.suptitle(title+' | '+kind+'\n'+period,fontsize=12)
        fig.text(.01,.005,f"Source: recorded ip_inputs / ip_prints. {data['bucket_s']}s last-observed buckets; gaps blank, no interpolation. Depth is truncated recorded depth, not full book. Public-print side is unverified.",fontsize=7)
        fig.savefig(folder/(kind+'.png'),dpi=140);plt.close(fig)


def replay(tape,tape_id,output,policy,data,mode,model,authorize_paper=False):
    from ..quote_engine.storage import PaperStore
    from ..inplay.store import SingleWriterStore
    from ..inplay.manager import UnifiedPaperManager
    from ..inplay.session import BookSession
    from ..inplay.report import report
    output.mkdir();store=SingleWriterStore(output/'paper.sqlite', shared_inputs=output.parent/'shared-inputs.sqlite');source=PaperStore.readonly(tape)
    started=time.perf_counter();derived=hashlib.sha256()
    try:
        om=UnifiedPaperManager(store,policy,data['start']);session=IndexedHistoricalSession(om,model)
        with om.db:om.db.execute("INSERT INTO ip_session_config VALUES('posthoc_origin',?)",(json.dumps({'mode':mode,'source_tape':tape_id,'not_original_execution':True}),))
        for event in observed_events(source,tape_id):
            current=redecode(event,authorize_paper) if mode=='REDECODED' else event
            derived.update(json.dumps(current,sort_keys=True,separators=(',',':')).encode())
            session.on_event(current)
        result=report(om)
        om.kill('posthoc_end_no_forced_liquidation');om.advance(om.now+max(policy.cancel_latency_ms,policy.request_timeout_ms)+1);om.reconcile()
        fills=[]
        for r in om.db.execute("SELECT id,time_ms,payload FROM l6_events WHERE kind='FILL' ORDER BY seq"):
            value=json.loads(r['payload'])
            if value.get('purpose')!='SIMULATED_COMPLETE_SET_SPLIT':fills.append({**value,'id':r['id'],'time_ms':r['time_ms']})
        result.update(posthoc_paper_authorization=bool(authorize_paper and mode=='REDECODED'),mode='POSTHOC_PAPER_NOT_ORIGINAL_EXECUTION',decoder=mode,fill_model=model,source_tape_id=tape_id,
            derived_input_sha256=derived.hexdigest(),shutdown_ledger_consistent=om.consistent(),
            fill_markouts={a:markouts([f for f in fills if f['asset_id']==a],ps,data['end'],(10,30,60,300)) for a,ps in data['prices'].items()},
            wall_seconds=time.perf_counter()-started,live_go=False,
            limitations=['Sports decoded only from raw attached to each historical callback; no future joins or invented clock',
                'Fill proxies uncalibrated; public side assumed aggressor only inside existing models',
                'No actual orders/fills today; no actual profit; missing marks remain unknown; fees/rewards not verified',
                'Cash-only 25 per lane, 2-cent distance from book mid is not fair-value edge; no fee assumption makes net profit unknown'])
        for name,value in [('result',result),('orders',om.orders()),('fills',fills)]:
            (output/(name+'.json')).write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
        return {'mode':mode,'model':model,'orders':result['orders'],'fills':result['fill_events'],'risk_state':result['risk_state'],
            'account':result['account'],'ledger_consistent':result['shutdown_ledger_consistent'],'derived_input_sha256':result['derived_input_sha256'],
            'real_submission_calls':0,'result':output.name+'/result.json','wall_seconds':result['wall_seconds']}
    finally:store.close();source.close()


def run(root,day,output,resume=False,market_id=None,authorize_paper=False,no_charts=False):
    root=Path(root).resolve();output=Path(output).resolve();output.mkdir(parents=True,exist_ok=resume)
    state=json.loads((root/'data/unattended'/day/'state.json').read_text(encoding='utf-8'))
    summary_file=output/(('summary-'+market_id+'.json') if market_id else 'summary.json')
    previous=summary_file if summary_file.exists() else output/'summary.json'
    results=json.loads(previous.read_text(encoding='utf-8')) if resume and previous.exists() else []
    if market_id:
        if market_id not in state['games']:raise ValueError('unknown recorded market')
        results=[r for r in results if r['market_id']==market_id]
    def publish():
        if not market_id:publish_html(output,results)
    prepared=[]
    for key,entry in state['games'].items():
        if market_id and key!=market_id:continue
        path=root/'data/console'/(entry['session_id']+'.sqlite')
        validate_closed(entry,path)
        folder=output/key;folder.mkdir(exist_ok=resume)
        data=extract(path)
        if not no_charts:draw(data,folder)
        print('Input prepared: '+key,flush=True)
        prepared.append((key,entry,path,folder,data))
        existing=next((r for r in results if r['market_id']==key),None)
        if existing and (existing['source_input_sha256']!=data['input_sha256'] or existing['session_id']!=entry['session_id']):raise ValueError('resume source changed')
        if not existing:results.append({'market_id':key,'session_id':entry['session_id'],'question':data['market']['question'],
            'source_input_sha256':data['input_sha256'],'start_ms':data['start'],'end_ms':data['end'],
            'capture_status':entry['status'],'posthoc_paper_authorization':authorize_paper,'replay_status':'PENDING','unique_book_refs':data['observations'],'lanes':[]})
        publish()
    for key,entry,path,folder,data in prepared:
        item=next(r for r in results if r['market_id']==key)
        sample=inspect(root,path);tape=folder/'source-tape.sqlite'
        tape_id=item['source_tape_id'] if resume and tape.exists() and item.get('source_tape_id') else freeze_recording(path,tape,sample)
        db=read_connection(path)
        try:
            policy=BookPolicy(**json.loads(db.execute('SELECT config FROM pe_control WHERE id=1').fetchone()[0]))
            original_orders=db.execute('SELECT count(*) FROM pe_orders').fetchone()[0]
            original_fills=db.execute("SELECT count(*) FROM l6_events WHERE kind='FILL'").fetchone()[0]
        finally:db.close()
        from ..quote_engine.storage import PaperStore
        from ..storage import encode
        frozen=PaperStore.readonly(tape);source_hash=hashlib.sha256()
        try:
            frozen.verify(tape_id)
            for event in observed_events(frozen,tape_id):source_hash.update((str(event['time_ms'])+'\t'+encode(event)+'\n').encode())
        finally:frozen.close()
        if source_hash.hexdigest()!=data['input_sha256']:raise ValueError('Source changed while preparing charts/tape')
        rows=[];saved_lanes=list(item['lanes'])
        item.update(source_tape_id=tape_id,original_orders=original_orders,original_fills=original_fills,replay_status='RUNNING')
        for mode,model in [('ORIGINAL','L2'),('REDECODED','L0'),('REDECODED','L1'),('REDECODED','L2')]:
            lane_folder=folder/(mode+'-'+model)
            completed=next((l for l in saved_lanes if l['mode']==mode and l['model']==model),None)
            if resume and completed and (lane_folder/'result.json').exists():
                old=json.loads((lane_folder/'result.json').read_text(encoding='utf-8'))
                if bool(old.get('posthoc_paper_authorization'))!=bool(authorize_paper and mode=='REDECODED') or old['source_tape_id']!=tape_id or not old['shutdown_ledger_consistent'] or old['derived_input_sha256']!=completed['derived_input_sha256']:raise ValueError('completed lane mismatch')
                rows.append(completed)
            else:
                if lane_folder.exists():
                    # Preserve an interrupted experiment; never restore it as an
                    # ACTIVE account or mix its incomplete evidence into results.
                    if output not in lane_folder.resolve().parents:raise ValueError('output boundary')
                    lane_folder.rename(lane_folder.with_name(lane_folder.name+'.incomplete-'+str(time.time_ns())))
                rows.append(replay(tape,tape_id,lane_folder,policy,data,mode,model,authorize_paper))
            print(key+' '+mode+' '+model+' '+json.dumps(rows[-1],ensure_ascii=False),flush=True)
            item['lanes']=rows
            summary_file.write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
            publish()
        item['replay_status']='FINISHED'
        summary_file.write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
        publish()
    if not market_id:finalize(output)
    from ..storage_workflow import after_research
    after_research(root, output)
    return results


def validate_closed(entry,path):
    if entry['status']=='FINISHED':return
    if entry['status']!='FAILED_NEEDS_REVIEW':raise ValueError('Recording not closed')
    db=read_connection(path)
    try:
        markers=[(r[0],json.loads(r[1]).get('detail',{}).get('reason')) for r in db.execute("SELECT time_ms,payload FROM pe_journal WHERE kind='KILL_SWITCH' ORDER BY seq")]
        stopped=[at for at,reason in markers if reason=='runner_stopped_keep_halted']
        last=db.execute('SELECT max(time_ms) FROM ip_inputs').fetchone()[0]
        anchor=db.execute("SELECT payload FROM ip_session_config WHERE id='recording_finished_ms'").fetchone()
        if not stopped or last is None or last>stopped[-1] or anchor is None or not any(reason=='runner_window_ended' for _,reason in markers):
            raise ValueError('Failed session lacks durable recording shutdown evidence')
    finally:db.close()


def publish_html(output,results):
    day=output.name
    terminal_path=output/'terminal-valuation.json'
    terminal=json.loads(terminal_path.read_text(encoding='utf-8')) if terminal_path.exists() else None
    values={(r['market_id'],r['model']):r for r in terminal['rows']} if terminal else {}
    content=['<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'+html.escape(day)+' post-hoc study</title><link rel="stylesheet" href="/day-study.css">',
        '<h1>'+html.escape(day)+' 真实行情 · 独立事后 Paper 回放</h1><p>原运行无订单。这里是事后研究，不是当日运行结果或真实盈利。ORIGINAL保留原记录；REDECODED重新解析当时已收到的体育消息；明确授权的独立研究分支启用Paper报价权限，保留记录的连接和风控限制。原始数据不修改。</p>',
        '<p>深度仅为已保存档位。图按10秒最后观察投影，空白不是零流动性；桶内短缺口可能被聚合隐藏，无未来补点。公开成交不能证明我们的订单成交。回放按完成组更新，未显示不代表零成交。</p><a href="summary.json">下载汇总JSON</a><a href="comparison.csv">下载完成后对照CSV</a><a href="/offline">返回离线控制台</a>']
    if terminal:
        content.append('<p>期末估值：将剩余模拟持仓按采集到的胜方每份$1、负方$0计算；不是实际兑付，不改变历史报价或原账本。模拟费用和奖励均为0。</p><a href="terminal-valuation.csv">下载逐场期末估值CSV</a><a href="terminal-valuation.json">结算假设与原始证据JSON</a>')
    for r in results:
        key=r['market_id'];content.append('<section><h2>'+html.escape(r['question'])+'</h2>')
        content.append('<p>回放进度：'+html.escape(r.get('replay_status','PENDING'))+'</p>')
        for kind in ('price-trades','depth-heatmap','liquidity-activity'):
            if not (output/key/(kind+'.png')).exists():continue
            label={'price-trades':'① 价格、BBO与公开成交','depth-heatmap':'② 已记录盘口深度热力图','liquidity-activity':'③ 价差、近端深度与成交活跃度'}[kind]
            content.append(f'<h3>{label}</h3><a href="{key}/{kind}.png">打开原尺寸 PNG</a><img src="{key}/{kind}.png" alt="{kind}">')
        for lane in r['lanes']:
            content.append('<p>'+html.escape(f"{lane['mode']} / {lane['model']}: orders={lane['orders']}, hypothetical fills={lane['fills']}, ledger consistent={lane['ledger_consistent']}, risk={lane['risk_state']}")+f' <a href="{key}/{lane["result"]}">完整模拟账户与Markout</a></p>')
            account=lane.get('account',{})
            if account:
                text=f"虚拟现金={account['cash']}；已实现模拟交易PnL（记录成本前）={account['realized_trading_pnl']}；"
                text+='终值估值缺失，未实现PnL和总PnL未知。' if account.get('missing_marks') else f"模型账本净PnL={account.get('net_pnl')}（未核实实际费用，不能视为可实现净收益）。"
                content.append('<p>'+html.escape(text)+'</p>')
            if lane['mode']=='REDECODED' and (key,lane['model']) in values:
                value=values[key,lane['model']]
                content.append('<p><strong>按记录胜方结算估值后的模拟总PnL：$'+html.escape(value['simulated_terminal_pnl'])+'</strong>；假设剩余份额兑付=$'+html.escape(value['assumed_inventory_payout'])+'，非真实盈利。</p>')
        content.append('</section>')
    (output/'index.html').write_text('\n'.join(content),encoding='utf-8')


def finalize(output):
    """Validate completed lane provenance, publish a compact analysis CSV."""
    from decimal import Decimal
    output=Path(output);results=json.loads((output/'summary.json').read_text(encoding='utf-8'));rows=[]
    for game in results:
        if game['replay_status']!='FINISHED' or len(game['lanes'])!=4:raise ValueError('incomplete experiments')
        if {(l['mode'],l['model']) for l in game['lanes']}!={('ORIGINAL','L2'),('REDECODED','L0'),('REDECODED','L1'),('REDECODED','L2')}:raise ValueError('missing/duplicate model')
        if len({l['derived_input_sha256'] for l in game['lanes'] if l['mode']=='REDECODED'})!=1:raise ValueError('different derived inputs')
        policies=[]
        for lane in game['lanes']:
            result=json.loads((output/game['market_id']/lane['result']).read_text(encoding='utf-8'))
            policies.append(result['policy'])
            if result['source_tape_id']!=game['source_tape_id'] or not result['shutdown_ledger_consistent'] or result['real_submission_calls']!=0:
                raise ValueError('provenance/ledger/submission evidence mismatch')
            if lane['mode']=='ORIGINAL' and (lane['orders']!=game['original_orders'] or lane['fills']!=game['original_fills']):
                raise ValueError('baseline differs from original')
            account=result['account']
            row={'market_id':game['market_id'],'question':game['question'],'decoder':lane['mode'],'model':lane['model'],
                'orders':lane['orders'],'hypothetical_fill_events':lane['fills'],'quote_availability':result['quote_availability'],
                'quote_availability_basis':'decision_count_not_wall_time','risk_state_at_input_end':result['risk_state'],
                'snapshot_stage':'END_OF_INPUT_BEFORE_CONTROLLED_CANCEL','gross_fair_value_edge':'NOT_COMPUTED_BOOK_DISTANCE_ONLY',
                'simulated_fill_order_rate':result['simulated_fill_order_rate'],'virtual_cash':account['cash'],
                'simulated_realized_trading_pnl_before_unverified_costs':account['realized_trading_pnl'],
                'model_net_pnl':account['net_pnl'],'actual_net_pnl':'UNKNOWN_NOT_REAL_EXECUTION',
                'missing_final_marks':bool(account['missing_marks']),'residual_shares':str(sum((Decimal(q) for q in account['positions'].values()),Decimal(0))),
                'ledger_consistent':lane['ledger_consistent'],'real_submission_calls':0,'source_tape_id':game['source_tape_id'],
                'derived_input_sha256':lane['derived_input_sha256']}
            for h in (10,30,60,300):
                ms=[m for group in result['fill_markouts'].values() for m in group if m['seconds']==h]
                valid=[m for m in ms if m['status']=='VALID'];weight=sum((Decimal(m['quantity']) for m in valid),Decimal(0))
                row[f'markout_{h}s_valid_events']=len(valid);row[f'markout_{h}s_total_events']=len(ms)
                row[f'markout_{h}s_qty_weighted_cents']=str(sum((Decimal(m['quantity'])*Decimal(m['fill_markout_cents']) for m in valid),Decimal(0))/weight) if weight else None
            rows.append(row)
        if any(p!=policies[0] for p in policies):raise ValueError('model policies differ')
    with (output/'comparison.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    publish_html(output,results)
    return rows


def merge_workers(output):
    output=Path(output);results=json.loads((output/'summary.json').read_text(encoding='utf-8'))
    for i,game in enumerate(results):
        path=output/('summary-'+game['market_id']+'.json')
        if not path.exists():raise ValueError('missing worker result')
        value=json.loads(path.read_text(encoding='utf-8'))
        if len(value)!=1 or value[0]['market_id']!=game['market_id'] or value[0]['source_input_sha256']!=game['source_input_sha256'] or value[0]['replay_status']!='FINISHED':raise ValueError('worker result mismatch/incomplete')
        results[i]=value[0]
    (output/'summary.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
    return finalize(output)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--date',default='2026-10-21');p.add_argument('--out',type=Path,required=True)
    p.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[2]);p.add_argument('--resume',action='store_true',help='reuse verified completed lanes; preserve and rerun incomplete lanes in fresh accounts')
    p.add_argument('--market',help='one recorded market; writes a separate summary for independent parallel workers')
    p.add_argument('--merge-workers',action='store_true',help='validate completed per-market summaries and publish final comparison')
    p.add_argument('--authorize-paper',action='store_true',help='Independent posthoc Paper permission; recorded transport remains unchanged')
    p.add_argument('--no-charts',action='store_true')
    a=p.parse_args()
    if a.merge_workers:merge_workers(a.out)
    else:run(a.root,a.date,a.out,a.resume,a.market,a.authorize_paper,a.no_charts)
