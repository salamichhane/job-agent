const state = {
  jobText: "",
  resumeText: "",
  analysis: null,
  activeTab: "matched",
};

const $ = (id) => document.getElementById(id);

function setStatus(message) {
  $("agentStatus").textContent = message;
}

function toast(message) {
  const el = $("toast");
  el.textContent = message;
  el.classList.add("show");
  window.setTimeout(() => el.classList.remove("show"), 3200);
}

function wordCount(text) {
  return (text.trim().match(/\S+/g) || []).length;
}

async function postJson(url, payload) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Request failed");
  return data;
}

function setBusy(button, busy, label) {
  button.disabled = busy;
  if (busy) {
    button.dataset.original = button.textContent;
    button.textContent = label;
  } else {
    button.textContent = button.dataset.original || button.textContent;
  }
}

function updateJobMeta(title) {
  state.jobText = $("jobText").value;
  $("jobTitle").textContent = title || "Job description ready";
  $("jobLength").textContent = `${wordCount(state.jobText)} words`;
}

function renderChips(items, variant = "") {
  if (!items || !items.length) return `<span class="empty">Nothing to show yet</span>`;
  return items.map((item) => `<span class="chip ${variant}">${item}</span>`).join("");
}

function renderAnalysisTab() {
  const cloud = $("keywordCloud");
  if (!state.analysis) {
    cloud.className = "keyword-cloud empty";
    cloud.textContent = "No analysis yet";
    return;
  }
  cloud.className = "keyword-cloud";
  if (state.activeTab === "missing") {
    cloud.innerHTML = renderChips(state.analysis.missing_keywords, "missing");
  } else if (state.activeTab === "skills") {
    cloud.innerHTML = renderChips(state.analysis.job_skills, "skill");
  } else {
    cloud.innerHTML = renderChips(state.analysis.matched_keywords);
  }
}

function updateScore(analysis) {
  $("scoreValue").textContent = analysis.score;
  const label = analysis.score >= 80 ? "Strong JD match" : analysis.score >= 60 ? "Solid base, worth tailoring" : "Needs targeted tailoring";
  $("scoreLabel").textContent = label;
  $("scoreDetail").textContent = `${analysis.sections.keyword_match}% keyword match and ${analysis.sections.skill_match}% skill match.`;
  $("scoreExplain").className = "score-explain";
  $("scoreExplain").innerHTML = `
    <strong>How this score was calculated:</strong><br>
    ${analysis.scoring_explanation.formula}.<br>
    Checked ${analysis.sections.job_keywords_checked} top JD keywords against the resume.
    Resume words extracted: ${analysis.sections.resume_words}.
    Length bonus: ${analysis.scoring_explanation.resume_length_bonus_applied ? "yes" : "no"}.
  `;
  renderAnalysisTab();
}

function updateResumeUploadState() {
  const file = $("resumeFile").files[0];
  if (!file) {
    $("resumeFileName").textContent = "Choose resume";
    $("resumeFileMeta").textContent = ".txt, .docx, or text-based .pdf";
    return;
  }
  const sizeKb = Math.max(1, Math.round(file.size / 1024));
  $("resumeFileName").textContent = file.name;
  $("resumeFileMeta").textContent = `${sizeKb} KB selected. Click Analyze to extract and score it.`;
}

async function fetchJob() {
  const button = $("fetchJobBtn");
  const url = $("jobUrl").value.trim();
  if (!url) return toast("Paste a job description URL first.");
  setBusy(button, true, "Fetching...");
  setStatus("Fetching JD");
  try {
    const data = await postJson("/api/fetch-job", { url });
    $("jobText").value = data.text;
    updateJobMeta(data.title);
    $("applicationUrl").value = data.source_url;
    toast("Job description fetched.");
  } catch (error) {
    toast(error.message);
  } finally {
    setBusy(button, false);
    setStatus("Ready");
  }
}

async function analyzeResume() {
  const button = $("analyzeBtn");
  const file = $("resumeFile").files[0];
  state.jobText = $("jobText").value.trim();
  if (!state.jobText) return toast("Fetch or paste a job description first.");
  if (!file) return toast("Upload a resume first.");

  const body = new FormData();
  body.append("job_text", state.jobText);
  body.append("resume", file);

  setBusy(button, true, "Analyzing...");
  setStatus("Scoring resume");
  try {
    const response = await fetch("/api/analyze", { method: "POST", body });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Analysis failed");
    state.analysis = data;
    state.resumeText = data.resume_text;
    $("resumePreviewText").value = state.resumeText;
    $("resumeWordCount").textContent = `${wordCount(state.resumeText)} resume words extracted`;
    updateScore(data);
    toast("ATS comparison complete.");
  } catch (error) {
    toast(error.message);
  } finally {
    setBusy(button, false);
    setStatus("Ready");
  }
}

async function tailorResume() {
  const button = $("tailorBtn");
  state.jobText = $("jobText").value.trim();
  if (!state.jobText || !state.resumeText) return toast("Run resume analysis before tailoring.");

  setBusy(button, true, "Tailoring...");
  setStatus("Tailoring resume");
  try {
    const data = await postJson("/api/tailor", {
      job_text: state.jobText,
      resume_text: state.resumeText,
    });
    $("summaryOut").value = data.professional_summary;
    $("skillsOut").className = "keyword-cloud";
    $("skillsOut").innerHTML = renderChips(data.recommended_skills, "skill");
    $("bulletsOut").className = "bullets";
    $("bulletsOut").innerHTML = data.resume_bullets.map((bullet) => `<div class="bullet">${bullet}</div>`).join("");
    $("coverOut").value = data.cover_note;
    $("scoreValue").textContent = data.target_score;
    $("scoreLabel").textContent = "Projected tailored score";
    $("scoreDetail").textContent = "Projection after adding the generated summary, skills, and bullets.";
    toast("Tailored resume draft generated.");
  } catch (error) {
    toast(error.message);
  } finally {
    setBusy(button, false);
    setStatus("Ready");
  }
}

async function prepareApply() {
  const button = $("submitBtn");
  setBusy(button, true, "Preparing...");
  setStatus("Preparing package");
  try {
    const data = await postJson("/api/submit", {
      application_url: $("applicationUrl").value.trim(),
      candidate: {
        name: $("candidateName").value.trim(),
        email: $("candidateEmail").value.trim(),
        phone: $("candidatePhone").value.trim(),
      },
    });
    const link = data.application_url
      ? `<a href="${data.application_url}" target="_blank" rel="noreferrer">Open application form</a>`
      : "";
    $("applyPlan").className = "apply-plan";
    $("applyPlan").innerHTML = [
      link,
      data.missing_candidate_fields.length
        ? `<div class="plan-step">Missing details: ${data.missing_candidate_fields.join(", ")}</div>`
        : `<div class="plan-step">Candidate details are ready for review.</div>`,
      ...data.steps.map((step) => `<div class="plan-step">${step}</div>`),
      `<div class="plan-step">${data.guardrail}</div>`,
    ].filter(Boolean).join("");
    toast("Application plan ready.");
  } catch (error) {
    toast(error.message);
  } finally {
    setBusy(button, false);
    setStatus("Ready");
  }
}

$("fetchJobBtn").addEventListener("click", fetchJob);
$("analyzeBtn").addEventListener("click", analyzeResume);
$("tailorBtn").addEventListener("click", tailorResume);
$("submitBtn").addEventListener("click", prepareApply);
$("jobText").addEventListener("input", () => updateJobMeta());
$("resumeFile").addEventListener("change", updateResumeUploadState);

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((item) => item.classList.remove("active"));
    tab.classList.add("active");
    state.activeTab = tab.dataset.tab;
    renderAnalysisTab();
  });
});

document.querySelectorAll("nav a").forEach((link) => {
  link.addEventListener("click", () => {
    document.querySelectorAll("nav a").forEach((item) => item.classList.remove("active"));
    link.classList.add("active");
  });
});
