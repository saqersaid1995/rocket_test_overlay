from __future__ import annotations

import os, sqlite3, uuid
from datetime import datetime, timezone
from pathlib import Path
from flask import Flask, flash, redirect, render_template, request, url_for
from .control import control

ROOT=Path(__file__).resolve().parent
DATA=Path(os.environ.get("STELLAR_OPS_DATA",ROOT/"data"))
DB=DATA/"stellar_ops.db"
ORG="org-stellar-kinetics"
MODULES=[
 ("programmes","Programmes","Programme Control"),("missions","Missions","Mission Control"),
 ("configuration","Configuration","Vehicle & Product Baselines"),("manufacturing","Manufacturing","Production & Quality"),
 ("tests","Test Operations","Ground Test Campaigns"),("launch","Launch Operations","Campaign & Countdown"),
 ("safety","Safety","Risk & Mission Assurance"),("documents","Documents","Controlled Records")]
app=Flask(__name__,template_folder="templates",static_folder="static")
app.secret_key=os.environ.get("STELLAR_OPS_SECRET","development-only-change-me")
app.register_blueprint(control)

def now(): return datetime.now(timezone.utc).isoformat(timespec="seconds")
def connect():
 DATA.mkdir(parents=True,exist_ok=True); c=sqlite3.connect(DB); c.row_factory=sqlite3.Row; c.execute("PRAGMA foreign_keys=ON"); return c
def init_db():
 with connect() as c:
  c.executescript((ROOT/"schema.sql").read_text())
  stamp=now(); c.execute("INSERT OR IGNORE INTO organisations VALUES(?,?,?,?)",(ORG,"SK","Stellar Kinetics",stamp))
  c.execute("INSERT OR IGNORE INTO users VALUES(?,?,?,?,?,?)",("user-local-admin",ORG,"local@stellar.invalid","Local Administrator","ACTIVE",stamp))
  c.execute("""INSERT OR IGNORE INTO programmes
   (id,organisation_id,code,name,objective,lifecycle_state,version,created_at,updated_at)
   VALUES(?,?,?,?,?,'ACTIVE',1,?,?)""",("programme-qualsrm",ORG,"QUALSRM","QualSRM Flight Qualification","Qualify the integrated sounding rocket, ground segment and recovery chain through controlled flight operations.",stamp,stamp))
  c.execute("""INSERT OR IGNORE INTO missions
   (id,organisation_id,programme_id,code,name,mission_type,objective,success_criteria,lifecycle_state,launch_site,version,created_at,updated_at)
   VALUES(?,?,?,?,?,?,?,?,?,'Oman — site pending',1,?,?)""",("mission-qualsrm-01",ORG,"programme-qualsrm","QUALSRM-01","Qualification Flight","Sounding Rocket","Demonstrate integrated vehicle readiness and controlled flight to the design envelope.","Safe launch; stable unguided flight; target apogee 1.5 km AGL; parachute deployment; vehicle recovery; barometric flight record recovered.","INTEGRATION",stamp,stamp))
  c.execute("""INSERT OR IGNORE INTO test_campaigns
   (id,organisation_id,programme_id,code,name,test_type,lifecycle_state,planned_start,planned_end,version,created_at,updated_at)
   VALUES(?,?,?,?,?,?,?,'2026-06-19','2026-08-31',1,?,?)""",("test-qualsrm-propulsion",ORG,"programme-qualsrm","TC-PROP-QUAL","RNX-71V Propulsion Qualification","Propulsion Static Fire","ANALYSED",stamp,stamp))
  c.execute("""INSERT OR IGNORE INTO launch_campaigns
   (id,organisation_id,mission_id,lifecycle_state,version,created_at,updated_at)
   VALUES(?,?,?,'PLANNING',1,?,?)""",("launch-qualsrm-01",ORG,"mission-qualsrm-01",stamp,stamp))
def audit(c,kind,entity,action,payload):
 c.execute("""INSERT INTO audit_events(event_id,organisation_id,actor_id,acting_role,occurred_at,correlation_id,entity_type,entity_id,action,previous_version,new_version,payload_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
 (str(uuid.uuid4()),ORG,"user-local-admin","SYSTEM_ADMINISTRATOR",now(),str(uuid.uuid4()),kind,entity,action,None,1,payload))

@app.context_processor
def context(): return {"modules":MODULES}

@app.get("/overview")
def command_center():
 init_db()
 with connect() as c:
  counts={"programmes":c.execute("SELECT count(*) FROM programmes WHERE archived_at IS NULL").fetchone()[0],"missions":c.execute("SELECT count(*) FROM missions").fetchone()[0],"tests":c.execute("SELECT count(*) FROM test_campaigns").fetchone()[0],"launch":c.execute("SELECT count(*) FROM launch_campaigns").fetchone()[0],"hazards":c.execute("SELECT count(*) FROM hazards WHERE lifecycle_state!='CLOSED'").fetchone()[0],"actions":c.execute("SELECT count(*) FROM actions WHERE status!='CLOSED'").fetchone()[0]}
  programmes=c.execute("SELECT * FROM programmes WHERE archived_at IS NULL ORDER BY updated_at DESC LIMIT 6").fetchall()
  missions=c.execute("SELECT m.*,p.code programme_code FROM missions m JOIN programmes p ON p.id=m.programme_id ORDER BY m.updated_at DESC LIMIT 6").fetchall()
  events=c.execute("SELECT * FROM audit_events ORDER BY sequence DESC LIMIT 8").fetchall()
 workspace={
  "mission":"QUALSRM-01","programme":"QUALSRM","phase":"INTEGRATION","target_apogee":"1.50 km AGL",
  "vehicle":"QSRM-FV01","propulsion":"RNX-71V","recovery":"Single parachute","guidance":"Unguided",
  "systems":[
   ("PROP","Propulsion","AMBER","Static-fire evidence requires final disposition"),
   ("STR","Structures","BLUE","Integration baseline in work"),
   ("AVN","Avionics","AMBER","Barometric altimeter verification open"),
   ("REC","Recovery","AMBER","Deployment test evidence required"),
   ("GSE","Ground Systems","BLUE","Launcher and ignition interfaces in work"),
   ("RNG","Range","GREY","Site and airspace package not baselined")],
  "gates":[("MDR","Mission Design Review",72,"IN REVIEW"),("TRR","Test Readiness Review",64,"OPEN"),("FRR","Flight Readiness Review",18,"LOCKED"),("LRR","Launch Readiness Review",0,"LOCKED")],
  "timeline":[("01","Mission definition","COMPLETE"),("02","Vehicle integration","ACTIVE"),("03","Ground qualification","ACTIVE"),("04","Flight readiness","PENDING"),("05","Launch campaign","PENDING"),("06","Recovery & review","PENDING")],
  "constraints":[("C-01","Launch site approval","OWNER REQUIRED","HIGH"),("C-02","Airspace coordination","NOT STARTED","HIGH"),("C-03","Recovery deployment evidence","OPEN","MEDIUM"),("C-04","Final vehicle mass properties","OPEN","MEDIUM")]
 }
 return render_template("ops.html",view="dashboard",counts=counts,programmes=programmes,missions=missions,events=events,workspace=workspace)

@app.get("/")
def home():
 return redirect(url_for("control.console"))

@app.route("/programmes",methods=["GET","POST"])
def programmes():
 init_db()
 if request.method=="POST":
  code=request.form.get("code","").strip().upper(); name=request.form.get("name","").strip(); objective=request.form.get("objective","").strip()
  if not all((code,name,objective)): flash("Code, name and objective are required.","error")
  else:
   ident,stamp=str(uuid.uuid4()),now()
   try:
    with connect() as c:
     c.execute("INSERT INTO programmes(id,organisation_id,code,name,objective,lifecycle_state,created_at,updated_at) VALUES(?,?,?,?,?,'DRAFT',?,?)",(ident,ORG,code,name,objective,stamp,stamp)); audit(c,"programme",ident,"PROGRAMME_CREATED",'{"state":"DRAFT"}')
    flash(f"Programme {code} created in DRAFT.","success"); return redirect(url_for("programmes"))
   except sqlite3.IntegrityError: flash("Programme code already exists.","error")
 with connect() as c: rows=c.execute("SELECT * FROM programmes WHERE archived_at IS NULL ORDER BY updated_at DESC").fetchall()
 return render_template("ops.html",view="programmes",rows=rows)

@app.route("/missions",methods=["GET","POST"])
def missions():
 init_db()
 if request.method=="POST":
  f={k:request.form.get(k,"").strip() for k in ("programme_id","code","name","mission_type","objective","success_criteria")}; f["code"]=f["code"].upper()
  if not all(f.values()): flash("All mission fields and measurable success criteria are required.","error")
  else:
   ident,stamp=str(uuid.uuid4()),now()
   try:
    with connect() as c:
     c.execute("INSERT INTO missions(id,organisation_id,programme_id,code,name,mission_type,objective,success_criteria,lifecycle_state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,'CONCEPT',?,?)",(ident,ORG,f["programme_id"],f["code"],f["name"],f["mission_type"],f["objective"],f["success_criteria"],stamp,stamp)); audit(c,"mission",ident,"MISSION_CREATED",'{"state":"CONCEPT"}')
    flash(f"Mission {f['code']} created in CONCEPT.","success"); return redirect(url_for("missions"))
   except sqlite3.IntegrityError: flash("Mission code is already used or programme is invalid.","error")
 with connect() as c:
  rows=c.execute("SELECT m.*,p.code programme_code FROM missions m JOIN programmes p ON p.id=m.programme_id ORDER BY m.updated_at DESC").fetchall(); parents=c.execute("SELECT id,code,name FROM programmes WHERE archived_at IS NULL ORDER BY code").fetchall()
 return render_template("ops.html",view="missions",rows=rows,parents=parents)

@app.get("/module/<key>")
def module(key):
 init_db(); meta=next((x for x in MODULES if x[0]==key),None)
 if not meta or key in ("programmes","missions"): return redirect(url_for("command_center"))
 tables={"configuration":("configuration_items","Product records"),"manufacturing":("serialised_assets","Serialized assets"),"tests":("test_campaigns","Test campaigns"),"launch":("launch_campaigns","Launch campaigns"),"safety":("hazards","Hazard records"),"documents":("controlled_documents","Controlled documents")}
 table,metric=tables[key]
 with connect() as c: count=c.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
 return render_template("ops.html",view="module",key=key,title=meta[1],subtitle=meta[2],count=count,metric=metric)

@app.get("/health")
def health(): init_db(); return {"status":"ok","service":"stellar-ops","database":"ready"}
if __name__=="__main__": init_db(); app.run(host="0.0.0.0",port=int(os.environ.get("PORT","5001")),debug=os.environ.get("FLASK_DEBUG")=="1")
