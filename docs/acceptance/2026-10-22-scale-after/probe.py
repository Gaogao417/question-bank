"""Bounded synthetic cloud benchmark. Own database/prefix; no production writes."""
import io, json, os, random, sys, tempfile, threading, time, uuid, math
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
import psycopg
from psycopg import sql
from PIL import Image
ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT))
from question_bank import db, oss_store
from question_bank.service import QuestionBankService
from tests.test_qb_manage_service import make_package
out=Path(__file__).parent
base=os.environ['QB_DATABASE_URL']; u=urlsplit(base)
run=uuid.uuid4().hex; name='qb_scale_'+run[:16]; prefix='acceptance/'+run+'/'
report={'test_database':name,'prefix':prefix,'profiles':[], 'model':'one shared service, closed loop, 8 requests per profile; synthetic noise PNG, no model calls'}
keys=set(); lock=threading.Lock(); local=threading.local()
original_execute=db.Connection.execute
def execute(conn,query,*args,**kwargs):
    start=time.perf_counter()
    try:return original_execute(conn,query,*args,**kwargs)
    finally:
        if hasattr(local,'stages'):
            category='lock_acquire_ms' if 'pg_advisory_xact_lock' in str(query) else 'sql_ms'
            local.stages[category]=local.stages.get(category,0)+(time.perf_counter()-start)*1000
original_render=__import__('question_bank.assets',fromlist=['AssetStore']).AssetStore.render_rel
# Capture total render wall time (including GET); concurrently accumulated.
render_times=[]
def render(store,*args,**kwargs):
    start=time.perf_counter()
    try:return original_render(store,*args,**kwargs)
    finally:
        with lock:render_times.append((time.perf_counter()-start)*1000)
# render signature remains stable across optimizations.
db.Connection.execute=execute
bucket=oss_store._bucket(); original_get=oss_store.get_object
original_put=oss_store.put_object; original_exists=oss_store.object_exists
ops={'get':0,'put':0,'head':0}
def get(key):
    with lock:ops['get']+=1
    return original_get(prefix+key)
def put(key,raw,mime):
    with lock:keys.add(prefix+key);ops['put']+=1
    return original_put(prefix+key,raw,mime)
def exists(key):
    with lock:ops['head']+=1
    return original_exists(prefix+key)
oss_store.get_object=get;oss_store.put_object=put;oss_store.object_exists=exists
created=False;svc=None
try:
    with psycopg.connect(base,autocommit=True) as admin:
        admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)));created=True
    os.environ['QB_DATABASE_URL']=urlunsplit((u.scheme,u.netloc,'/'+name,u.query,u.fragment))
    os.environ['QB_DATABASE_SCHEMA']='benchmark'
    with tempfile.TemporaryDirectory(prefix='qb-scale-') as tmp:
        tmp=Path(tmp);svc=QuestionBankService(tmp/'bank')
        assert svc.conn.execute('SELECT current_database() AS name').fetchone()['name']==name
        number=0
        for side in (512,1024):
            images=[]
            for j in range(4):
                raw=random.Random(side*10+j).randbytes(side*side)
                buf=io.BytesIO();Image.frombytes('L',(side,side),raw).save(buf,format='PNG')
                images.append(buf.getvalue())
                svc.register_asset(images[-1],source_kind='manual')
            for concurrency in (1,4):
                packages=[]
                for i in range(8):
                    number+=1
                    p=make_package(tmp/'packages',submission_id=f'scale-{number}',question_number=number)
                    for file,raw in zip(sorted(p.rglob('*.png')),images):file.write_bytes(raw)
                    packages.append(p)
                def work(package):
                    local.stages={}
                    start=time.perf_counter();receipt=svc.submit_candidate(package)
                    elapsed=(time.perf_counter()-start)*1000
                    assert receipt['status']=='accepted',receipt
                    return {'ms':elapsed,'stages':local.stages,'status':receipt['status']}
                with ThreadPoolExecutor(max_workers=concurrency) as pool:
                    # Each request thread owns a connection; exclude first connection setup.
                    barrier=threading.Barrier(concurrency)
                    def warm():
                        svc.conn.execute('SELECT 1');barrier.wait(timeout=30)
                    list(pool.map(lambda _:warm(),range(concurrency)))
                    before_ops=dict(ops);start=time.perf_counter()
                    samples=list(pool.map(work,packages));elapsed=time.perf_counter()-start
                values=sorted(s['ms'] for s in samples)
                report['profiles'].append({'image_side':side,'bytes_per_image':[len(b) for b in images],
                    'concurrency':concurrency,'samples':samples,'duration_seconds':elapsed,
                    'throughput':len(samples)/elapsed,'p50_ms':values[math.ceil(len(values)*.5)-1],
                    'p95_ms':values[math.ceil(len(values)*.95)-1],
                    'ops':{k:ops[k]-before_ops[k] for k in ops}})
                (out/'progress.json').write_text(json.dumps(report,indent=2))
        assert svc.conn.execute('SELECT count(*) AS n FROM revisions').fetchone()['n']==32
        report['result']='passed'
finally:
    if svc:svc.close()
    try:
        for key in keys:
            assert key.startswith(prefix);bucket.delete_object(key)
        report['objects_removed']=all(not bucket.object_exists(key) for key in keys)
    finally:
        if created:
            with psycopg.connect(base,autocommit=True) as admin:
                admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
                report['database_removed']=admin.execute('SELECT NOT EXISTS(SELECT 1 FROM pg_database WHERE datname=%s)',(name,)).fetchone()[0]
        (out/'result.json').write_text(json.dumps(report,indent=2))
print(json.dumps({**report,'profiles':[{k:v for k,v in p.items() if k!='samples'} for p in report['profiles']]},indent=2))
