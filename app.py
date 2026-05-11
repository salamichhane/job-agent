import cgi
import io
import json
import re
import ssl
import subprocess
import sys
import tempfile
import textwrap
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib
from collections import Counter
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
PDFKIT_EXTRACTOR = ROOT / "tools" / "extract_pdf_text"
PDFKIT_EXTRACTOR_SOURCE = ROOT / "tools" / "extract_pdf_text.swift"
MAX_BODY = 12 * 1024 * 1024

STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "for", "from",
    "has", "have", "in", "is", "it", "its", "of", "on", "or", "our", "that",
    "the", "their", "this", "to", "we", "with", "you", "your", "will", "work",
    "team", "role", "job", "candidate", "experience", "years", "using",
}

SKILL_TERMS = [
    "python", "javascript", "typescript", "react", "node", "fastapi", "django",
    "flask", "sql", "postgresql", "mysql", "mongodb", "redis", "aws", "azure",
    "gcp", "docker", "kubernetes", "terraform", "ci/cd", "git", "linux",
    "machine learning", "data analysis", "excel", "tableau", "power bi",
    "salesforce", "hubspot", "seo", "sem", "copywriting", "analytics",
    "product management", "roadmap", "agile", "scrum", "stakeholder",
    "customer success", "account management", "lead generation", "cold outreach",
    "financial modeling", "forecasting", "budgeting", "compliance", "security",
    "api", "rest", "graphql", "etl", "airflow", "spark", "pandas",
    "communication", "leadership", "project management", "operations",
]


class VisibleTextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.skip_depth = 0
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "svg"}:
            self.skip_depth += 1
        if tag == "title":
            self._in_title = True
        if tag in {"p", "div", "li", "br", "h1", "h2", "h3", "section"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "svg"} and self.skip_depth:
            self.skip_depth -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        text = data.strip()
        if not text:
            return
        if self._in_title:
            self.title += f" {text}"
        if not self.skip_depth:
            self.parts.append(text)

    def text(self):
        content = " ".join(self.parts)
        content = re.sub(r"\s+", " ", content)
        return content.strip()


def json_response(handler, payload, status=200):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def read_json(handler):
    length = int(handler.headers.get("Content-Length", "0"))
    if length > MAX_BODY:
        raise ValueError("Request body is too large.")
    raw = handler.rfile.read(length)
    return json.loads(raw.decode("utf-8") or "{}")


def fetch_job_url(url):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Please enter a valid http or https job description URL.")

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 JobApplicationAgent/1.0",
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.8,*/*;q=0.5",
        },
    )
    context = ssl.create_default_context()
    try:
        with urllib.request.urlopen(request, timeout=15, context=context) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            raw = response.read(2_000_000)
            html = raw.decode(charset, errors="replace")
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403, 429}:
            return fetch_reader_fallback(url)
        raise

    parser = VisibleTextParser()
    parser.feed(html)
    text = parser.text()
    if len(text) < 150:
        raise ValueError("The page loaded, but I could not find enough visible job description text.")
    return {"title": parser.title.strip() or parsed.netloc, "text": text[:24000], "source_url": url}


def fetch_reader_fallback(url):
    parsed = urllib.parse.urlparse(url)
    reader_url = f"https://r.jina.ai/http://{parsed.netloc}{parsed.path}"
    if parsed.query:
        reader_url += f"?{parsed.query}"

    request = urllib.request.Request(
        reader_url,
        headers={
            "User-Agent": "Mozilla/5.0 JobApplicationAgent/1.0",
            "Accept": "text/plain,*/*;q=0.5",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read(2_000_000)
            text = raw.decode("utf-8", errors="replace")
    except Exception as exc:
        raise ValueError(
            "This job board blocked automated fetching, and the reader fallback could not load it. "
            "Open the job URL in your browser, copy the visible job description, paste it into the JD box, "
            "and continue with analysis."
        ) from exc

    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    title_match = re.search(r"^Title:\s*(.+)$", text, flags=re.MULTILINE)
    text = re.sub(r"^URL Source:.*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"^Markdown Content:\s*", "", text, flags=re.MULTILINE).strip()
    if len(text) < 150:
        raise ValueError("The reader fallback loaded, but I could not find enough job description text.")
    return {
        "title": title_match.group(1).strip() if title_match else parsed.netloc,
        "text": text[:24000],
        "source_url": url,
    }


def extract_docx_text(data):
    with zipfile.ZipFile(io.BytesIO(data)) as docx:
        xml = docx.read("word/document.xml").decode("utf-8", errors="ignore")
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<[^>]+>", " ", xml)
    return re.sub(r"\s+", " ", xml).strip()


def extract_pdfish_text(data):
    macos_text = extract_pdf_text_pdfkit(data)
    if looks_like_resume_text(macos_text):
        return macos_text

    macos_text = extract_pdf_text_macos(data)
    if looks_like_resume_text(macos_text):
        return macos_text

    chunks = []

    for match in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", data, flags=re.S):
        stream = match.group(1).strip(b"\r\n")
        decoded = None
        try:
            decoded = zlib.decompress(stream)
        except zlib.error:
            try:
                decoded = zlib.decompress(stream, -15)
            except zlib.error:
                decoded = stream
        chunks.append(decoded.decode("latin-1", errors="ignore"))

    if not chunks:
        chunks.append(data.decode("latin-1", errors="ignore"))

    content = "\n".join(chunks)
    cmap = extract_tounicode_map(content)
    text = extract_pdf_text_operators(content, cmap)
    text = cleanup_extracted_text(text)
    if not looks_like_resume_text(text):
        raise ValueError(pdf_extraction_error())
    return text


def extract_pdf_text_pdfkit(data):
    if not PDFKIT_EXTRACTOR.exists() and not PDFKIT_EXTRACTOR_SOURCE.exists():
        return ""
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(data)
            tmp_path = tmp.name

        command = [str(PDFKIT_EXTRACTOR), tmp_path]
        if not PDFKIT_EXTRACTOR.exists():
            command = ["/usr/bin/swift", str(PDFKIT_EXTRACTOR_SOURCE), tmp_path]

        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        return cleanup_extracted_text(result.stdout)
    except Exception:
        return ""
    finally:
        remove_tmp_file(tmp_path)


def extract_pdf_text_macos(data):
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(data)
            tmp_path = tmp.name

        result = subprocess.run(
            ["/usr/bin/mdls", "-raw", "-name", "kMDItemTextContent", tmp_path],
            check=False,
            capture_output=True,
            text=True,
            timeout=12,
        )
        text = result.stdout.strip()
        if text and text != "(null)":
            return cleanup_extracted_text(text)
    except Exception:
        return ""
    finally:
        remove_tmp_file(tmp_path)

    return ""


def remove_tmp_file(tmp_path):
    if not tmp_path:
        return
    try:
        Path(tmp_path).unlink(missing_ok=True)
    except TypeError:
        if Path(tmp_path).exists():
            Path(tmp_path).unlink()
    except OSError:
        pass


def pdf_extraction_error():
    return (
        "I still could not extract readable text from this PDF. It may be scanned/image-based "
        "or exported with a custom font encoding that hides real text from ATS-style parsers. "
        "Try exporting the resume from Word/Google Docs as a text-based PDF, or upload .docx."
    )


def extract_tounicode_map(content):
    cmap = {}
    for block in re.findall(r"beginbfchar(.*?)endbfchar", content, flags=re.S):
        for source, target in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
            decoded = decode_hex_unicode(target)
            if decoded:
                cmap[source.upper()] = decoded

    for block in re.findall(r"beginbfrange(.*?)endbfrange", content, flags=re.S):
        for start, end, target in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
            try:
                start_int = int(start, 16)
                end_int = int(end, 16)
                target_int = int(target, 16)
            except ValueError:
                continue
            width = len(start)
            for offset, code in enumerate(range(start_int, end_int + 1)):
                mapped_hex = f"{target_int + offset:0{len(target)}X}"
                decoded = decode_hex_unicode(mapped_hex)
                if decoded:
                    cmap[f"{code:0{width}X}"] = decoded

        for start, end, array_body in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\[(.*?)\]", block, flags=re.S):
            try:
                start_int = int(start, 16)
            except ValueError:
                continue
            width = len(start)
            targets = re.findall(r"<([0-9A-Fa-f]+)>", array_body)
            for offset, target in enumerate(targets):
                decoded = decode_hex_unicode(target)
                if decoded:
                    cmap[f"{start_int + offset:0{width}X}"] = decoded
    return cmap


def extract_pdf_text_operators(content, cmap=None):
    cmap = cmap or {}
    pieces = []
    for array_match in re.finditer(r"\[(.*?)\]\s*TJ", content, flags=re.S):
        for item in re.finditer(r"\((?:\\.|[^\\()])*\)|<[0-9A-Fa-f\s]+>", array_match.group(1)):
            pieces.append(decode_pdf_string(item.group(0), cmap))
        pieces.append("\n")

    for string_match in re.finditer(r"\((?:\\.|[^\\()])*\)\s*Tj", content, flags=re.S):
        pieces.append(decode_pdf_string(string_match.group(0).rsplit(")", 1)[0] + ")", cmap))
        pieces.append("\n")

    for quote_match in re.finditer(r"\((?:\\.|[^\\()])*\)\s*['\"]", content, flags=re.S):
        pieces.append(decode_pdf_string(quote_match.group(0).rsplit(")", 1)[0] + ")", cmap))
        pieces.append("\n")

    return " ".join(part for part in pieces if part)


def decode_pdf_string(value, cmap=None):
    cmap = cmap or {}
    if isinstance(value, tuple):
        value = next((part for part in value if part), "")
    value = value.strip()
    if not value:
        return ""
    if value.startswith("<") and value.endswith(">"):
        hex_text = re.sub(r"\s+", "", value[1:-1])
        if cmap:
            mapped = decode_with_cmap(hex_text, cmap)
            if mapped:
                return mapped
        return decode_hex_unicode(hex_text)
    if value.startswith("(") and value.endswith(")"):
        value = value[1:-1]
    value = re.sub(r"\\([nrtbf])", " ", value)
    value = re.sub(r"\\([()\\])", r"\1", value)
    value = re.sub(r"\\[0-7]{1,3}", " ", value)
    return value


def decode_with_cmap(hex_text, cmap):
    hex_text = hex_text.upper()
    widths = sorted({len(key) for key in cmap}, reverse=True)
    if not widths:
        return ""
    out = []
    index = 0
    while index < len(hex_text):
        matched = False
        for width in widths:
            chunk = hex_text[index:index + width]
            if len(chunk) == width and chunk in cmap:
                out.append(cmap[chunk])
                index += width
                matched = True
                break
        if not matched:
            index += 2
    return "".join(out)


def decode_hex_unicode(hex_text):
    hex_text = re.sub(r"\s+", "", hex_text)
    if not hex_text:
        return ""
    try:
        raw = bytes.fromhex(hex_text)
    except ValueError:
        return ""
    for encoding in ("utf-16-be", "utf-8", "latin-1"):
        decoded = raw.decode(encoding, errors="ignore").strip()
        if decoded and any(char.isalnum() for char in decoded):
            return decoded
    return ""


def cleanup_extracted_text(text):
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    return text.strip()


def looks_like_resume_text(text):
    if len(text.split()) < 40:
        return False
    printable = sum(1 for char in text if char.isprintable() or char.isspace())
    if printable / max(len(text), 1) < 0.94:
        return False
    if text_quality_score(text) < 0.34:
        return False
    resume_signals = [
        "experience", "education", "skills", "project", "summary", "work",
        "email", "phone", "linkedin", "university", "manager", "engineer",
    ]
    lowered = text.lower()
    return any(signal in lowered for signal in resume_signals)


def text_quality_score(text):
    words = re.findall(r"[A-Za-z]{3,}", text)
    if not words:
        return 0
    common_words = {
        "and", "the", "for", "with", "from", "that", "this", "are", "was",
        "you", "your", "project", "projects", "experience", "skills", "work",
        "education", "company", "team", "teams", "built", "managed", "led",
        "developed", "created", "designed", "implemented", "improved", "using",
        "data", "product", "customer", "business", "engineering", "manager",
        "software", "systems", "api", "cloud", "resume", "summary",
    }
    plausible = 0
    for word in words[:600]:
        lower = word.lower()
        vowels = sum(1 for char in lower if char in "aeiou")
        ratio = vowels / max(len(lower), 1)
        has_common_shape = 0.18 <= ratio <= 0.65 and not re.search(r"([A-Za-z])\1{3,}", word)
        if lower in common_words or has_common_shape:
            plausible += 1
    return plausible / min(len(words), 600)


def extract_resume_text(filename, data):
    suffix = Path(filename or "").suffix.lower()
    if suffix == ".docx":
        return extract_docx_text(data)
    if suffix == ".pdf":
        return extract_pdfish_text(data)
    return data.decode("utf-8", errors="replace")


def normalize(text):
    return re.sub(r"[^a-z0-9+#./ -]+", " ", text.lower())


def keyword_counts(text):
    normalized = normalize(text)
    words = re.findall(r"[a-z][a-z0-9+#./-]{2,}", normalized)
    counts = Counter(w for w in words if w not in STOPWORDS and not w.isdigit())
    for term in SKILL_TERMS:
        if term in normalized:
            counts[term] += 4
    return counts


def analyze_match(job_text, resume_text):
    job_counts = keyword_counts(job_text)
    resume_counts = keyword_counts(resume_text)
    priority = [word for word, _ in job_counts.most_common(45)]
    matched = [word for word in priority if resume_counts[word] > 0]
    missing = [word for word in priority if resume_counts[word] == 0][:18]
    coverage = len(matched) / max(len(priority), 1)

    job_skills = [term for term in SKILL_TERMS if term in normalize(job_text)]
    resume_skills = [term for term in job_skills if term in normalize(resume_text)]
    skill_coverage = len(resume_skills) / max(len(job_skills), 1) if job_skills else coverage

    length_bonus = 1 if 450 <= len(resume_text.split()) <= 1100 else 0
    score = round(min(98, max(12, coverage * 62 + skill_coverage * 28 + length_bonus * 10)))

    strengths = [word for word in matched[:12]]
    sections = {
        "keyword_match": round(coverage * 100),
        "skill_match": round(skill_coverage * 100),
        "resume_length": "ATS-friendly" if length_bonus else "Could be tighter or more complete",
        "resume_words": len(resume_text.split()),
        "job_keywords_checked": len(priority),
    }
    return {
        "score": score,
        "matched_keywords": matched[:20],
        "missing_keywords": missing,
        "job_skills": job_skills[:24],
        "resume_skills": resume_skills[:24],
        "strengths": strengths,
        "sections": sections,
        "scoring_explanation": {
            "formula": "score = keyword match x 62 + skill match x 28 + resume length bonus x 10",
            "keyword_match_weight": 62,
            "skill_match_weight": 28,
            "resume_length_bonus_weight": 10,
            "keyword_match_percent": round(coverage * 100),
            "skill_match_percent": round(skill_coverage * 100),
            "resume_length_bonus_applied": bool(length_bonus),
        },
    }


def sentence_case(term):
    return " ".join(part.upper() if part in {"api", "sql", "aws", "gcp"} else part.capitalize() for part in term.split())


def tailor_resume(job_text, resume_text):
    analysis = analyze_match(job_text, resume_text)
    missing = analysis["missing_keywords"][:10]
    matched = analysis["matched_keywords"][:8]
    skill_line = sorted(set(analysis["resume_skills"] + missing[:8]))
    summary_terms = ", ".join(sentence_case(term) for term in (matched + missing)[:8])

    summary = (
        "Results-driven candidate with experience aligned to "
        f"{summary_terms}. Brings a track record of translating requirements into measurable outcomes, "
        "partnering across teams, and adapting quickly to role-specific tools and processes."
    )

    bullets = []
    for term in (missing + matched)[:8]:
        bullets.append(
            f"Applied {sentence_case(term)} in cross-functional work to improve delivery quality, "
            "reduce manual effort, and support measurable business outcomes."
        )
    if not bullets:
        bullets = [
            "Converted ambiguous requirements into clear execution plans with measurable milestones.",
            "Partnered with stakeholders to improve workflow quality, speed, and customer-facing results.",
        ]

    improved_text = f"{summary}\n\n" + "\n".join(f"- {b}" for b in bullets)
    new_score = analyze_match(job_text, resume_text + "\n" + improved_text)["score"]
    return {
        "target_score": new_score,
        "professional_summary": summary,
        "recommended_skills": [sentence_case(term) for term in skill_line[:16]],
        "resume_bullets": bullets,
        "cover_note": textwrap.fill(
            "I am excited to apply for this role because my background maps closely to the responsibilities "
            "described in the posting. I would welcome the chance to bring my experience, learning velocity, "
            "and execution discipline to your team.",
            width=92,
        ),
        "ats_notes": [
            "Use exact role keywords naturally in your summary, skills, and most relevant experience.",
            "Keep formatting simple: text headings, standard bullets, no tables for core resume content.",
            "Only claim skills and outcomes you can defend in an interview.",
        ],
    }


def parse_multipart(handler):
    form = cgi.FieldStorage(
        fp=handler.rfile,
        headers=handler.headers,
        environ={
            "REQUEST_METHOD": "POST",
            "CONTENT_TYPE": handler.headers.get("Content-Type", ""),
            "CONTENT_LENGTH": handler.headers.get("Content-Length", "0"),
        },
    )
    return form


def application_plan(payload):
    url = payload.get("application_url", "").strip()
    candidate = payload.get("candidate", {})
    if url and urllib.parse.urlparse(url).scheme not in {"http", "https"}:
        raise ValueError("Application URL must start with http or https.")
    missing = [field for field in ["name", "email", "phone"] if not candidate.get(field)]
    return {
        "status": "ready_for_review" if not missing else "needs_candidate_details",
        "missing_candidate_fields": missing,
        "application_url": url,
        "steps": [
            "Open the application page and confirm the role, company, and location.",
            "Paste the tailored resume content into your resume file, export it as PDF, and upload it.",
            "Use the generated cover note where the form asks for a message or cover letter.",
            "Review every field manually before clicking submit.",
        ],
        "guardrail": "This prototype prepares the application package and checklist, but does not click final submit without an explicit human review step.",
    }


class AgentHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/":
            path = "/index.html"
        file_path = (STATIC_DIR / path.lstrip("/")).resolve()
        if not str(file_path).startswith(str(STATIC_DIR.resolve())) or not file_path.exists():
            self.send_error(404)
            return
        content_type = "text/html"
        if file_path.suffix == ".css":
            content_type = "text/css"
        elif file_path.suffix == ".js":
            content_type = "application/javascript"
        data = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        try:
            if self.path == "/api/fetch-job":
                payload = read_json(self)
                json_response(self, fetch_job_url(payload.get("url", "")))
            elif self.path == "/api/analyze":
                form = parse_multipart(self)
                job_text = form.getfirst("job_text", "").strip()
                resume_field = form["resume"] if "resume" in form else None
                if not job_text:
                    raise ValueError("Fetch or paste a job description first.")
                if resume_field is None or not resume_field.file:
                    raise ValueError("Upload a resume file first.")
                resume_data = resume_field.file.read()
                resume_text = extract_resume_text(resume_field.filename, resume_data)
                if len(resume_text.strip()) < 80:
                    raise ValueError("I could not extract enough resume text. Try a .txt or .docx version.")
                analysis = analyze_match(job_text, resume_text)
                analysis["resume_text"] = resume_text[:24000]
                json_response(self, analysis)
            elif self.path == "/api/tailor":
                payload = read_json(self)
                job_text = payload.get("job_text", "").strip()
                resume_text = payload.get("resume_text", "").strip()
                if not job_text or not resume_text:
                    raise ValueError("Analyze a resume against a job before tailoring.")
                json_response(self, tailor_resume(job_text, resume_text))
            elif self.path == "/api/submit":
                json_response(self, application_plan(read_json(self)))
            else:
                self.send_error(404)
        except (ValueError, urllib.error.URLError, TimeoutError) as exc:
            json_response(self, {"error": str(exc)}, 400)
        except Exception as exc:
            print(f"Unhandled error: {exc}", file=sys.stderr)
            json_response(self, {"error": "Something went wrong while the agent was working."}, 500)

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args))


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    server = ThreadingHTTPServer(("127.0.0.1", port), AgentHandler)
    print(f"Job Application Agent running at http://127.0.0.1:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
