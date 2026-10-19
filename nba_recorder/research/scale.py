"""Explicit synthetic load fixture. Stretched phases are not an NBA clock model."""
import os


def peak_rss_bytes():
    if os.name=='nt':
        import ctypes
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_=[('cb',wintypes.DWORD),('PageFaultCount',wintypes.DWORD)]+[(name,ctypes.c_size_t) for name in (
                'PeakWorkingSetSize','WorkingSetSize','QuotaPeakPagedPoolUsage','QuotaPagedPoolUsage',
                'QuotaPeakNonPagedPoolUsage','QuotaNonPagedPoolUsage','PagefileUsage','PeakPagefileUsage')]
        value=Counters();value.cb=ctypes.sizeof(value)
        get=ctypes.windll.kernel32.GetCurrentProcess;get.restype=ctypes.c_void_p
        query=ctypes.windll.psapi.GetProcessMemoryInfo
        query.argtypes=[ctypes.c_void_p,ctypes.POINTER(Counters),wintypes.DWORD]
        return value.PeakWorkingSetSize if query(get(),ctypes.byref(value),value.cb) else None
    try:
        import resource,sys
        value=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return value if sys.platform=='darwin' else value*1024
    except ImportError:return None


def events(seconds,cadence_ms=1000):
    if type(seconds) is not int or not 60<=seconds<=28800 or type(cadence_ms) is not int or not 100<=cadence_ms<=10000:
        raise ValueError('Synthetic load window 60..28800s and cadence 100..10000ms required')
    from ..inplay.demo import frame,AT
    previous_minute=-1
    for offset in range(0,seconds*1000+1,cadence_ms):
        at=AT+offset;period=min(4,1+offset*4//(seconds*1000))
        remaining=min(600,max(0,seconds-offset//1000)) if period==4 else 600
        fs=[frame(at,a,period,remaining) for a in ('a','b')]
        trades=[];minute=offset//60000
        if minute and minute!=previous_minute:
            side='SELL' if minute%2 else 'BUY'
            trades=[{'event_id':f'scale-print-{minute}','asset_id':'a','condition_id':'synthetic-condition',
                'exchange_ms':at,'received_ms':at,'processed_ms':at,'strategy_available_ms':at,
                'side':side,'price':'.45' if side=='SELL' else '.55','size':'110'}]
        previous_minute=minute
        yield {'id':f'synthetic-scale-{offset}','time_ms':at,'frames':fs,'trades':trades,
            'provenance':'SYNTHETIC_SCALE_FIXTURE; no real game; stretched phases, controlled prints'}
