"""Renders the real General / Salesforce templates with sample data, so
tests exercise the actual markup the app produces instead of hand-written
HTML that can drift from it."""

import os

import src.resume_parser_general as parser_gen
import src.resume_parser_salesforce as parser_sf
from src.resume_parser_common import render_template

TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")

GENERAL_DATA = {
    "name": "Jane Doe",
    "current_job_role": "Backend Engineer",
    "experience": "5 Years",
    "expertise": [{
        "category_name": "Backend Development",
        "skills": [
            {"skill_name": "Languages", "details": ["Python", "Go"]},
            {"skill_name": "Cloud", "details": ["AWS", "Docker"]},
        ],
    }],
    "development_tool": ["Git", "Jira"],
    "database": ["PostgreSQL"],
    "projects_descriptions": [
        {
            "project_name": "Acme Payments",
            "Role": "Engineer",
            "Duration": "Jan 2020 - Dec 2021",
            "description": ["Built a service handling 10k requests/sec", "Reduced latency by 40% using caching"],
            "tech_stack": ["Python", "Redis"],
            "link": "https://acme.example.com",
        },
        {
            "project_name": "Data Pipeline",
            "Role": "Engineer",
            "Duration": "Jan 2022 - Dec 2023",
            "description": ["Designed ETL jobs processing 2TB daily"],
            "tech_stack": ["Spark"],
            "link": "",
        },
    ],
}

SALESFORCE_DATA = {
    "name": "Raj Kumar",
    "current_job_role": "Salesforce Developer",
    "experience": "4 years 2 months",
    "crm_administration": ["User Management", "Profiles"],
    "certifications": ["Salesforce Certified Administrator", "Salesforce Platform Developer I"],
    "salesforce_expertise": ["Apex", "LWC"],
    "languages": ["Java", "JavaScript"],
    "salesforce_components": ["Flows", "Triggers"],
    "ticketing_case_management": ["Jira"],
    "database": ["SOQL"],
    "salesforce_clouds": ["Sales Cloud", "Service Cloud"],
    "soft_skills": ["Communication"],
    "development_tool": ["VS Code"],
    "projects_descriptions": [
        {
            "project_name": "Retail CRM Rollout",
            "Clouds": "Sales Cloud",
            "Role": "Developer",
            "Industry": "Retail",
            "Duration": "Jan 2022 - Dec 2022",
            "description": ["Built Apex triggers automating lead assignment for 200 reps", "Developed LWC dashboards for sales managers"],
            "tech_stack": ["Apex", "LWC"],
            "link": "",
        },
    ],
}


def render_general_html(tmp_path, data=None):
    out = os.path.join(str(tmp_path), "general.html")
    render_template(os.path.join(TEMPLATES_DIR, "resume_template_general.jinja2.html"), out,
                    parser_gen.build_template_vars(data or GENERAL_DATA))
    with open(out, encoding="utf-8") as f:
        return out, f.read()


def render_salesforce_html(tmp_path, data=None):
    out = os.path.join(str(tmp_path), "salesforce.html")
    render_template(os.path.join(TEMPLATES_DIR, "resume_template_salesforce.jinja2.html"), out,
                    parser_sf.build_template_vars(data or SALESFORCE_DATA))
    with open(out, encoding="utf-8") as f:
        return out, f.read()
