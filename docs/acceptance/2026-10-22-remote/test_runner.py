import os,sys,json,subprocess,uuid,time
from pathlib import Path
from urllib.parse import urlsplit,urlunsplit
import psycopg
from psycopg import sql
root=Path(__file__).resolve().parents[3];out=Path(__file__).resolve().parent
base=os.environ['QB_DATABASE_URL'];name='qb_remote_test_'+uuid.uuid4().hex[:16]
report={'test_database':name,'isolation':'dedicated temporary database; fake OSS in pytest'}
u=urlsplit(base);env=dict(os.environ,QB_DATABASE_URL=urlunsplit((u.scheme,u.netloc,'/'+name,u.query,u.fragment)))
for key in list(env):
 if key.startswith('QB_OSS_') or key.startswith('ALIBABA_CLOUD_'):env.pop(key)
assert not any(k.startswith('QB_OSS_') or k.startswith('ALIBABA_CLOUD_') for k in env)
try:
 with psycopg.connect(base,autocommit=True) as c:
  c.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
 start=time.perf_counter()
 with (out/'pytest.txt').open('w') as log:
  proc=subprocess.run([str(root/'.venv/bin/python'),'-m','pytest','--runxfail','-q','--junitxml='+str(out/'pytest.xml')]+sys.argv[1:],cwd=root,env=env,stdout=log,stderr=subprocess.STDOUT)
 report['exit_code']=proc.returncode;report['elapsed_seconds']=time.perf_counter()-start
finally:
 with psycopg.connect(base,autocommit=True) as c:
  c.execute(sql.SQL('DROP DATABASE IF EXISTS {} WITH (FORCE)').format(sql.Identifier(name)))
  report['test_database_removed']=not c.execute('SELECT 1 FROM pg_database WHERE datname=%s',(name,)).fetchone()
 (out/'environment.json').write_text(json.dumps(report,indent=2))
sys.exit(proc.returncode)
