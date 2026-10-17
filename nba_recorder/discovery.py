"""Bounded general NBA discovery. Reading a game never certifies its strategy."""
import re
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
import httpx
from .api import GAMMA, array, parse_time, utc_ms
from .teams import team_code
from .game_state import schedule_match

SCHEDULE_URL = 'https://cdn.nba.com/static/json/staticData/scheduleLeagueV2_1.json'
TYPES = {'ALL','PRESEASON','REGULAR_SEASON','PLAY_IN','PLAYOFFS','UNKNOWN'}


def classify(event, start, official=None):
    official = official or {}
    labels = [str(event.get(k) or '') for k in ('seasonType','season_type','gameLabel','gameSubLabel','competitionRound')]
    labels += [str(t.get('label','')) for t in event.get('tags',[]) if isinstance(t,dict)]
    labels += [str(official.get(k) or '') for k in ('gameLabel','gameSubLabel','season_type')]
    text = ' '.join(labels).lower().replace('_',' ').replace('–','-').replace('summerleague','summer league').replace('allstar','all star')
    excluded = any(x in text for x in ('summer league','all-star','all star'))
    cup = 'cup' in text
    kind = ('PRESEASON' if 'preseason' in text or 'pre-season' in text else
            'PLAY_IN' if 'play-in' in text or 'play in' in text else
            'PLAYOFFS' if 'playoff' in text or ('finals' in text and not cup) else
            'REGULAR_SEASON' if 'regular season' in text else 'UNKNOWN')
    round_ = next((v for v in labels if any(x in v.lower() for x in ('conference','first round','second round','nba finals'))), 'UNKNOWN')
    dt = datetime.fromtimestamp(start/1000,timezone.utc)
    year = dt.year if dt.month >= 7 else dt.year-1
    season = str(official.get('season') or event.get('season') or f'{year}-{str(year+1)[-2:]}')
    parsed=re.fullmatch(r'(\d{4})(?:[-–](?:\d{2}|\d{4}))?',season)
    if parsed:season=f'{parsed[1]}-{str(int(parsed[1])+1)[-2:]}'
    return {'season': season, 'season_type':kind, 'competition_round':round_, 'competition_tags':['NBA_CUP'] if cup else [],
            'excluded': excluded, 'season_evidence':{'labels':labels,'source':'official schedule + Gamma explicit metadata',
            'season_label_basis':'explicit season when available, otherwise July-to-June display convention; not classification evidence'}}


def normalize_market(event, market, nba, now, official=None):
    """No fixed season dates/series/slug template. Fail closed on identity/rules."""
    try:
        declared=(event.get('sport') or {}).get('sport')
        if declared and declared.lower()!='nba':return None,'EXCLUDED_BY_FILTER'
        nba_ok = (event.get('sport') or {}).get('sport') == 'nba' or any(str(s.get('id'))==str(nba.get('series')) for s in event.get('series',[])) or any(str(t.get('id'))==str(nba.get('primaryTagId')) for t in event.get('tags',[]) if isinstance(t,dict))
        if not nba_ok:
            return None,'EXCLUDED_BY_FILTER'
        if market.get('sportsMarketType') != 'moneyline':
            return None,'EXCLUDED_BY_FILTER'
        normalized_rule = ' '.join(market.get('description','').lower().replace('cancelled','canceled').split())
        normalized_rule=normalized_rule.replace('including overtime','including any overtime').replace('50/50','50-50')
        full_rule='final score including any overtime' in normalized_rule
        if any(str(market.get(k,'')).lower() not in {'','none','full','full_game','game'} for k in ('gamePeriod','periodType')):
            return None,'EXCLUDED_BY_FILTER'
        if market.get('slug') != event.get('slug') and not full_rule:
            return None,'IDENTITY_AMBIGUOUS'
        roles = {}
        for t in event.get('teams',[]):
            if t.get('league')=='nba' and t.get('ordering') in {'home','away'}:
                code = team_code(t.get('abbreviation')) or team_code(t.get('name'))
                if t['ordering'] in roles or not code:
                    return None,'IDENTITY_AMBIGUOUS'
                roles[t['ordering']] = code
        if len(roles)!=2 or roles['home']==roles['away']:
            return None,'IDENTITY_AMBIGUOUS'
        start = parse_time(market.get('gameStartTime') or event['startTime'])
        labels = array(market['outcomes'])
        version = market.get('version','v1')
        if version not in {'v1','v2'}:
            return None,'SCHEMA_UNSUPPORTED'
        assets = array(market['positionIds'] if version=='v2' else market['clobTokenIds'])
        if len(labels)!=2 or len(assets)!=2 or len(set(assets))!=2 or {team_code(l) for l in labels} != set(roles.values()):
            return None,'IDENTITY_AMBIGUOUS'
        if not all(market.get(k) for k in ('id','conditionId','question')) or not event.get('id') or not re.fullmatch('[a-zA-Z0-9-]{1,160}',event.get('slug','')):
            return None,'IDENTITY_AMBIGUOUS'
        tick,minimum = Decimal(str(market['orderPriceMinTickSize'])),Decimal(str(market['orderMinSize']))
        if not tick.is_finite() or not minimum.is_finite() or not 0<tick<=1 or minimum<=0:
            return None,'SCHEMA_UNSUPPORTED'
        schedule = schedule_match(roles['away'],roles['home'],start)
        official = official or schedule
        category = classify(event,start,official)
        if schedule and category['season_type']=='UNKNOWN':
            category['season_type']='PRESEASON'
            category['season_evidence']['verified_schedule']=schedule
        if category.pop('excluded'):
            return None,'EXCLUDED_BY_FILTER'
        description = market.get('description','')
        rules = all(x in normalized_rule for x in ('including any overtime','postponed','canceled','50-50'))
        conflict = bool(event.get('startTime') and parse_time(event['startTime']) != start)
        return {**category,'venue':'polymarket.com','league':'nba','market_type':'moneyline','version':version,
                'discovery_schema':'NBA_GENERAL_V1','event_id':str(event['id']),'market_id':str(market['id']),
                'condition_id':market['conditionId'],'slug':event['slug'],'question':market['question'],
                'game_start':market.get('gameStartTime') or event['startTime'],'game_start_ms':start,
                'home_team':roles['home'],'away_team':roles['away'],'home_away_evidence':'Gamma event teams',
                'outcomes':[{'label':l,'asset_id':str(a)} for l,a in zip(labels,assets)],
                'tick_size':str(tick),'minimum_order_size':str(minimum),'rules_verified':rules,
                'rules_evidence':{'source':GAMMA+'/events/slug/'+event['slug'],'description':description},
                'description':description,'resolution_source':market.get('resolutionSource'),
                'polymarket_game_id':event.get('gameId'),'nba_game_id':(official or {}).get('nba_game_id'),
                'official_game':official,'identity_conflict':conflict,'event_live':event.get('live'),'event_ended':event.get('ended'),
                'event_status':{k:event.get(k) for k in ('active','closed','archived')},
                'status':{k:market.get(k) for k in ('active','closed','archived','acceptingOrders','enableOrderBook','restricted')},
                'schedule_status_raw':event.get('status'),'game_phase':'ENDED' if event.get('ended') else 'PAUSED' if event.get('paused') is True or event.get('status')=='Suspended' else 'BREAK' if event.get('period')=='HT' else 'LIVE' if event.get('live') else 'SCHEDULED' if start>now else 'UNKNOWN',
                'paper_scope_approved': False,
                'research_eligible': bool(rules and not conflict),
                'collection_allowed':not conflict},'MARKET_FOUND'
    except (KeyError,TypeError,ValueError,ArithmeticError):
        return None,'SCHEMA_UNSUPPORTED'


def schedule_rows(payload):
    league = payload['leagueSchedule']
    rows=[]
    for day in league['gameDates']:
        for g in day['games']:
            rows.append({'nba_game_id':str(g['gameId']),'home_team':team_code(g['homeTeam']['teamTricode']),
                         'away_team':team_code(g['awayTeam']['teamTricode']),'start_ms':parse_time(g['gameDateTimeUTC']),
                         'season':league.get('seasonYear'),'gameLabel':g.get('gameLabel'),'gameSubLabel':g.get('gameSubLabel'),
                         'source':SCHEDULE_URL,'raw':g})
    return rows


async def discover(api, start, end, season_type='ALL'):
    if season_type not in TYPES or not 0<end-start<=8*86400000:
        raise ValueError('bounded discovery window/type required')
    evidence=[]; schedule=[]; now=utc_ms()
    async with httpx.AsyncClient(timeout=8,follow_redirects=False) as client:
        try:
            r=await client.get(SCHEDULE_URL)
            evidence.append({'url':SCHEDULE_URL,'http_status':r.status_code,'received_ms':utc_ms()})
            r.raise_for_status();schedule=schedule_rows(r.json())
        except (httpx.HTTPError,KeyError,ValueError,TypeError) as error:
            evidence.append({'source':'NBA schedule','status':'ACCESS_FAILED' if isinstance(error,httpx.HTTPError) else 'SCHEMA_UNSUPPORTED','error':type(error).__name__})
    counts=Counter(); markets=[]; raw_events=[]
    try:
        sports=await api.get(GAMMA,'/sports');nba=next(s for s in sports if s.get('sport')=='nba')
        evidence.append({'url':GAMMA+'/sports','http_status':200,'selected_nba_metadata':nba,'received_ms':utc_ms()})
        for offset in range(0,500,100):
            params={'limit':100,'offset':offset,'order':'id','ascending':'true',
                    'start_time_min':datetime.fromtimestamp(start/1000,timezone.utc).isoformat(),
                    'start_time_max':datetime.fromtimestamp(end/1000,timezone.utc).isoformat()}
            params['tag_id' if nba.get('primaryTagId') else 'series_id']=str(nba.get('primaryTagId') or nba['series'])
            events=await api.get(GAMMA,'/events',params)
            evidence.append({'url':GAMMA+'/events','params':params,'http_status':200,'received_ms':utc_ms()})
            if not isinstance(events,list):raise ValueError('events schema')
            raw_events.extend(events)
            for event in events:
                # Detail lookup supplies missing roles without guessing from title.
                if len(event.get('teams',[]))!=2 and re.fullmatch('[a-zA-Z0-9-]{1,160}',event.get('slug','')):
                    event=await api.get(GAMMA,'/events/slug/'+event['slug'])
                for m in event.get('markets',[]):
                    official=None
                    try:
                        t=parse_time(m.get('gameStartTime') or event['startTime'])
                        codes={x.get('ordering'):team_code(x.get('abbreviation')) or team_code(x.get('name')) for x in event.get('teams',[])}
                        hits=[g for g in schedule if (g['home_team'],g['away_team'],g['start_ms'])==(codes.get('home'),codes.get('away'),t)]
                        official=hits[0] if len(hits)==1 else None
                    except (KeyError,ValueError,TypeError):pass
                    item,status=normalize_market(event,m,nba,now,official)
                    if item and not start<=item['game_start_ms']<=end:status='EXCLUDED_BY_FILTER';item=None
                    if item and season_type!='ALL' and item['season_type']!=season_type:status='EXCLUDED_BY_FILTER';item=None
                    counts[status]+=1
                    if item:markets.append(item)
            if len(events)<100:break
        else:raise ValueError('pagination incomplete at safety bound')
        status='MARKET_FOUND' if markets else 'IDENTITY_AMBIGUOUS' if counts['IDENTITY_AMBIGUOUS'] else 'SCHEMA_UNSUPPORTED' if counts['SCHEMA_UNSUPPORTED'] else 'EXCLUDED_BY_FILTER' if raw_events else 'NO_LISTED_MARKET'
    except (httpx.HTTPError,OSError) as error:
        status='ACCESS_FAILED';evidence.append({'error':type(error).__name__})
    except (KeyError,ValueError,TypeError,StopIteration) as error:
        status='SCHEMA_UNSUPPORTED';evidence.append({'error':str(error)})
    matched={m.get('nba_game_id') for m in markets}
    unlisted=[{**g,'status':'NO_LISTED_MARKET' if status not in {'ACCESS_FAILED','SCHEMA_UNSUPPORTED'} else status} for g in schedule if start<=g['start_ms']<=end and g['nba_game_id'] not in matched]
    return {'markets':list({m['market_id']:m for m in markets}.values()),'status':status,'reasons':dict(counts),
            'schedule_only':unlisted,'evidence':evidence,'raw_events':raw_events,'scanned_at_ms':utc_ms(),'started_ms':now,'season_type':season_type}
