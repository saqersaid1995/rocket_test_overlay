from __future__ import annotations

import hashlib
from pathlib import Path

from .documents import safe_token


def validate_execution_pack(operation: dict, scope_kind: str, scope: dict, tasks: list[dict], release: bool = False) -> dict:
    blockers, warnings = [], []
    if scope_kind not in {"DEPARTMENT", "PERSON"}: blockers.append("Execution pack scope is invalid.")
    if not scope: blockers.append("A controlled recipient is required.")
    if not tasks: blockers.append("The recipient has no assigned controlled work.")
    if not operation.get("planned_start"): blockers.append("Operation planned start is not defined.")
    if scope_kind == "PERSON":
        if not scope.get("person_name") or scope.get("person_name") == "UNASSIGNED": blockers.append("Named recipient is not assigned.")
        if scope.get("qualification_status") != "CURRENT":
            warnings.append("Recipient qualification is not CURRENT.")
            if release: blockers.append("Recipient qualification must be CURRENT for controlled distribution.")
    for task in tasks:
        if task.get("status") == "BLOCKED": blockers.append(f"{task['task_code']}: task is blocked.")
        if task.get("status") != "ACCEPTED": warnings.append(f"{task['task_code']}: task is {task.get('status','UNKNOWN').replace('_',' ')}.")
        if task.get("safety_critical") and not task.get("required_evidence"): blockers.append(f"{task['task_code']}: safety-critical evidence requirement is missing.")
    for label, record, expected in (("Baseline",operation.get("baseline"),"RELEASED"),("Staffing",operation.get("staffing"),"APPROVED"),
                                    ("Procedure",operation.get("procedure"),"APPROVED"),("Safety case",operation.get("safety_case"),"APPROVED")):
        if (record or {}).get("state") != expected:
            warnings.append(f"{label} is not {expected}.")
            if release: blockers.append(f"{label} must be {expected} before controlled distribution.")
    return {"release_ready":not blockers,"blockers":list(dict.fromkeys(blockers)),"warnings":list(dict.fromkeys(warnings)),
            "summary":{"tasks":len(tasks),"accepted":sum(x.get("status")=="ACCEPTED" for x in tasks),
                       "safety_critical":sum(bool(x.get("safety_critical")) for x in tasks),"evidence":sum(len(x.get("evidence",[])) for x in tasks)}}


def _p(value):
    return str(value or "-").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace("\n","<br/>")


def build_execution_pack_pdf(path: Path, operation: dict, scope_kind: str, scope: dict, tasks: list[dict], verification: list[dict], metadata: dict) -> None:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    ink, cyan, muted, line, pale = [colors.HexColor(x) for x in ("#071A22","#00A8D6","#52717C","#B8CBD2","#EDF5F7")]
    styles=getSampleStyleSheet()
    styles.add(ParagraphStyle(name="XTitle",parent=styles["Title"],fontName="Helvetica-Bold",fontSize=22,leading=27,textColor=ink))
    styles.add(ParagraphStyle(name="X1",parent=styles["Heading1"],fontName="Helvetica-Bold",fontSize=15,leading=19,textColor=ink,spaceAfter=7))
    styles.add(ParagraphStyle(name="X2",parent=styles["Heading2"],fontName="Helvetica-Bold",fontSize=10,leading=13,textColor=cyan,spaceBefore=8,spaceAfter=4))
    styles.add(ParagraphStyle(name="XB",parent=styles["BodyText"],fontSize=8.2,leading=11.5,textColor=ink))
    styles.add(ParagraphStyle(name="XS",parent=styles["BodyText"],fontSize=7,leading=9,textColor=muted))
    def footer(canvas,doc):
        canvas.saveState();canvas.setStrokeColor(line);canvas.line(16*mm,13*mm,194*mm,13*mm);canvas.setFont("Helvetica",7);canvas.setFillColor(muted)
        canvas.drawString(16*mm,8*mm,f"{metadata['pack_code']} | ISSUE {metadata['issue']} | {metadata['state']} | ASSIGNED COPY")
        canvas.drawRightString(194*mm,8*mm,f"Page {doc.page}")
        if metadata["state"]=="DRAFT":
            canvas.setFillColor(colors.Color(.8,.1,.1,alpha=.07));canvas.setFont("Helvetica-Bold",46);canvas.translate(105*mm,148*mm);canvas.rotate(35);canvas.drawCentredString(0,0,"DRAFT - FOR REVIEW")
        canvas.restoreState()
    def table(headers,rows,widths=None):
        data=[[Paragraph(_p(x),styles["XS"]) for x in headers]]+[[Paragraph(_p(x),styles["XS"]) for x in row] for row in rows]
        t=Table(data,repeatRows=1,colWidths=widths);t.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),ink),("TEXTCOLOR",(0,0),(-1,0),colors.white),("GRID",(0,0),(-1,-1),.35,line),("VALIGN",(0,0),(-1,-1),"TOP"),("PADDING",(0,0),(-1,-1),4),("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white,pale])]))
        return t
    recipient=scope.get("person_name") if scope_kind=="PERSON" else scope.get("name")
    doc=SimpleDocTemplate(str(path),pagesize=A4,leftMargin=16*mm,rightMargin=16*mm,topMargin=15*mm,bottomMargin=19*mm,title=f"{recipient} Execution Pack",author="Stellar Mission & Test Control")
    story=[Spacer(1,20*mm),Paragraph("STELLAR MISSION & TEST CONTROL",styles["X2"]),Spacer(1,5*mm),Paragraph("Targeted Execution Pack",styles["XTitle"]),Paragraph(f"{_p(operation['code'])} - {_p(operation['title'])}",styles["X2"]),Spacer(1,10*mm)]
    identity=[["RECIPIENT",recipient,"SCOPE",scope_kind],["ROLE / FUNCTION",scope.get("role_code") or scope.get("code"),"CALL SIGN",scope.get("call_sign") or "FUNCTION NET"],["PLANNED START",operation.get("planned_start"),"ISSUE STATE",metadata["state"]],["PACK CODE",metadata["pack_code"],"ISSUED BY",metadata["issued_by"]]]
    t=Table(identity,colWidths=[32*mm,56*mm,32*mm,58*mm]);t.setStyle(TableStyle([("GRID",(0,0),(-1,-1),.5,line),("BACKGROUND",(0,0),(0,-1),ink),("BACKGROUND",(2,0),(2,-1),ink),("TEXTCOLOR",(0,0),(0,-1),colors.white),("TEXTCOLOR",(2,0),(2,-1),colors.white),("FONT",(0,0),(-1,-1),"Helvetica",8),("PADDING",(0,0),(-1,-1),6)]));story += [t,Spacer(1,6*mm),Paragraph("This pack is a role-specific extract. The master handbook and live controlled system remain authoritative. Stop and escalate any conflict or changed condition.",styles["XS"]),PageBreak()]
    story += [Paragraph("1. Command, authority and communications",styles["X1"])]
    assignments=(operation.get("staffing") or {}).get("assignments",[])
    story += [table(["Role","Person","Call sign","Authority","Contact"],[[x.get("role_code"),x.get("person_name"),x.get("call_sign"),x.get("authority_scope"),x.get("contact_method")] for x in assignments],[20*mm,37*mm,25*mm,60*mm,36*mm]),Paragraph("STOP-WORK RULE",styles["X2"]),Paragraph(_p((operation.get("safety_case") or {}).get("emergency_policy") or "Any unsafe or unclear condition requires HOLD and escalation to the Test Director / RSO."),styles["XB"]),PageBreak()]
    story += [Paragraph("2. Assigned work and timeline",styles["X1"]),table(["Task","Phase","Controlled instruction","Due","A / V","Status"],[[x.get("task_code"),x.get("phase"),x.get("description"),x.get("due_at"),f"{x.get('accountable_role')} / {x.get('verifier_role')}",x.get("status")] for x in tasks],[21*mm,21*mm,70*mm,34*mm,20*mm,22*mm])]
    for i,task in enumerate(tasks,1):
        story += [PageBreak(),Paragraph(f"{i+2}. {_p(task['task_code'])} - {_p(task['title'])}",styles["X1"]),
                  table(["Control","Requirement"],[["Responsible",f"{task.get('responsible_role')} / {task.get('assigned_person')}"],["Accountable",task.get("accountable_role")],["Independent verifier",task.get("verifier_role")],["Inputs",task.get("required_inputs")],["Acceptance criteria",task.get("acceptance_criteria")],["Evidence required",task.get("required_evidence")],["Escalation",task.get("blocker") or "Set task BLOCKED and notify accountable authority before deviating."]],[38*mm,140*mm])]
    story += [PageBreak(),Paragraph("Safety hazards and HOLD interfaces",styles["X1"])]
    hazard_rows=[]
    for h in operation.get("hazards",[]): hazard_rows.append([h.get("hazard_code"),h.get("title"),h.get("residual_risk"),h.get("owner_role"),"; ".join(c.get("description","") for c in h.get("controls",[]))])
    story += [table(["Hazard","Title","Residual","Owner","Controls"],hazard_rows,[20*mm,35*mm,20*mm,22*mm,81*mm]),Paragraph("Formal HOLD points",styles["X2"])]
    holds=[s.get("hold_point")|{"step_code":s.get("step_code")} for s in (operation.get("procedure") or {}).get("steps",[]) if s.get("hold_point")]
    story += [table(["Hold","Step","Trigger","Safe state","Release authority"],[[x.get("hold_code"),x.get("step_code"),x.get("trigger_condition"),x.get("safe_state"),x.get("release_authority")] for x in holds],[23*mm,20*mm,52*mm,55*mm,28*mm]),PageBreak(),Paragraph("Evidence and independent verification",styles["X1"]),table(["Task","Required evidence","Current records","Verifier"],[[x.get("task_code"),x.get("required_evidence"),len(x.get("evidence",[])),x.get("verifier_role")] for x in tasks],[25*mm,95*mm,25*mm,33*mm])]
    if verification: story += [Paragraph("Your verification queue",styles["X2"]),table(["Task","Performed by","Acceptance","Evidence"],[[x.get("task_code"),x.get("assigned_person"),x.get("acceptance_criteria"),x.get("required_evidence")] for x in verification],[25*mm,40*mm,65*mm,48*mm])]
    story += [Spacer(1,10*mm),Paragraph("Recipient acknowledgement",styles["X1"]),Paragraph("I confirm that I received this issue, understand my responsibilities, authority limits, HOLD obligations, evidence requirements and escalation path. I will verify the current controlled issue before execution.",styles["XB"]),Spacer(1,12*mm),table(["Recipient signature","Date / time","Briefed by"],[["","",metadata["issued_by"]]],[70*mm,50*mm,58*mm])]
    doc.build(story,onFirstPage=footer,onLaterPages=footer)


def create_execution_pack(directory: Path, operation: dict, scope_kind: str, scope: dict, tasks: list[dict], verification: list[dict], metadata: dict) -> dict:
    directory.mkdir(parents=True,exist_ok=True); path=directory/f"{safe_token(metadata['pack_code'])}-I{metadata['issue']:03d}.pdf"
    build_execution_pack_pdf(path,operation,scope_kind,scope,tasks,verification,metadata)
    return {"filename":path.name,"storage_path":str(path.resolve()),"mime_type":"application/pdf","byte_size":path.stat().st_size,"sha256":hashlib.sha256(path.read_bytes()).hexdigest()}
