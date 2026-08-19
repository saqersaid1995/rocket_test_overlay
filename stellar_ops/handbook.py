from __future__ import annotations

import hashlib
from pathlib import Path

from .documents import safe_token


CHAPTERS = (
    ("DOCUMENT_CONTROL", "Document Control", True), ("OPERATION_OVERVIEW", "Operation Overview", True),
    ("OBJECTIVES", "Objectives and Success Criteria", False), ("TIMELINE", "Mission Timeline", False),
    ("ORGANIZATION", "Organization and Command Authority", True), ("RACI", "RACI Matrix", False),
    ("SITE", "Site and Exclusion Zone", True), ("CONFIGURATION", "Configuration and Equipment", True),
    ("INSTRUMENTATION", "Instrumentation and Recording", True), ("COMMUNICATIONS", "Communications Plan", False),
    ("SAFETY", "Hazards and Safety Controls", True), ("PROCEDURE", "Controlled Procedures", True),
    ("HOLD_ABORT", "HOLD and Abort Logic", True), ("EMERGENCY", "Emergency Response", True),
    ("EVIDENCE", "Evidence and Records", True), ("SAFING", "Post-Operation Safing", True),
    ("APPENDICES", "Appendices and References", False),
)


def source_status(operation: dict, key: str) -> tuple[str, str]:
    mapping = {
        "DOCUMENT_CONTROL": (bool(operation.get("baseline")), "Configuration baseline"),
        "OPERATION_OVERVIEW": (bool(operation.get("objective")), "Operation register"),
        "OBJECTIVES": (bool(operation.get("success_criteria")), "Operation register"),
        "TIMELINE": (bool(operation.get("planned_start")), "Master plan"),
        "ORGANIZATION": (bool((operation.get("staffing") or {}).get("assignments")), "Team & Authority"),
        "RACI": (bool(operation.get("planning_tasks")), "Master plan"),
        "SITE": (bool(operation.get("site")), "Operation register / Safety"),
        "CONFIGURATION": (bool(operation.get("baseline")), "Released baseline"),
        "INSTRUMENTATION": (bool(operation.get("instrumentation")) and bool(operation.get("video_plan")), "Instrumentation / Video"),
        "COMMUNICATIONS": (bool((operation.get("staffing") or {}).get("assignments")), "Team call signs; formal net plan not yet modelled"),
        "SAFETY": (bool(operation.get("safety_case")) and bool(operation.get("hazards")), "Safety assurance"),
        "PROCEDURE": (bool(operation.get("procedure")), "Approved procedure"),
        "HOLD_ABORT": (any(s.get("hold_point") for s in (operation.get("procedure") or {}).get("steps", [])), "Procedure hold points"),
        "EMERGENCY": (bool((operation.get("safety_case") or {}).get("emergency_policy")), "Safety assurance"),
        "EVIDENCE": (bool(operation.get("planning_tasks")), "Evidence register"),
        "SAFING": (any(s.get("phase") == "SAFING" for s in (operation.get("procedure") or {}).get("steps", [])), "Controlled procedure"),
        "APPENDICES": (bool((operation.get("baseline") or {}).get("items")), "Controlled references"),
    }
    ready, source = mapping[key]
    return ("READY" if ready else "MISSING", source)


def default_chapters(operation: dict) -> list[dict]:
    result = []
    hazardous = operation.get("risk_class") == "HAZARDOUS"
    for sequence, (key, title, critical) in enumerate(CHAPTERS, 1):
        status, source = source_status(operation, key)
        result.append({"chapter_key": key, "title": title, "sequence": sequence,
                       "mandatory": int(critical and hazardous), "included": 1,
                       "source_status": status, "source_label": source, "custom_note": ""})
    return result


def validate_handbook(operation: dict, config: dict, chapters: list[dict], release: bool = False) -> dict:
    blockers, warnings = [], []
    for field, label in (("handbook_code", "handbook code"), ("revision", "revision"), ("title", "title"), ("prepared_by", "prepared by")):
        if not str(config.get(field, "")).strip(): blockers.append(f"Document {label} is required.")
    for chapter in chapters:
        if chapter["mandatory"] and not chapter["included"]:
            blockers.append(f"Mandatory chapter cannot be omitted: {chapter['title']}.")
        if chapter["included"] and chapter["source_status"] != "READY":
            warnings.append(f"{chapter['title']}: controlled source is incomplete.")
    approvals = (("baseline", "RELEASED", "Configuration baseline"), ("staffing", "APPROVED", "Team & Authority"),
                 ("procedure", "APPROVED", "Procedure"), ("safety_case", "APPROVED", "Safety assurance"),
                 ("instrumentation", "APPROVED", "Instrumentation"), ("video_plan", "APPROVED", "Video plan"))
    for key, expected, label in approvals:
        record = operation.get(key) or {}
        if record.get("state") != expected:
            warnings.append(f"{label} is not {expected}.")
    readiness = operation.get("readiness") or {}
    if readiness.get("decision") != "GO": warnings.append("Readiness review does not hold a GO decision.")
    if release: blockers.extend(warnings)
    return {"release_ready": not blockers, "blockers": list(dict.fromkeys(blockers)), "warnings": list(dict.fromkeys(warnings)),
            "summary": {"chapters": sum(bool(x["included"]) for x in chapters),
                        "ready": sum(bool(x["included"]) and x["source_status"] == "READY" for x in chapters),
                        "mandatory": sum(bool(x["mandatory"]) for x in chapters)}}


def _p(value):
    return str(value or "-").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br/>")


def build_handbook_pdf(path: Path, operation: dict, config: dict, chapters: list[dict]) -> None:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    ink, cyan, muted, line, pale = [colors.HexColor(x) for x in ("#071A22", "#00A8D6", "#52717C", "#B8CBD2", "#EDF5F7")]
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="HTitle", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=25, leading=30, textColor=ink))
    styles.add(ParagraphStyle(name="H1", parent=styles["Heading1"], fontName="Helvetica-Bold", fontSize=16, leading=20, textColor=ink, spaceAfter=8))
    styles.add(ParagraphStyle(name="H2", parent=styles["Heading2"], fontName="Helvetica-Bold", fontSize=10, leading=13, textColor=cyan, spaceBefore=8, spaceAfter=4))
    styles.add(ParagraphStyle(name="BodyX", parent=styles["BodyText"], fontSize=8.5, leading=12, textColor=ink))
    styles.add(ParagraphStyle(name="SmallX", parent=styles["BodyText"], fontSize=7, leading=9, textColor=muted))
    state = config["state"]
    def footer(canvas, doc):
        canvas.saveState(); canvas.setStrokeColor(line); canvas.line(16*mm, 13*mm, 194*mm, 13*mm)
        canvas.setFont("Helvetica", 7); canvas.setFillColor(muted)
        canvas.drawString(16*mm, 8*mm, f"{config['handbook_code']} | REV {config['revision']} | {state} | CONTROLLED WHEN VIEWED IN SMTCS")
        canvas.drawRightString(194*mm, 8*mm, f"Page {doc.page}")
        if state == "DRAFT":
            canvas.setFillColor(colors.Color(0.8,0.1,0.1,alpha=.08)); canvas.setFont("Helvetica-Bold", 52)
            canvas.translate(105*mm,148*mm); canvas.rotate(35); canvas.drawCentredString(0,0,"DRAFT - NOT RELEASED")
        canvas.restoreState()
    doc = SimpleDocTemplate(str(path), pagesize=A4, leftMargin=16*mm, rightMargin=16*mm, topMargin=15*mm, bottomMargin=19*mm,
                            title=config["title"], author="Stellar Mission & Test Control")
    story = [Spacer(1,24*mm), Paragraph("STELLAR MISSION & TEST CONTROL", styles["H2"]), Spacer(1,6*mm),
             Paragraph(_p(config["title"]), styles["HTitle"]), Spacer(1,3*mm),
             Paragraph(f"{_p(operation['code'])} - {_p(operation['title'])}", styles["H2"]), Spacer(1,12*mm)]
    control = [["DOCUMENT CODE", config["handbook_code"], "REVISION", config["revision"]], ["STATE", state, "TEMPLATE", config["template_key"]],
               ["PREPARED BY", config["prepared_by"], "APPROVED BY", config.get("approved_by") or "NOT RELEASED"],
               ["CLASSIFICATION", config["distribution_classification"], "GENERATED", config["generated_at"]]]
    t=Table(control,colWidths=[32*mm,56*mm,32*mm,58*mm]); t.setStyle(TableStyle([("GRID",(0,0),(-1,-1),.5,line),("BACKGROUND",(0,0),(0,-1),ink),("BACKGROUND",(2,0),(2,-1),ink),("TEXTCOLOR",(0,0),(0,-1),colors.white),("TEXTCOLOR",(2,0),(2,-1),colors.white),("FONT",(0,0),(-1,-1),"Helvetica",8),("VALIGN",(0,0),(-1,-1),"MIDDLE"),("PADDING",(0,0),(-1,-1),6)]))
    story += [t, Spacer(1,8*mm), Paragraph("Verify the current revision and operational release state before use. This handbook does not itself command field hardware.",styles["SmallX"]),PageBreak()]

    def table(headers, rows, widths=None):
        data=[[Paragraph(_p(x),styles["SmallX"]) for x in headers]]+[[Paragraph(_p(x),styles["SmallX"]) for x in row] for row in rows]
        obj=Table(data,repeatRows=1,colWidths=widths); obj.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),ink),("TEXTCOLOR",(0,0),(-1,0),colors.white),("GRID",(0,0),(-1,-1),.35,line),("VALIGN",(0,0),(-1,-1),"TOP"),("PADDING",(0,0),(-1,-1),4),("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white,pale])]))
        return obj
    for chapter in chapters:
        if not chapter["included"]: continue
        key=chapter["chapter_key"]; story += [Paragraph(f"{chapter['sequence']:02d}. {_p(chapter['title'])}",styles["H1"]),Paragraph(f"CONTROLLED SOURCE: {_p(chapter['source_label'])} | STATUS: {chapter['source_status']}",styles["SmallX"])]
        if chapter.get("custom_note"): story += [Paragraph("Operation-specific direction",styles["H2"]),Paragraph(_p(chapter["custom_note"]),styles["BodyX"])]
        if key=="OPERATION_OVERVIEW": story += [table(["Field","Controlled value"],[["Mission",operation.get("mission_name")],["Site",operation.get("site")],["Planned start",operation.get("planned_start")],["Owner",operation.get("owner")],["Risk",operation.get("risk_class")],["Objective",operation.get("objective")]], [38*mm,140*mm])]
        elif key=="OBJECTIVES": story += [table(["#","Success criterion"],[[i,x] for i,x in enumerate(operation.get("success_criteria",[]),1)],[14*mm,164*mm])]
        elif key in {"ORGANIZATION","COMMUNICATIONS"}: story += [table(["Role","Person","Authority","Call sign","Primary contact"],[[x.get("role_code"),x.get("person_name"),x.get("authority_scope"),x.get("call_sign"),x.get("contact") or "NOT RECORDED"] for x in (operation.get("staffing") or {}).get("assignments",[])],[22*mm,38*mm,50*mm,28*mm,40*mm])]
        elif key in {"TIMELINE","RACI"}: story += [table(["Task","Phase","Responsible","Accountable","Verifier","Due","Status"],[[x.get("task_code"),x.get("phase"),x.get("responsible_role"),x.get("accountable_role"),x.get("verifier_role"),x.get("due_at"),x.get("status")] for x in operation.get("planning_tasks",[])],[22*mm,22*mm,25*mm,25*mm,22*mm,39*mm,23*mm])]
        elif key=="CONFIGURATION": story += [table(["Item","Reference","Revision","Source","Status"],[[x.get("item_type"),x.get("controlled_reference"),x.get("revision"),x.get("source"),x.get("verification_status")] for x in (operation.get("baseline") or {}).get("items",[])],[32*mm,45*mm,22*mm,40*mm,39*mm])]
        elif key=="INSTRUMENTATION": story += [Paragraph("Measurement requirements",styles["H2"]),table(["Code","Measurement","Unit","Rate","Limits","E2E"],[[x.get("measurement_code"),x.get("name"),x.get("unit"),x.get("sample_rate_hz"),f"W {x.get('warning_limit')} / C {x.get('critical_limit')} / A {x.get('abort_limit')}",x.get("end_to_end_status")] for x in (operation.get("instrumentation") or {}).get("measurements",[])],[27*mm,42*mm,18*mm,20*mm,47*mm,24*mm]),Paragraph("Required camera views",styles["H2"]),table(["View","Purpose","Position","Recording","Status"],[[x.get("view_code"),x.get("purpose"),x.get("position"),x.get("recording_profile"),x.get("test_status")] for x in (operation.get("video_plan") or {}).get("views",[])])]
        elif key=="SAFETY":
            rows=[]
            for h in operation.get("hazards",[]):
                rows.append([h.get("hazard_code"),h.get("title"),f"{h.get('inherent_risk')} -> {h.get('residual_risk')}",h.get("owner_role"),"; ".join(c.get("control_code","")+": "+c.get("description","") for c in h.get("controls",[]))])
            story += [table(["Hazard","Title","Risk","Owner","Verified controls"],rows,[22*mm,38*mm,24*mm,23*mm,71*mm])]
        elif key in {"PROCEDURE","HOLD_ABORT","SAFING"}:
            steps=(operation.get("procedure") or {}).get("steps",[])
            if key=="SAFING": steps=[x for x in steps if x.get("phase")=="SAFING"]
            if key=="HOLD_ABORT": steps=[x for x in steps if x.get("hold_point") or x.get("safety_critical")]
            story += [table(["Step","Phase","Instruction","Responsible","Verification","Hold / safe action"],[[x.get("step_code"),x.get("phase"),x.get("instruction"),x.get("responsible_role"),x.get("verification_method"),((x.get("hold_point") or {}).get("safe_state") or x.get("abort_action"))] for x in steps],[20*mm,22*mm,65*mm,23*mm,25*mm,23*mm])]
        elif key=="EMERGENCY": story += [Paragraph(_p((operation.get("safety_case") or {}).get("emergency_policy") or "NOT DEFINED"),styles["BodyX"])]
        elif key=="EVIDENCE": story += [table(["Task","Evidence","Reference","Custodian","Status"],[[t.get("task_code"),e.get("title"),e.get("reference"),e.get("supplied_by"),e.get("status")] for t in operation.get("planning_tasks",[]) for e in t.get("evidence",[])])]
        elif key=="APPENDICES": story += [Paragraph("Canonical fingerprints",styles["H2"]),table(["Record","State","SHA-256"],[["Baseline",(operation.get("baseline") or {}).get("state"),(operation.get("baseline") or {}).get("canonical_sha256")],["Procedure",(operation.get("procedure") or {}).get("state"),(operation.get("procedure") or {}).get("canonical_sha256")],["Safety",(operation.get("safety_case") or {}).get("state"),(operation.get("safety_case") or {}).get("canonical_sha256")]])]
        elif key=="DOCUMENT_CONTROL": story += [table(["Control","Value"],[["Handbook",config["handbook_code"]],["Revision",config["revision"]],["State",state],["Prepared by",config["prepared_by"]],["Checked by",config.get("checked_by") or "NOT RECORDED"],["Approved by",config.get("approved_by") or "NOT RELEASED"]],[40*mm,138*mm])]
        elif key=="SITE": story += [Paragraph(f"Controlled site: <b>{_p(operation.get('site'))}</b>. Exclusion-zone authority and hazard controls remain governed by Safety Assurance.",styles["BodyX"])]
        story.append(PageBreak())
    doc.build(story,onFirstPage=footer,onLaterPages=footer)


def create_handbook_file(directory: Path, operation: dict, config: dict, chapters: list[dict]) -> dict:
    directory.mkdir(parents=True,exist_ok=True)
    path=directory/f"{safe_token(config['handbook_code'])}-{safe_token(config['revision'])}.pdf"
    build_handbook_pdf(path,operation,config,chapters)
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    return {"filename":path.name,"storage_path":str(path.resolve()),"mime_type":"application/pdf","byte_size":path.stat().st_size,"sha256":digest}
