"""Pruebas del almacenamiento persistente."""
import json, os, tempfile
from storage import SignalStore, signal_record

results=[]
def check(n,c,d=''):
    results.append((n,c)); print(f"{'PASS' if c else 'FAIL':4}  {n}  {d}")

with tempfile.TemporaryDirectory() as tmp:
    st=SignalStore(token='',gist_id='',local_path=os.path.join(tmp,'s.jsonl'))
    check('sin token usa archivo local', st.backend=='local')
    check('se marca como no persistente', st.persistent is False)
    check('el estado avisa del borrado', 'borra' in st.status()['note'])
    check('escribe', st.append([{'ts':'x','symbol':'A','verdict':'GO'}])==1)
    st.append([{'ts':'y','symbol':'B','verdict':'WAIT'}])
    check('acumula entre llamadas', len(st.read())==2)
    check('count coincide', st.count()==2)
    check('lista vacia no escribe', st.append([])==0)

r=signal_record({'symbol':'MUU','verdict':'GO','leverage_factor':2.0,
  'streak':{'last_close':10.5,'streak_days':3,'avg_rvol_streak':2.2,
            'extension_atr':1.1,'atr':0.4},
  'liquidity':{'adv_usd':5e6},'rules':[{'x':1}]*6,'position':{'notional':1000}})
check('extrae solo campos medibles', set(r)=={'ts','symbol','verdict','close',
  'streak_days','avg_rvol','extension_atr','atr','leverage_factor','adv_usd'})
check('no arrastra rules ni position','rules' not in r and 'position' not in r)
check('serializa a JSON', bool(json.dumps(r)))

class Fake(SignalStore):
    def __init__(s):
        super().__init__(token='fake',gist_id='g1',local_path='/tmp/nope')
        s.blob=''
    def _gist_read(s): return s.blob
    def _gist_write(s,c): s.blob=c
f=Fake()
check('con token usa gist', f.backend=='gist' and f.persistent is True)
f.append([{'ts':'1','symbol':'A'}]); f.append([{'ts':'2','symbol':'B'},{'ts':'3','symbol':'C'}])
check('el gist acumula', len(f.read())==3)
check('una linea JSON por registro', f.blob.count('\n')==3)

class Broken(SignalStore):
    def __init__(s): super().__init__(token='fake',gist_id='g1',local_path='/tmp/brk.jsonl')
    def _gist_read(s): raise RuntimeError('github caido')
    def _gist_write(s,c): raise RuntimeError('github caido')
b=Broken()
check('fallo de red no lanza excepcion', b.append([{'ts':'1'}])==0)
check('el error queda en el estado','github caido' in (b.status()['last_error'] or ''))
check('respalda en local al fallar', os.path.exists('/tmp/brk.jsonl'))
os.path.exists('/tmp/brk.jsonl') and os.remove('/tmp/brk.jsonl')

print('\n'+'='*60)
bad=[n for n,c in results if not c]
print(f"{len(results)-len(bad)}/{len(results)} pruebas pasaron")
if bad: print('FALLARON:',bad); raise SystemExit(1)
print('Almacenamiento validado.')
