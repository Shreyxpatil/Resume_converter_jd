from src.keyword_highlighter import bold_skill_mentions, dedupe_skill_lines

BASE_HTML = """
<html><body><div class="resume-container">
<div class="skill-line"><strong>Languages:</strong> Python, Docker, Kubernetes</div>
<p class="summary-text">Backend engineer experienced with Python and Docker.</p>
<div class="entry-block" data-section-id="work_experience_0">
    <ul class="bullets">
        <li>Built a service in Python using Docker containers</li>
        <li>Migrated infra to Kubernetes for better scaling</li>
    </ul>
</div>
</div></body></html>
"""


def test_bolds_skill_terms_in_summary():
    result = bold_skill_mentions(BASE_HTML)
    assert "<strong>Python</strong>" in result
    assert "<strong>Docker</strong>" in result


def test_bolds_skill_terms_in_bullets():
    result = bold_skill_mentions(BASE_HTML)
    assert "Built a service in <strong>Python</strong> using <strong>Docker</strong> containers" in result
    assert "Migrated infra to <strong>Kubernetes</strong> for better scaling" in result


def test_does_not_bold_inside_existing_skill_line_label():
    # The "Languages:" skill-line itself is not a target (only summary/bullets are).
    result = bold_skill_mentions(BASE_HTML)
    assert "<strong>Languages:</strong> Python, Docker, Kubernetes" in result


def test_extra_terms_get_bolded_too():
    html = '<html><body><div class="resume-container"><p class="summary-text">Uses Rust for performance.</p></div></body></html>'
    result = bold_skill_mentions(html, extra_terms=["Rust"])
    assert "<strong>Rust</strong>" in result


def test_idempotent_no_double_wrap_on_second_call():
    once = bold_skill_mentions(BASE_HTML)
    twice = bold_skill_mentions(once)
    assert twice.count("<strong>Python</strong>") == once.count("<strong>Python</strong>")
    assert "<strong><strong>" not in twice


def test_does_not_bold_text_already_inside_a_link():
    html = """
    <html><body><div class="resume-container">
    <ul class="bullets"><li>Built <a href="https://x.com">Python tooling</a> for the team</li></ul>
    </div></body></html>
    """
    result = bold_skill_mentions(html, extra_terms=["Python"])
    assert "<strong>Python</strong>" not in result
    assert '<a href="https://x.com">Python tooling</a>' in result


def test_no_terms_returns_html_unchanged():
    html = '<html><body><div class="resume-container"><p class="summary-text">No skills listed here.</p></div></body></html>'
    assert bold_skill_mentions(html) == html


def test_skips_symbol_containing_terms():
    html = """
    <html><body><div class="resume-container">
    <div class="skill-line"><strong>Languages:</strong> C++, Node.js</div>
    <p class="summary-text">Experienced with C++ and Node.js development.</p>
    </div></body></html>
    """
    result = bold_skill_mentions(html)
    assert "<strong>C++</strong>" not in result
    assert "<strong>Node.js</strong>" not in result


def test_case_insensitive_matching():
    html = '<html><body><div class="resume-container"><div class="skill-line">Python</div><p class="summary-text">expert in python development</p></div></body></html>'
    result = bold_skill_mentions(html)
    assert "<strong>python</strong>" in result


def test_multi_word_term_matches_before_short_substring():
    html = """
    <html><body><div class="resume-container">
    <div class="skill-line">Machine Learning, Learning</div>
    <p class="summary-text">Focused on Machine Learning research.</p>
    </div></body></html>
    """
    result = bold_skill_mentions(html)
    assert "<strong>Machine Learning</strong>" in result
    assert "Machine <strong>Learning</strong>" not in result


DUPLICATE_SKILLS_HTML = """
<html><body><div class="resume-container">
<div class="skill-line" data-section-id="skill_0"><strong>Vector Stores &amp; Data Systems:</strong> Pinecone, ChromaDB, vector databases, pgvector</div>
<div class="skill-line" data-section-id="skill_1"><strong>Databases:</strong> Pinecone, ChromaDB, Vector Database, SQL</div>
<div class="skill-line"><strong>Tech Stack:</strong> Pinecone, ChromaDB</div>
</div></body></html>
"""


def test_dedupe_skill_lines_removes_exact_duplicate_across_categories():
    result = dedupe_skill_lines(DUPLICATE_SKILLS_HTML)
    databases_line = [l for l in result.splitlines() if "Databases:" in l][0]
    assert "Pinecone" not in databases_line
    assert "ChromaDB" not in databases_line
    assert "SQL" in databases_line


def test_dedupe_skill_lines_removes_case_spacing_and_plural_variants():
    # "vector databases" (first category) and "Vector Database" (second
    # category) must be recognized as the same skill despite the casing/
    # spacing/pluralization difference.
    result = dedupe_skill_lines(DUPLICATE_SKILLS_HTML)
    assert result.count("vector databases") + result.count("Vector Database") == 1


def test_dedupe_skill_lines_never_touches_tech_stack_lines():
    # Tech Stack lines (no skill_N data-section-id) are per-project and
    # expected to repeat overlapping tech — not a bug to fix.
    result = dedupe_skill_lines(DUPLICATE_SKILLS_HTML)
    assert '<strong>Tech Stack:</strong> Pinecone, ChromaDB' in result


def test_dedupe_skill_lines_removes_category_left_empty():
    html = """
    <html><body><div class="resume-container">
    <div class="skill-line" data-section-id="skill_0"><strong>A:</strong> Python</div>
    <div class="skill-line" data-section-id="skill_1"><strong>B:</strong> python</div>
    </div></body></html>
    """
    result = dedupe_skill_lines(html)
    assert "Python" in result
    assert "<strong>B:</strong>" not in result


def test_dedupe_skill_lines_no_duplicates_returns_html_unchanged():
    html = '<html><body><div class="resume-container"><div class="skill-line" data-section-id="skill_0"><strong>A:</strong> Python, Docker</div></div></body></html>'
    assert dedupe_skill_lines(html) == html
