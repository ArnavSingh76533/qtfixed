"""Separate, owner-scoped agent state. Never migrates or replaces legacy records."""
import json
import sqlite3
import time
import uuid

class AgentStore:
    def __init__(self,path):
        self.db=sqlite3.connect(str(path));self.db.row_factory=sqlite3.Row
        self.db.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS preferences(owner INTEGER PRIMARY KEY,enabled INTEGER NOT NULL DEFAULT 0,timezone TEXT NOT NULL DEFAULT 'Asia/Kolkata');
        CREATE TABLE IF NOT EXISTS bundles(owner INTEGER,kind TEXT,name TEXT,description TEXT,instructions TEXT,files TEXT,enabled INTEGER DEFAULT 1,PRIMARY KEY(owner,kind,name));
        CREATE TABLE IF NOT EXISTS memories(owner INTEGER,scope TEXT,name TEXT,value TEXT,PRIMARY KEY(owner,scope,name));
        CREATE TABLE IF NOT EXISTS schedules(id TEXT PRIMARY KEY,owner INTEGER,text TEXT,cron TEXT,timezone TEXT,next_run REAL,status TEXT,failures INTEGER DEFAULT 0);
        CREATE INDEX IF NOT EXISTS due_schedules ON schedules(status,next_run);
        CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,owner INTEGER,scope TEXT,status TEXT,plan TEXT,summary TEXT,created REAL);
        CREATE TABLE IF NOT EXISTS run_artifacts(owner INTEGER,run_id TEXT,path TEXT,token TEXT,expires REAL,PRIMARY KEY(owner,run_id,path));
        CREATE TABLE IF NOT EXISTS answers(owner INTEGER,scope TEXT,body TEXT,created REAL,PRIMARY KEY(owner,scope));
        ''')
        # A process may have died after sending a reminder but before recording it.
        self.db.execute("UPDATE schedules SET status='uncertain' WHERE status='sending'")
        self.db.execute("UPDATE runs SET status='interrupted' WHERE status='running'")
        self.db.commit()
    def close(self):self.db.close()
    def prefs(self,owner):
        row=self.db.execute('SELECT enabled,timezone FROM preferences WHERE owner=?',(owner,)).fetchone()
        return dict(row) if row else {'enabled':0,'timezone':'Asia/Kolkata'}
    def set_pref(self,owner,**values):
        current=self.prefs(owner);current.update({k:v for k,v in values.items() if k in current})
        with self.db:self.db.execute('INSERT OR REPLACE INTO preferences VALUES (?,?,?)',(owner,int(bool(current['enabled'])),current['timezone']))
    def bundles(self,owner,kind=None):
        rows=self.db.execute('SELECT * FROM bundles WHERE owner=?'+(' AND kind=?' if kind else '')+' ORDER BY name',(owner,kind) if kind else (owner,))
        return [dict(r) for r in rows]
    def install(self,owner,kind,bundle):
        existing=self.bundles(owner)
        if len(existing)>=20 and not any(r['kind']==kind and r['name']==bundle['name'] for r in existing):raise ValueError('Maximum 20 installed skills/agents. Remove one first.')
        with self.db:self.db.execute('INSERT OR REPLACE INTO bundles VALUES (?,?,?,?,?,?,1)',(owner,kind,bundle['name'],bundle['description'],bundle['instructions'],json.dumps(bundle['files'])))
    def change_bundle(self,owner,kind,name,action):
        with self.db:
            if action=='remove':cur=self.db.execute('DELETE FROM bundles WHERE owner=? AND kind=? AND name=?',(owner,kind,name))
            else:cur=self.db.execute('UPDATE bundles SET enabled=? WHERE owner=? AND kind=? AND name=?',(int(action=='enable'),owner,kind,name))
        return bool(cur.rowcount)
    def memories(self,owner,scope):
        return {r['name']:r['value'] for r in self.db.execute('SELECT name,value FROM memories WHERE owner=? AND scope=?',(owner,scope))}
    def remember(self,owner,scope,name,value):
        if not name or len(name)>80 or not value or len(value)>2000:raise ValueError('Memory needs a name up to 80 and value up to 2,000 characters.')
        old=self.memories(owner,scope)
        if len(old)>=50 and name not in old:raise ValueError('Maximum 50 memories in this context. Delete one first.')
        with self.db:self.db.execute('INSERT OR REPLACE INTO memories VALUES (?,?,?,?)',(owner,scope,name,value))
    def forget(self,owner,scope,name=None):
        with self.db:self.db.execute('DELETE FROM memories WHERE owner=? AND scope=?'+(' AND name=?' if name else ''),(owner,scope,name) if name else (owner,scope))
    def add_schedule(self,owner,text,cron,tz,next_run):
        if len(self.schedules(owner))>=30:raise ValueError('Maximum 30 reminders. Cancel one first.')
        identity=uuid.uuid4().hex[:12]
        with self.db:self.db.execute('INSERT INTO schedules VALUES (?,?,?,?,?,?,?,0)',(identity,owner,text,cron,tz,next_run,'active'))
        return identity
    def schedules(self,owner):
        return [dict(r) for r in self.db.execute("SELECT * FROM schedules WHERE owner=? AND status NOT IN ('done','cancelled') ORDER BY next_run",(owner,))]
    def cancel_schedule(self,owner,identity):
        with self.db:return self.db.execute("UPDATE schedules SET status='cancelled' WHERE owner=? AND id=?",(owner,identity)).rowcount
    def due(self,now):
        return [dict(r) for r in self.db.execute("SELECT * FROM schedules WHERE status='active' AND next_run<=? ORDER BY next_run LIMIT 20",(now,))]
    def claim(self,identity):
        with self.db:return bool(self.db.execute("UPDATE schedules SET status='sending' WHERE id=? AND status='active'",(identity,)).rowcount)
    def schedule_status(self,identity,status,next_run=None,failures=0):
        with self.db:self.db.execute('UPDATE schedules SET status=?,next_run=COALESCE(?,next_run),failures=? WHERE id=?',(status,next_run,failures,identity))
    def begin(self,owner,scope,plan):
        identity=uuid.uuid4().hex[:12]
        with self.db:
            self.db.execute('INSERT INTO runs VALUES (?,?,?,?,?,?,?)',(identity,owner,scope,'running',json.dumps(plan),'',time.time()))
            self.db.execute('DELETE FROM runs WHERE owner=? AND id NOT IN (SELECT id FROM runs WHERE owner=? ORDER BY created DESC LIMIT 100)',(owner,owner))
        return identity
    def finish(self,identity,status,summary):
        with self.db:self.db.execute('UPDATE runs SET status=?,summary=? WHERE id=?',(status,summary[:12000],identity))
    def last_run(self,owner):
        row=self.db.execute('SELECT * FROM runs WHERE owner=? ORDER BY created DESC LIMIT 1',(owner,)).fetchone()
        return dict(row) if row else None
    def run(self,owner,identity):
        row=self.db.execute('SELECT * FROM runs WHERE owner=? AND id=?',(owner,identity)).fetchone()
        return dict(row) if row else None
    def add_artifact(self,owner,identity,path,token):
        if not self.run(owner,identity):raise ValueError('Artifact run does not belong to this owner.')
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO run_artifacts VALUES (?,?,?,?,?)',(owner,identity,path,token,time.time()+3600))
            self.db.execute('DELETE FROM run_artifacts WHERE expires<?',(time.time(),))
    def artifacts(self,owner,identity):
        if not self.run(owner,identity):return []
        return [dict(r) for r in self.db.execute('SELECT * FROM run_artifacts WHERE owner=? AND run_id=? ORDER BY path',(owner,identity))]
    def save_answer(self,owner,scope,body):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO answers VALUES (?,?,?,?)',(owner,scope,body[:120000],time.time()))
            self.db.execute('DELETE FROM answers WHERE created<?',(time.time()-7*86400,))
    def last_answer(self,owner,scope):
        row=self.db.execute('SELECT body FROM answers WHERE owner=? AND scope=? AND created>?',(owner,scope,time.time()-7*86400)).fetchone()
        return row['body'] if row else None
