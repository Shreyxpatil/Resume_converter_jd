# Resume templates

Two templates, each paired in `app.py`'s `TEMPLATES` with a parser and a DOCX converter:

| Choice       | Template                                  | Parser                          | DOCX converter                    |
|--------------|-------------------------------------------|---------------------------------|-----------------------------------|
| `general`    | `resume_template_general.jinja2.html`     | `src/resume_parser_general.py`  | `src/docx_converter_general.py`   |
| `salesforce` | `resume_template_salesforce.jinja2.html`  | `src/resume_parser_salesforce.py` | `src/docx_converter_salesforce.py` |

## Feature hooks

The ATS check, keyword tools and the in-preview section editor read the rendered HTML through these
hooks. They're extra classes and attributes only, so they don't change how a template looks.

| Hook | Where | Read by |
|------|-------|---------|
| `.name` | candidate name | output filename, ATS name-position check |
| `.skill-line` | each skills row/line (`<strong>Area:</strong> a, b` or a plain comma list) | keyword bolding, de-duplication (`skill_N` ids only), resume-skill extraction |
| `.entry-block` + `.entry-title` | one project row and its title | ATS reading-order check |
| `data-section-id` | any editable unit | section editor (Edit / Ask AI / Delete). Ids ending `_N` get a "+ Add" button |
| `data-add-label` | entry (optional) | label for that "+ Add" button |

Bullets are plain `<li>`s inside the entry. The keyword highlighter may wrap terms in `<strong>`, and
both converters render that as bold runs.
