"""Resume parser for the Salesforce template — a two-column table of
Salesforce-specific skill rows (CRM admin, clouds, components, ...) plus
certification badges, then one row per project with full-length
descriptions and durations expressed in months."""

import re
from datetime import datetime

from src import resume_parser_common as common
from src.docx_converter_salesforce import matching_badges
from src.logging_config import get_logger

logger = get_logger(__name__)

# Template variable -> JSON field the model is asked for.
SKILL_FIELDS = {
    "crm_administration": "crm_administration",
    "certifications": "certifications",
    "salesforce_expertise": "salesforce_expertise",
    "languages": "languages",
    "salesforce_components": "salesforce_components",
    "ticketing": "ticketing_case_management",
    "database": "database",
    "salesforce_clouds": "salesforce_clouds",
    "soft_skills": "soft_skills",
    "development_tools": "development_tool",
}


# =========================
# DURATION → MONTHS
# =========================
def calculate_duration_string(duration_raw, now=None):
    if not duration_raw or str(duration_raw).strip().lower() in ["na", "none", "", "n/a"]:
        return "N/A"
    duration_raw = str(duration_raw).strip()

    parts = re.split(r'\s*[-–]\s*|\s+to\s+', duration_raw, flags=re.IGNORECASE)
    if len(parts) != 2:
        return duration_raw

    start_str = parts[0].strip()
    end_str = parts[1].strip()

    def parse_date(d_str):
        if d_str.lower() in ["present", "current", "now", "ongoing"]:
            return now or datetime.now()
        formats = ["%b %Y", "%B %Y", "%b-%Y", "%B-%Y", "%m/%Y", "%Y", "%b %d, %Y", "%B %d, %Y"]
        for fmt in formats:
            try:
                return datetime.strptime(d_str, fmt)
            except ValueError:
                continue
        return None

    start_date = parse_date(start_str)
    end_date = parse_date(end_str)

    if start_date and end_date:
        diff_months = (end_date.year - start_date.year) * 12 + (end_date.month - start_date.month) + 1
        return f"{max(diff_months, 1)} Months"

    return duration_raw


# =========================
# PROJECT TRANSFORM
# (LONG DESCRIPTIONS)
# =========================
def transform_projects_data(projects_list):
    if not isinstance(projects_list, list):
        return []

    transformed = []
    for project in projects_list:
        if not isinstance(project, dict):
            continue
        idx = len(transformed) + 1

        clouds = str(project.get("Clouds") or "").strip()
        extra = clouds if clouds not in ["NA", "None", ""] else ""

        transformed.append({
            "title": f"Project {idx}: ({extra})" if extra else f"Project {idx}:",
            "role": project.get("Role") or "",
            "industry": project.get("Industry") or "NA",
            "duration": calculate_duration_string(project.get("Duration", "")),
            "link": project.get("link") or "",
            "description": common.as_text_list(project.get("description")),
            "tech_stack": ", ".join(common.as_text_list(project.get("tech_stack"))) if isinstance(project.get("tech_stack"), list) else str(project.get("tech_stack") or ""),
        })

    return transformed


# =========================
# PROMPT
# =========================
def get_improved_extraction_prompt():
    system_prompt = """You are an expert resume parser AI.

CRITICAL EXTRACTION RULES:

1. Extract ALL bullet points for every project.
2. Do NOT summarize.
3. Do NOT shorten.
4. If project description has paragraph → split into multiple points.
5. Provide all points as it is.
6. Preserve full sentences.
7. Return long descriptions exactly as written.
8. Duration must remain date range (parser converts to months).
9. Output ONLY JSON.
10. If certification is shortformed then return full form (e.g. 'Salesforce PD-I' → 'Salesforce Platform Developer I').
"""

    user_prompt = """Extract resume into this JSON:

{
 "Extract and Synthesize Candidate's Resume":{
   "name":"",
   "current_job_role":"",
   "experience":"in years and months format (e.g. '3 years 2 months')",
   "crm_administration":[],
   "certifications":[],
   "salesforce_expertise":[],
   "languages":[],
   "salesforce_components":[],
   "ticketing_case_management":[],
   "database":[],
   "salesforce_clouds":[],
   "soft_skills":[],
   "development_tool":[],
   "projects_descriptions":[
     {
       "project_name":"",
       "Clouds":"",
       "Role":"",
       "Industry":"it should not be na, infer industry from description instead of technology prefer IT",
       "Duration":"date range as written in the resume, e.g. 'Jan 2022 - May 2024'",
       "description":[
         "Full long point 1",
         "Full long point 2",
         "Full long point 3",
         "Full long point 4"
       ],
       "tech_stack":[],
       "link":""
     }
   ]
 }
}"""
    return system_prompt, user_prompt


# =========================
# TEMPLATE VARS
# =========================
def build_template_vars(data):
    template_vars = {
        "name": data.get("name") or "NA",
        "job_role": data.get("current_job_role") or "NA",
        "experience": data.get("experience") or "NA",
        "projects": transform_projects_data(data.get("projects_descriptions") or data.get("projects") or []),
    }
    for var_name, field in SKILL_FIELDS.items():
        template_vars[var_name] = common.join_or_na(data.get(field))
    # Same badges the DOCX header picks, so the preview shows them too.
    template_vars["badges"] = matching_badges(template_vars["certifications"])
    return template_vars


# =========================
# MAIN EXTRACTION
# =========================
def extract_resume_data(pdf_file_path, template_file_path, output_html_path, job_description=None, extra_instructions=None):
    logger.info("Starting Salesforce Resume Extraction...")
    system_prompt, user_prompt = get_improved_extraction_prompt()
    prompt = common.build_prompt(system_prompt, user_prompt, job_description, extra_instructions)

    data, resume_text = common.request_resume_json(prompt, pdf_file_path)
    if data is None:
        return False

    try:
        template_vars = build_template_vars(data)
        if not (job_description and job_description.strip()):
            # No JD: project points must be the resume's own words.
            for project in template_vars["projects"]:
                project["description"] = common.restore_verbatim_bullets(project["description"], resume_text)
        common.render_template(template_file_path, output_html_path, template_vars)
        return True
    except Exception as e:
        logger.error(f"Error saving HTML file: {e}")
        return False
