"""Start project-local services, provision the local DB and serve Django on loopback."""
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import signal
from dotenv import dotenv_values

ROOT=Path(__file__).resolve().parent.parent
env={**os.environ,**{k:v for k,v in dotenv_values(ROOT/'.env').items() if v is not None}}
runtime=ROOT/'.runtime'; runtime.mkdir(exist_ok=True)
services=runtime/'services'/'usr'
env['LD_LIBRARY_PATH']=str(services/'lib/x86_64-linux-gnu')
os.environ.update(env)
owned=[]
def running(port):
    try:
        with socket.create_connection(('127.0.0.1',port),timeout=.3): return True
    except OSError: return False
def start(name,args,port=None):
    if port and running(port): return
    pidpath=runtime/f'{name}.pid'
    if not port and pidpath.exists():
        try:
            pid=int(pidpath.read_text())
            command=Path(f'/proc/{pid}/cmdline').read_bytes().replace(b'\x00',b' ').decode()
            expected='celery worker' if name=='worker' else 'celery beat'
            if str(ROOT) in command and all(part in command for part in expected.split()): return
        except (OSError,ValueError): pass
    log=(runtime/f'{name}.log').open('ab')
    proc=subprocess.Popen(args,cwd=ROOT,env=env,stdout=log,stderr=log,start_new_session=True)
    owned.append(proc)
    pidpath.write_text(str(proc.pid))
    if port:
        for _ in range(60):
            if running(port): return
            if proc.poll() is not None: break
            time.sleep(.2)
        raise SystemExit(f'{name} failed. Read {runtime / (name+".log")}')

def cleanup():
    for proc in reversed(owned):
        if proc.poll() is None: proc.terminate()
    for proc in reversed(owned):
        try: proc.wait(timeout=8)
        except subprocess.TimeoutExpired: proc.kill()

def interrupted(signum,frame):
    raise KeyboardInterrupt

import atexit
atexit.register(cleanup)
signal.signal(signal.SIGTERM,interrupted)

if not services.exists(): raise SystemExit('Run bash scripts/setup_local.sh first.')
dbport=int(env.get('DB_PORT','3307'))
start('mariadb',[str(services/'sbin/mariadbd'),'--no-defaults',f'--basedir={services}',f'--datadir={runtime/"db"}',f'--socket={runtime/"mysql.sock"}',f'--pid-file={runtime/"mariadb-server.pid"}',f'--port={dbport}','--bind-address=127.0.0.1','--skip-name-resolve','--innodb-buffer-pool-size=64M'],dbport)
start('redis',[str(services/'bin/redis-server'),'--bind','127.0.0.1','--port','6380','--dir',str(runtime),'--appendonly','yes','--protected-mode','yes'],6380)
# Socket administration authenticates the OS user; application user has access only to its DB/test DB.
password=env['DB_PASSWORD']
if not all(c.isalnum() or c in '_-' for c in password): raise SystemExit('Local bootstrap DB_PASSWORD must be alphanumeric, underscore or hyphen.')
sql=f"CREATE DATABASE IF NOT EXISTS smartcare CHARACTER SET utf8mb4; CREATE USER IF NOT EXISTS 'smartcare'@'127.0.0.1' IDENTIFIED BY '{password}'; GRANT ALL ON smartcare.* TO 'smartcare'@'127.0.0.1'; GRANT ALL ON test_smartcare.* TO 'smartcare'@'127.0.0.1';"
subprocess.run([str(services/'bin/mariadb'),'--no-defaults',f'--socket={runtime/"mysql.sock"}'],input=sql,text=True,env=env,check=True,stdout=subprocess.DEVNULL)
if '--services-only' in sys.argv:
    print('MariaDB and Redis ready. Keep this process running.',flush=True)
    try:
        while True: time.sleep(30)
    except KeyboardInterrupt: sys.exit(0)
subprocess.run([sys.executable,'manage.py','migrate','--noinput'],cwd=ROOT,env=env,check=True)
subprocess.run([sys.executable,'manage.py','seed_demo'],cwd=ROOT,env=env,check=True)
from datetime import date
subprocess.run([sys.executable,'manage.py','generate_slots','--start-date',date.today().isoformat(),'--days','30'],cwd=ROOT,env=env,check=True)
start('worker',[sys.executable,'-m','celery','-A','smartcare','worker','--pool=solo','--loglevel=info','--hostname=smartcare@%h'])
start('beat',[sys.executable,'-m','celery','-A','smartcare','beat','--loglevel=info','--schedule',str(runtime/'celerybeat')])
os.chdir(ROOT)
print('Smart Care: http://127.0.0.1:8000/ — keep this terminal running.',flush=True)
web=subprocess.Popen([sys.executable,'manage.py','runserver','127.0.0.1:8000','--noreload'],cwd=ROOT,env=env)
owned.append(web)
try: sys.exit(web.wait())
except KeyboardInterrupt: sys.exit(0)
