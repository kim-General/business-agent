from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


def uid(prefix):
    return f"{prefix}_{uuid.uuid4().hex}"


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, default=str)


class Conflict(ValueError):
    pass


class NotFound(ValueError):
    pass


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, title TEXT, dataset_id TEXT, context TEXT,
                    context_version INTEGER DEFAULT 0, created_at TEXT, updated_at TEXT);
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, session_id TEXT, status TEXT, phase TEXT,
                    question TEXT, client_key TEXT, expected_version INTEGER,
                    plan TEXT, result_id TEXT, clarification TEXT, error TEXT,
                    replay_of TEXT, created_at TEXT, updated_at TEXT,
                    UNIQUE(session_id,client_key));
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
                    run_id TEXT, result_id TEXT, status TEXT, created_at TEXT);
                CREATE TABLE IF NOT EXISTS results (
                    id TEXT PRIMARY KEY, run_id TEXT UNIQUE, payload TEXT, evidence TEXT,
                    context_version INTEGER, created_at TEXT);
                CREATE TABLE IF NOT EXISTS events (
                    run_id TEXT, seq INTEGER, type TEXT, payload TEXT, created_at TEXT,
                    PRIMARY KEY(run_id,seq));
                CREATE INDEX IF NOT EXISTS message_session ON messages(session_id,created_at);
            """)

    @contextmanager
    def db(self, write=False):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _session(self, row):
        if not row:
            raise NotFound("会话不存在")
        result = dict(row)
        result["context"] = json.loads(result["context"])
        return result

    def create_session(self, dataset_id):
        key, timestamp = uid("session"), now()
        with self.db(write=True) as db:
            db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?)", (key,"新会话",dataset_id,encode({"dataset_id":dataset_id}),0,timestamp,timestamp))
        return self.session(key)

    def sessions(self, dataset_id=None):
        with self.db() as db:
            rows = db.execute("SELECT * FROM sessions WHERE (? IS NULL OR dataset_id=?) ORDER BY updated_at DESC",(dataset_id,dataset_id)).fetchall()
        return [self._session(r) for r in rows]

    def session(self, session_id, messages=False):
        with self.db() as db:
            value = self._session(db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone())
            if messages:
                value["messages"] = [dict(r) for r in db.execute("SELECT * FROM messages WHERE session_id=? ORDER BY created_at,rowid",(session_id,)).fetchall()]
        return value

    def _run(self,row):
        if not row:
            raise NotFound("运行不存在")
        value=dict(row)
        for key in ("plan","clarification"):
            value[key]=json.loads(value[key]) if value[key] else None
        return value

    def run(self,run_id):
        with self.db() as db:
            return self._run(db.execute("SELECT * FROM runs WHERE id=?",(run_id,)).fetchone())

    def _event(self,db,run_id,event_type,payload):
        seq=db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM events WHERE run_id=?",(run_id,)).fetchone()[0]
        db.execute("INSERT INTO events VALUES (?,?,?,?,?)",(run_id,seq,event_type,encode(payload),now()))

    def event(self,run_id,event_type,payload):
        with self.db(write=True) as db:
            run=db.execute("SELECT status FROM runs WHERE id=?",(run_id,)).fetchone()
            if run and run[0] not in ("failed","cancelled","completed"):
                self._event(db,run_id,event_type,payload)

    def phase(self,run_id,phase,label):
        with self.db(write=True) as db:
            changed=db.execute("UPDATE runs SET phase=?,updated_at=? WHERE id=? AND status='running'",(phase,now(),run_id)).rowcount
            if changed:
                self._event(db,run_id,"phase_changed",{"phase":phase,"label":label})

    def create_run(self,session_id,question,client_key,expected_version,replay_of=None):
        timestamp=now()
        with self.db(write=True) as db:
            old=db.execute("SELECT * FROM runs WHERE session_id=? AND client_key=?",(session_id,client_key)).fetchone()
            if old:
                return self._run(old),False
            session=self._session(db.execute("SELECT * FROM sessions WHERE id=?",(session_id,)).fetchone())
            if session["context_version"]!=expected_version:
                raise Conflict("会话条件已更新，请刷新后重试")
            if db.execute("SELECT 1 FROM runs WHERE session_id=? AND status IN ('queued','running','awaiting_clarification')",(session_id,)).fetchone():
                raise Conflict("当前会话还有未完成的分析，请先完成澄清或取消")
            key=uid("run")
            db.execute("INSERT INTO runs(id,session_id,status,phase,question,client_key,expected_version,replay_of,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",(key,session_id,"queued","queued",question,client_key,expected_version,replay_of,timestamp,timestamp))
            db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?)",(uid("message"),session_id,"user",question,key,None,"submitted",timestamp))
            db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?)",(uid("message"),session_id,"assistant","",key,None,"queued",timestamp))
            if session["title"]=="新会话":
                db.execute("UPDATE sessions SET title=?,updated_at=? WHERE id=?",(question[:40],timestamp,session_id))
            self._event(db,key,"phase_changed",{"phase":"queued","label":"已提交，正在准备分析"})
            value=self._run(db.execute("SELECT * FROM runs WHERE id=?",(key,)).fetchone())
        return value,True

    def start(self,run_id):
        with self.db(write=True) as db:
            return bool(db.execute("UPDATE runs SET status='running',phase='planning',updated_at=? WHERE id=? AND status='queued'",(now(),run_id)).rowcount)

    def pause(self,run_id,clarification):
        clarification={**clarification,"id":uid("clarify")}
        with self.db(write=True) as db:
            changed=db.execute("UPDATE runs SET status='awaiting_clarification',clarification=?,updated_at=? WHERE id=? AND status='running'",(encode(clarification),now(),run_id)).rowcount
            if changed:
                self._event(db,run_id,"clarification_required",{"clarification":clarification})
                db.execute("UPDATE messages SET status='awaiting_clarification',content=? WHERE run_id=? AND role='assistant'",(clarification["question"],run_id))

    def resume(self,run_id,clarification_id,expected_version,answer=""):
        with self.db(write=True) as db:
            run=self._run(db.execute("SELECT * FROM runs WHERE id=?",(run_id,)).fetchone())
            session=self._session(db.execute("SELECT * FROM sessions WHERE id=?",(run["session_id"],)).fetchone())
            if run["status"]!="awaiting_clarification" or (run["clarification"] or {}).get("id")!=clarification_id:
                raise Conflict("此澄清已处理或运行已结束")
            if session["context_version"]!=expected_version:
                raise Conflict("会话条件已更新，请刷新")
            db.execute("UPDATE runs SET status='queued',updated_at=? WHERE id=?",(now(),run_id))
            db.execute("UPDATE messages SET status='queued' WHERE run_id=? AND role='assistant'",(run_id,))
            if answer:
                db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?)",(uid("message"),run["session_id"],"user","澄清："+answer,run_id,None,"submitted",now()))

    def save_plan(self,run_id,plan):
        with self.db(write=True) as db:
            db.execute("UPDATE runs SET plan=? WHERE id=? AND status='running'",(encode(plan),run_id))

    def complete(self,run_id,result,plan):
        with self.db(write=True) as db:
            run=self._run(db.execute("SELECT * FROM runs WHERE id=?",(run_id,)).fetchone())
            if run["status"]!="running":
                return False
            session=self._session(db.execute("SELECT * FROM sessions WHERE id=?",(run["session_id"],)).fetchone())
            if session["context_version"]!=run["expected_version"]:
                raise Conflict("运行期间会话状态发生变化")
            # A replay re-executes a saved historical plan for verification and
            # must NOT rewind the session's confirmed context to that old plan.
            is_replay=run.get("replay_of") is not None
            version=session["context_version"] if is_replay else session["context_version"]+1
            result_id=uid("result")
            payload={k:v for k,v in result.items() if k!="evidence"}
            payload.update(id=result_id,run_id=run_id,context_version=version)
            evidence={**result.get("evidence",{}),"plan":plan,"query_id":run_id,"metric_version":"1"}
            db.execute("INSERT INTO results VALUES(?,?,?,?,?,?)",(result_id,run_id,encode(payload),encode(evidence),version,now()))
            if not is_replay:
                db.execute("UPDATE sessions SET context=?,context_version=?,updated_at=? WHERE id=?",(encode(plan),version,now(),session["id"]))
            db.execute("UPDATE runs SET status='completed',phase='completed',result_id=?,plan=?,clarification=NULL,updated_at=? WHERE id=?",(result_id,encode(plan),now(),run_id))
            db.execute("UPDATE messages SET content=?,result_id=?,status='completed',created_at=? WHERE run_id=? AND role='assistant'",(result.get("summary","分析已完成"),result_id,now(),run_id))
            self._event(db,run_id,"result_ready",{"result_id":result_id,"context_version":version})
            self._event(db,run_id,"answer_ready",{"summary":result.get("summary","")})
            self._event(db,run_id,"run_completed",{"result_id":result_id})
        return True

    def finish_error(self,run_id,message,cancel=False):
        status="cancelled" if cancel else "failed"
        with self.db(write=True) as db:
            changed=db.execute("UPDATE runs SET status=?,phase=?,error=?,updated_at=? WHERE id=? AND status IN ('queued','running','awaiting_clarification')",(status,status,message,now(),run_id)).rowcount
            if changed:
                db.execute("UPDATE messages SET content=?,status=? WHERE run_id=? AND role='assistant'",(message,status,run_id))
                self._event(db,run_id,"run_cancelled" if cancel else "run_failed",{"message":message})
        return bool(changed)

    def events(self,run_id,after=0):
        with self.db() as db:
            rows=db.execute("SELECT * FROM events WHERE run_id=? AND seq>? ORDER BY seq",(run_id,after)).fetchall()
        return [{"id":f"{run_id}:{r['seq']}","run_id":run_id,"seq":r["seq"],"type":r["type"],"payload":json.loads(r["payload"])} for r in rows]

    def result(self,result_id,evidence=False):
        with self.db() as db:
            row=db.execute("SELECT * FROM results WHERE id=?",(result_id,)).fetchone()
        if not row:
            raise NotFound("结果不存在")
        return json.loads(row["evidence" if evidence else "payload"])

    def recover(self):
        with self.db() as db:
            interrupted=[r[0] for r in db.execute("SELECT id FROM runs WHERE status IN ('queued','running')")]
        for run_id in interrupted:
            self.finish_error(run_id,"服务重启中断了分析，请重新提交；已确认条件保持不变。")
