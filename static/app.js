(function () {
  const twoCol = document.querySelector(".two-col[data-position-id]");
  if (!twoCol) return;
  const positionId = twoCol.dataset.positionId;

  const search = document.getElementById("candidate-search");
  const searchForm = document.getElementById("search-form");
  if (search && searchForm) {
    let debounceHandle = null;
    search.addEventListener("input", () => {
      clearTimeout(debounceHandle);
      debounceHandle = setTimeout(() => searchForm.submit(), 400);
    });
  }

  const addResumesForm = document.getElementById("add-resumes-form");
  const addResumesFiles = document.getElementById("add-resumes-files");
  const addResumesFolder = document.getElementById("add-resumes-folder");
  const addResumesCount = document.getElementById("add-resumes-count");
  if (addResumesForm && addResumesFiles && addResumesFolder) {
    function updateAddResumesCount() {
      const n = (addResumesFiles.files.length || 0) + (addResumesFolder.files.length || 0);
      addResumesCount.textContent = n ? `${n} resume file(s) selected` : "";
    }
    addResumesFiles.addEventListener("change", updateAddResumesCount);
    addResumesFolder.addEventListener("change", updateAddResumesCount);

    addResumesForm.addEventListener("submit", (e) => {
      const total = (addResumesFiles.files.length || 0) + (addResumesFolder.files.length || 0);
      if (!total) {
        e.preventDefault();
        alert("Please select at least one resume file or folder.");
        return;
      }
      if (addResumesFolder.files.length) {
        const dt = new DataTransfer();
        for (const f of addResumesFiles.files) dt.items.add(f);
        for (const f of addResumesFolder.files) dt.items.add(f);
        addResumesFiles.files = dt.files;
      }
    });
  }

  const selectAllCheckbox = document.getElementById("select-all-checkbox");
  const scoreBtn = document.getElementById("score-selected-btn");
  const emailBtn = document.getElementById("email-selected-btn");
  const deleteBtn = document.getElementById("delete-selected-btn");
  const progressWrap = document.getElementById("score-progress");
  const progressFill = document.getElementById("score-progress-fill");
  const progressText = document.getElementById("score-progress-text");
  let pollHandle = null;

  function selectedCandidateIds() {
    return Array.from(document.querySelectorAll(".candidate-select:checked")).map(
      (cb) => cb.dataset.candidateId
    );
  }

  function updateSelectionButtons() {
    const count = selectedCandidateIds().length;
    if (scoreBtn) scoreBtn.disabled = count === 0;
    if (emailBtn) emailBtn.disabled = count === 0;
    if (deleteBtn) deleteBtn.disabled = count === 0;
  }

  if (emailBtn) {
    emailBtn.addEventListener("click", () => {
      const candidateIds = selectedCandidateIds();
      if (!candidateIds.length) return;
      window.location.href = `/positions/${positionId}/send-jd-email?candidates=${candidateIds.join(",")}`;
    });
  }

  document.querySelectorAll(".candidate-select").forEach((cb) => {
    cb.addEventListener("change", () => {
      updateSelectionButtons();
      if (selectAllCheckbox && !cb.checked) selectAllCheckbox.checked = false;
    });
  });

  if (selectAllCheckbox) {
    selectAllCheckbox.addEventListener("change", () => {
      document.querySelectorAll(".candidate-select").forEach((cb) => {
        cb.checked = selectAllCheckbox.checked;
      });
      updateSelectionButtons();
    });
  }

  if (deleteBtn) {
    deleteBtn.addEventListener("click", () => {
      const candidateIds = selectedCandidateIds();
      if (!candidateIds.length) return;
      if (!confirm(`Delete ${candidateIds.length} selected resume(s)? This cannot be undone.`)) return;
      deleteBtn.disabled = true;
      fetch(`/positions/${positionId}/candidates/delete`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ candidate_ids: candidateIds }),
      })
        .then((r) => r.json())
        .then((data) => {
          if (data.error) {
            alert(data.error);
            deleteBtn.disabled = false;
            return;
          }
          window.location.href = `/positions/${positionId}/resumes`;
        });
    });
  }

  function applyBucket(row, bucket, score) {
    row.className = row.className.replace(/bucket-\S+/, "bucket-" + bucket);
    row.dataset.score = score != null ? String(score) : "";
    const scoreEl = row.querySelector(".candidate-score");
    if (!scoreEl) return;
    if (score != null) {
      // a scored resume: clickable badge that opens the score breakdown popup
      scoreEl.textContent = score.toFixed(1);
      scoreEl.className = "candidate-score candidate-score-btn";
      scoreEl.setAttribute("role", "button");
      scoreEl.tabIndex = 0;
      scoreEl.dataset.scoreBreakdown = row.dataset.candidateId;
      scoreEl.title = "Click for score breakdown";
    } else {
      scoreEl.textContent = "Not scored yet";
      scoreEl.className = "candidate-score candidate-score-none";
      scoreEl.removeAttribute("role");
      scoreEl.removeAttribute("tabindex");
      delete scoreEl.dataset.scoreBreakdown;
      scoreEl.title = "This resume has not been scored yet";
    }
  }

  // Default order of the list: highest score first, resumes with no score yet last, ties by the
  // most recently added CV and then by name - the same order the server renders on page load.
  function resortCandidateList() {
    const list = document.getElementById("candidate-list");
    if (!list) return;
    const rows = Array.from(list.querySelectorAll(".candidate-row"));
    const scoreOf = (r) => (r.dataset.score === "" || r.dataset.score == null ? null : parseFloat(r.dataset.score));
    const nameOf = (r) => (r.querySelector(".candidate-name") ? r.querySelector(".candidate-name").textContent.trim().toLowerCase() : "");
    rows.sort((a, b) => {
      const sa = scoreOf(a);
      const sb = scoreOf(b);
      if ((sa === null) !== (sb === null)) return sa === null ? 1 : -1;
      if (sa !== null && sa !== sb) return sb - sa;
      const da = a.dataset.added || "";
      const db = b.dataset.added || "";
      if (da !== db) return da < db ? 1 : -1;
      return nameOf(a).localeCompare(nameOf(b));
    });
    rows.forEach((r) => list.appendChild(r));
  }

  function refreshCandidateColors(resort) {
    fetch(`/positions/${positionId}/candidates.json`)
      .then((r) => r.json())
      .then((list) => {
        list.forEach((c) => {
          const row = document.querySelector(`.candidate-row[data-candidate-id="${c.id}"]`);
          if (row) applyBucket(row, c.bucket, c.score);
        });
        if (resort) resortCandidateList();
      });
  }

  function renderScoreError(message) {
    if (message.includes("Settings")) {
      progressText.innerHTML = `Error: ${message.replace("Settings", '<a href="/settings">Settings</a>')}`;
    } else {
      progressText.textContent = "Error: " + message;
    }
  }

  function pollProgress() {
    fetch(`/positions/${positionId}/score-progress.json`)
      .then((r) => r.json())
      .then((p) => {
        const total = p.total || 0;
        const done = p.done || 0;
        const pct = total ? Math.round((done / total) * 100) : 0;
        progressFill.style.width = pct + "%";
        progressText.textContent = total ? `Scoring ${done} / ${total}...` : "Starting...";
        refreshCandidateColors();

        if (p.error) {
          renderScoreError(p.error);
          clearInterval(pollHandle);
          updateSelectionButtons();
        } else if (total > 0 && done >= total) {
          progressText.textContent = `Scored ${total} candidate(s).`;
          clearInterval(pollHandle);
          updateSelectionButtons();
          refreshCandidateColors(true); // re-order highest score first now that scoring is done
        }
      });
  }

  if (scoreBtn) {
    scoreBtn.addEventListener("click", () => {
      const candidateIds = selectedCandidateIds();
      if (!candidateIds.length) return;
      scoreBtn.disabled = true;
      progressWrap.hidden = false;
      progressFill.style.width = "0%";
      progressText.textContent = "Starting...";
      fetch(`/positions/${positionId}/score-selected`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ candidate_ids: candidateIds }),
      })
        .then((r) => r.json())
        .then((data) => {
          if (data.error) {
            renderScoreError(data.error);
            updateSelectionButtons();
            return;
          }
          pollHandle = setInterval(pollProgress, 1500);
        });
    });
  }

  const detailModal = document.getElementById("detail-modal");
  const detailContent = document.getElementById("detail-content");
  const detailClose = document.getElementById("detail-close");
  const detailBtn = document.getElementById("detail-btn");

  function bucketLabel(bucket) {
    return { green: "Best Fit", yellow: "Needs Review", red: "Not a Fit", unscored: "Not Scored Yet" }[bucket] || bucket;
  }

  function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str || "";
    return div.innerHTML;
  }

  function renderDetail(d, candidateId) {
    const positives = (d.positives || []).map((p) => `<li>${p}</li>`).join("") || "<li>None noted.</li>";
    const negatives = (d.negatives || []).map((n) => `<li>${n}</li>`).join("") || "<li>None noted.</li>";
    const scoreText = d.score != null ? d.score.toFixed(1) : "--";
    detailContent.innerHTML = `
      <h2>${d.candidate_name || "Candidate"}</h2>
      <div class="detail-score-row">
        <div class="detail-score bucket-${d.bucket}" ${d.score != null ? `data-score-breakdown="${candidateId}" title="Click for score breakdown"` : ""}>${scoreText}<span>/10</span></div>
        <div class="detail-bucket-label bucket-label-${d.bucket}">${bucketLabel(d.bucket)}</div>
      </div>
      <p class="detail-summary">${d.fit_summary || "This candidate has not been scored yet. Select it and click Score Selected to run the assessment."}</p>
      <div class="detail-columns">
        <div>
          <h3>Strengths</h3>
          <ul class="detail-list detail-positives">${positives}</ul>
        </div>
        <div>
          <h3>Concerns</h3>
          <ul class="detail-list detail-negatives">${negatives}</ul>
        </div>
      </div>
      <div class="detail-review">
        <h3>Human Review</h3>
        <textarea id="human-review-input" rows="3" placeholder="Add a human take on this candidate - agree or disagree with the AI score, interview notes, anything worth flagging.">${escapeHtml(d.human_review)}</textarea>
        <div class="detail-review-actions">
          <button type="button" class="btn btn-secondary btn-small" id="human-review-save">Save Review</button>
          <span id="human-review-status" class="field-hint-small"></span>
        </div>
      </div>
    `;

    const saveBtn = document.getElementById("human-review-save");
    const status = document.getElementById("human-review-status");
    const textarea = document.getElementById("human-review-input");
    saveBtn.addEventListener("click", () => {
      saveBtn.disabled = true;
      status.textContent = "Saving...";
      fetch(`/candidates/${candidateId}/human-review`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ review: textarea.value }),
      })
        .then((r) => r.json())
        .then(() => { status.textContent = "Saved."; saveBtn.disabled = false; })
        .catch(() => { status.textContent = "Couldn't save - try again."; saveBtn.disabled = false; });
    });
  }

  if (detailBtn) {
    detailBtn.addEventListener("click", () => {
      const candidateId = detailBtn.dataset.candidateId;
      fetch(`/candidates/${candidateId}/detail.json`)
        .then((r) => r.json())
        .then((d) => {
          renderDetail(d, candidateId);
          detailModal.hidden = false;
        });
    });
  }
  if (detailClose) {
    detailClose.addEventListener("click", () => {
      detailModal.hidden = true;
    });
  }
  if (detailModal) {
    detailModal.addEventListener("click", (e) => {
      if (e.target === detailModal) detailModal.hidden = true;
    });
  }

  document.querySelectorAll(".onboarding-checkbox").forEach((box) => {
    box.addEventListener("change", () => {
      const li = box.closest(".onboarding-item");
      box.disabled = true;
      fetch(`/onboarding-items/${box.dataset.itemId}/toggle`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ completed: box.checked }),
      })
        .then((r) => r.json())
        .then(() => {
          box.disabled = false;
          li.classList.toggle("onboarding-item-done", box.checked);
        })
        .catch(() => {
          box.disabled = false;
          box.checked = !box.checked;
        });
    });
  });
})();
