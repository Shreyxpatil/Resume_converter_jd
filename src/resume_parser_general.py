"""Resume parser for the General (Classic) template — a two-column
company-branded table: identity rows, grouped expertise, tools, databases,
then one table row per project."""

from src import resume_parser_common as common
from src.logging_config import get_logger

logger = get_logger(__name__)

_NA_VALUES = {"", "na", "n/a", "none"}


def transform_expertise_data(expertise_list):
    if not isinstance(expertise_list, list):
        return []
    transformed = []
    for category in expertise_list:
        if not isinstance(category, dict):
            continue
        skills = []
        for skill in category.get("skills", []) or []:
            if not isinstance(skill, dict):
                continue
            area = skill.get("skill_name") or skill.get("area") or ""
            details = skill.get("details") or skill.get("technologies") or []
            technologies = ", ".join(common.as_text_list(details)) if isinstance(details, list) else str(details)
            if area or technologies:
                skills.append({"area": area, "technologies": technologies})
        if skills:
            transformed.append({"category_name": category.get("category_name", ""), "skills": skills})
    return transformed


def transform_projects_data(projects_list, fallback_role=""):
    if not isinstance(projects_list, list):
        return []
    transformed = []
    for project in projects_list:
        if not isinstance(project, dict):
            continue

        # The prompt asks the model to infer a missing role from context; if
        # it still returns nothing, fall back to the candidate's own current
        # title rather than inventing one.
        role = str(project.get("Role") or project.get("role") or "").strip()
        if role.lower() in _NA_VALUES:
            role = fallback_role

        duration = str(project.get("Duration") or project.get("duration") or "").strip()
        if duration.lower() in _NA_VALUES:
            duration = "NA"

        transformed.append({
            "title": project.get("project_name") or project.get("title") or "Project",
            "role": role,
            "duration": duration,
            "link": project.get("link") or "",
            "description": common.as_text_list(project.get("description")),
            "tech_stack": common.join_or_na(project.get("tech_stack")),
        })
    return transformed


def get_improved_extraction_prompt():
    system_prompt = """You are an expert resume parser AI.
CRITICAL RULES:
1. **Role & Duration**: Extract a Role and Duration for EVERY project.
   - **Duration Format**: Convert ALL dates to 'Mon YYYY - Mon YYYY' (e.g., 'Jan 2024 - Oct 2024'). Never invent dates that aren't in the resume.
   - If Role is not stated, INFER it from the project context.
2. **Expertise Grouping**: Structure skills exactly as the schema requests (Category -> Sub-Category -> Details).
3. **Descriptions**: Extract the text EXACTLY as it appears in the resume ("As Is"). Do not summarize or rephrase. Return as a list of strings (split by bullet points or new lines).
4. **Output**: JSON only."""

    user_prompt = """Extract the resume into this EXACT JSON structure:

{
  "Extract and Synthesize Candidate's Resume": {
    "name": "Candidate Name",
    "current_job_role": "Current Job Title",
    "experience": "Total Experience",
    "expertise": [
      {
        "category_name": "Main Category (e.g. Salesforce Development)",
        "skills": [
          {
            "skill_name": "Sub-Category (e.g. Core Salesforce)",
            "details": ["Apex", "Triggers", "LWC"]
          }
        ]
      }
    ],
    "development_tool": ["Jira", "Git"],
    "database": ["MySQL"],
    "projects_descriptions": [
      {
        "project_name": "Project Title",
        "Role": "Job Role",
        "Duration": "Jan 2023 - Dec 2023",
        "description": [
          "Exact text from resume line 1",
          "Exact text from resume line 2"
        ],
        "tech_stack": ["Java", "AWS"],
        "link": ""
      }
    ]
  }
}"""
    return system_prompt, user_prompt


def build_template_vars(data):
    name = data.get("name") or "NA"
    if name in ("Full Name", "Candidate Name"):
        name = "NA"
    job_role = data.get("current_job_role") or "NA"

    return {
        "name": name,
        "job_role": job_role,
        "experience": data.get("experience") or "NA",
        "expertise": transform_expertise_data(data.get("expertise", [])),
        "development_tools": common.join_or_na(data.get("development_tool") or data.get("development_tools")),
        "databases": common.join_or_na(data.get("database") or data.get("databases")),
        "projects": transform_projects_data(
            data.get("projects_descriptions") or data.get("projects") or [],
            fallback_role="" if job_role == "NA" else job_role,
        ),
    }


def extract_resume_data(pdf_file_path, template_file_path, output_html_path, job_description=None, extra_instructions=None):
    logger.info("Starting General Resume Extraction...")
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
