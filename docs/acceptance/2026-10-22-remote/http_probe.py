"""Dedicated DB + prefixed real OSS + loopback HTTP, never production root/port."""
import json,os,sys,subprocess,tempfile,time,uuid,socket,threading,hashlib
from pathlib import Path
from urllib.parse import urlsplit,urlunsplit
import psycopg
from psycopg import sql
import uvicorn
from PIL import Image
REPO=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(REPO))
from question_bank import oss_store,remote_ingestion as remote
from question_bank.server import create_app
from tests.test_qb_manage_service import make_package
base=os.environ['QB_DATABASE_URL'];run=uuid.uuid4().hex;name='qb_remote_http_'+run[:16];prefix='acceptance/'+run+'/'
report={'database':name,'oss_prefix':prefix,'transport':'real HTTP loopback; serve_public BasicAuthMiddleware; real RDS and OSS','paid_models':False}
out=Path(__file__).parent
keys=set();metrics={'put':0,'get':0,'head':0};server=None;thread=None;created=False
try:
 with psycopg.connect(base,autocommit=True) as c:
  c.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)));created=True
 u=urlsplit(base);os.environ['QB_DATABASE_URL']=urlunsplit((u.scheme,u.netloc,'/'+name,u.query,u.fragment));os.environ['QB_DATABASE_SCHEMA']='http_probe'
 bucket=oss_store._bucket()
 original_get=oss_store.get_object
 def put(key,raw,content_type):
  metrics['put']+=1;keys.add(prefix+key);bucket.put_object(prefix+key,raw,headers={'Content-Type':content_type})
 def get(key):
  metrics['get']+=1;return original_get(prefix+key)
 def head(key):
  metrics['head']+=1;return bucket.object_exists(prefix+key)
 oss_store.put_object=put;oss_store.get_object=get;oss_store.object_exists=head
 os.environ['QB_UI_USER']='probe';os.environ['QB_UI_PASSWORD']=uuid.uuid4().hex
 import serve_public
 from question_bank.remote_ingestion import request
 original_submit=remote.submit_upload
 delay=False
 def measured_submit(*args,**kwargs):
  start=time.perf_counter()
  result=original_submit(*args,**kwargs)
  report.setdefault('server_ingestion_ms',[]).append((time.perf_counter()-start)*1000)
  if delay:time.sleep(.3)
  return result
 remote.submit_upload=measured_submit
 with tempfile.TemporaryDirectory(prefix='qb-http-remote-') as tmp:
  tmp=Path(tmp);root=tmp/'root';package=make_package(tmp/'packages',submission_id='remote-first')
  for path in package.rglob('*.png'):
   Image.effect_noise((512,512),100).convert('RGB').save(path)
  report['first_upload_asset_bytes']=sum(p.stat().st_size for p in package.rglob('*.png'))
  report['zip_bytes']=len(remote.package_bytes(package))
  sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen(128);port=sock.getsockname()[1]
  server=uvicorn.Server(uvicorn.Config(serve_public.BasicAuthMiddleware(create_app(root)),log_level='error',lifespan='on'))
  thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
  for _ in range(200):
   if server.started:break
   time.sleep(.01)
  assert server.started
  url=f'http://127.0.0.1:{port}'
  env={k:v for k,v in os.environ.items() if not(k.startswith('QB_') or k.startswith('ALIBABA_'))}
  env.update(QB_SERVER_URL=url,QB_SERVER_USER='probe',QB_SERVER_PASSWORD=os.environ['QB_UI_PASSWORD'])
  report['client_configuration']='only QB_SERVER_URL/USER/PASSWORD; all other QB_* and ALIBABA_* removed'
  def cli(*args):
   proc=subprocess.run([str(REPO/'.venv/bin/python'),'-m','question_bank.agent_tools',*args],cwd=REPO,env=env,text=True,capture_output=True,timeout=30)
   result=json.loads(proc.stdout);return proc.returncode,result
  assert request(url,'/api/submissions/remote-first',user='',password='')['http_status']==401
  report['unauthenticated_status']=401
  start=time.perf_counter();code,first=cli('upload','--package',str(package),'--receipt-out',str(tmp/'first-receipt.json'));report['first_cli_upload_ms']=(time.perf_counter()-start)*1000
  assert code==0 and first['receipt']['status']=='accepted',first
  report['first_upload_put_calls']=metrics['put'];assert metrics['put']==4
  code,replay=cli('upload','--package',str(package));assert code==0 and replay['receipt']['replayed'] is True
  report['replay_verified']=True
  delay=True
  code,timed=cli('upload','--package',str(package),'--timeout','0.01');assert code==1 and timed['outcome_unknown'] is True,timed
  time.sleep(.5);delay=False
  code,receipt=cli('receipt','--submission-id','remote-first');assert code==0 and receipt['receipt']['submission_id']=='remote-first'
  report['timeout_then_receipt_verified']=True
  payload=json.loads((package/'candidate.json').read_text());payload['item']['stem']+=' changed';(package/'candidate.json').write_text(json.dumps(payload))
  code,conflict=cli('upload','--package',str(package));assert code==1 and conflict['http_status']==409
  report['conflict_status']=conflict['http_status']
  (package/'candidate.json').write_text('[]');code,bad=cli('upload','--package',str(package));assert code==1 and bad['http_status']==422 and bad['errors'][0]['code']=='SCHEMA_INVALID'
  report['invalid_schema_status']=bad['http_status']
  attempts=list((root/'inputs/remote').iterdir());report['archived_attempts']=len(attempts)
  assert len(attempts)==5 and all((p/'upload.zip').is_file() for p in attempts)
  assert sum((p/'receipt.json').is_file() for p in attempts)==3
  assert sum((p/'error.json').is_file() for p in attempts)==2
  report['original_packages_and_receipts_verified']=True
  from question_bank.service import QuestionBankService
  svc=QuestionBankService(root)
  rows=svc.conn.execute('SELECT path,sha256 FROM assets').fetchall()
  for row in rows:
   assert 'sha256:'+hashlib.sha256(get(row['path'])).hexdigest()==row['sha256']
  report['real_oss_sha256_verified']=len(rows)
  assert svc.conn.execute('SELECT count(*) AS n FROM items').fetchone()['n']==1
  svc.close();report['single_item_after_replays']=True
  report['operations']=dict(metrics);report['ok']=True
except Exception as exc:
 report['error']=repr(exc);raise
finally:
 if server:
  server.should_exit=True
  if thread:thread.join(10)
 if 'bucket' in locals():
  for key in keys:bucket.delete_object(key)
  report['oss_objects_removed']=all(not bucket.object_exists(key) for key in keys)
  report['objects_created']=len(keys)
 if created:
  with psycopg.connect(base,autocommit=True) as c:
   c.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
   report['database_removed']=not c.execute('SELECT 1 FROM pg_database WHERE datname=%s',(name,)).fetchone()
 (out/'http.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
