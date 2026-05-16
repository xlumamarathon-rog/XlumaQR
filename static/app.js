(function () {
  "use strict";

  // Tab switching
  var tabs = document.querySelectorAll(".tab");
  var panels = {
    single: document.getElementById("panel-single"),
    batch: document.getElementById("panel-batch"),
  };

  tabs.forEach(function (tab) {
    tab.addEventListener("click", function () {
      var key = tab.getAttribute("data-tab");
      tabs.forEach(function (t) {
        var active = t === tab;
        t.classList.toggle("active", active);
        t.setAttribute("aria-selected", active ? "true" : "false");
      });
      Object.keys(panels).forEach(function (k) {
        var panel = panels[k];
        if (!panel) return;
        var visible = k === key;
        panel.classList.toggle("active", visible);
        if (visible) {
          panel.removeAttribute("hidden");
        } else {
          panel.setAttribute("hidden", "");
        }
      });
    });
  });

  // ---- Single QR ---------------------------------------------------
  var singleForm = document.getElementById("single-form");
  var singlePreview = document.getElementById("single-preview");
  var singleError = document.getElementById("single-error");
  var lastBlobUrl = null;

  if (singleForm) {
    singleForm.addEventListener("submit", function (event) {
      event.preventDefault();
      singleError.hidden = true;
      singleError.textContent = "";

      var formData = new FormData(singleForm);
      fetch("/api/qr/single", { method: "POST", body: formData })
        .then(function (response) {
          if (!response.ok) {
            return response.json().then(
              function (body) {
                throw new Error(body.error || "Request failed");
              },
              function () {
                throw new Error("Request failed (" + response.status + ")");
              }
            );
          }
          return response.blob();
        })
        .then(function (blob) {
          if (lastBlobUrl) {
            URL.revokeObjectURL(lastBlobUrl);
          }
          lastBlobUrl = URL.createObjectURL(blob);
          singlePreview.innerHTML = "";
          var img = document.createElement("img");
          img.alt = "Generated QR code";
          img.src = lastBlobUrl;
          singlePreview.appendChild(img);
        })
        .catch(function (err) {
          singleError.textContent = err.message;
          singleError.hidden = false;
        });
    });
  }

  // ---- Batch -------------------------------------------------------
  var batchForm = document.getElementById("batch-form");
  var batchHint = document.getElementById("batch-hint");
  var batchError = document.getElementById("batch-error");
  var batchProgress = document.getElementById("batch-progress");
  var batchProgressText = batchProgress ? batchProgress.querySelector(".progress-text") : null;
  var countRow = document.getElementById("batch-count-row");
  var endRow = document.getElementById("batch-end-row");

  // Advanced settings toggle
  var advancedToggle = document.getElementById("batch-advanced-toggle");
  var advancedSection = document.getElementById("batch-advanced");
  if (advancedToggle && advancedSection) {
    advancedToggle.addEventListener("click", function () {
      var isHidden = advancedSection.hasAttribute("hidden");
      var arrow = advancedToggle.querySelector(".advanced-arrow");
      if (isHidden) {
        advancedSection.removeAttribute("hidden");
        if (arrow) arrow.classList.add("open");
      } else {
        advancedSection.setAttribute("hidden", "");
        if (arrow) arrow.classList.remove("open");
      }
    });
  }

  function getMode() {
    var radios = batchForm.querySelectorAll('input[name="mode"]');
    for (var i = 0; i < radios.length; i++) {
      if (radios[i].checked) return radios[i].value;
    }
    return "count";
  }

  function pad(value, width) {
    var s = String(value);
    while (s.length < width) {
      s = "0" + s;
    }
    return s;
  }

  function updateModeVisibility() {
    var mode = getMode();
    if (mode === "count") {
      countRow.removeAttribute("hidden");
      endRow.setAttribute("hidden", "");
    } else {
      countRow.setAttribute("hidden", "");
      endRow.removeAttribute("hidden");
    }
    updateHint();
  }

  function updateHint() {
    if (!batchForm || !batchHint) return;
    var startVal = parseInt(
      batchForm.elements["start"].value || "",
      10
    );
    var paddingVal = parseInt(
      batchForm.elements["padding"].value || "0",
      10
    );
    if (isNaN(paddingVal) || paddingVal < 0) paddingVal = 0;

    var mode = getMode();
    var count;
    var lastN;

    if (isNaN(startVal)) {
      batchHint.textContent = "Enter a start number to see the range.";
      batchHint.classList.add("error");
      return;
    }

    if (mode === "count") {
      count = parseInt(batchForm.elements["count"].value || "", 10);
      if (isNaN(count) || count <= 0) {
        batchHint.textContent = "Count must be a positive integer.";
        batchHint.classList.add("error");
        return;
      }
      lastN = startVal + count - 1;
    } else {
      var endVal = parseInt(batchForm.elements["end"].value || "", 10);
      if (isNaN(endVal) || endVal < startVal) {
        batchHint.textContent = "End must be >= start.";
        batchHint.classList.add("error");
        return;
      }
      lastN = endVal;
      count = endVal - startVal + 1;
    }

    var firstStr = paddingVal > 0 ? pad(startVal, paddingVal) : String(startVal);
    var lastStr = paddingVal > 0 ? pad(lastN, paddingVal) : String(lastN);

    batchHint.classList.remove("error");
    batchHint.textContent =
      "Will generate " +
      count +
      " QR code" +
      (count === 1 ? "" : "s") +
      ": " +
      firstStr +
      " -> " +
      lastStr;
  }

  if (batchForm) {
    batchForm.addEventListener("input", updateHint);
    batchForm.addEventListener("change", function (event) {
      if (event.target && event.target.name === "mode") {
        updateModeVisibility();
      }
    });
    updateModeVisibility();

    batchForm.addEventListener("submit", function (event) {
      event.preventDefault();
      batchError.hidden = true;
      batchError.textContent = "";

      var mode = getMode();
      // Build a clean FormData that contains only the active range field.
      var formData = new FormData();
      var pass = ["start", "padding", "prefix", "data_template", "label_template", "box_size", "border", "format"];
      pass.forEach(function (name) {
        var el = batchForm.elements[name];
        if (el && el.value !== undefined && el.value !== null) {
          formData.set(name, el.value);
        }
      });
      if (mode === "count") {
        formData.set("count", batchForm.elements["count"].value);
      } else {
        formData.set("end", batchForm.elements["end"].value);
      }

      var formatEl = batchForm.querySelector('input[name="format"]:checked');
      var fmt = formatEl ? formatEl.value : "zip";

      // Show progress indicator
      var codeCount = 0;
      if (mode === "count") {
        codeCount = parseInt(batchForm.elements["count"].value || "0", 10);
      } else {
        var s = parseInt(batchForm.elements["start"].value || "0", 10);
        var e = parseInt(batchForm.elements["end"].value || "0", 10);
        codeCount = Math.max(0, e - s + 1);
      }
      if (batchProgress) {
        batchProgress.removeAttribute("hidden");
        if (batchProgressText) {
          batchProgressText.textContent = "Generating " + codeCount + " QR code" + (codeCount === 1 ? "" : "s") + "...";
        }
      }

      // Disable the submit button during request
      var submitBtn = batchForm.querySelector('button[type="submit"]');
      if (submitBtn) submitBtn.disabled = true;

      fetch("/api/qr/batch", { method: "POST", body: formData })
        .then(function (response) {
          if (!response.ok) {
            return response.json().then(
              function (body) {
                throw new Error(body.error || "Request failed");
              },
              function () {
                throw new Error("Request failed (" + response.status + ")");
              }
            );
          }
          var disposition = response.headers.get("Content-Disposition") || "";
          var match = /filename="?([^"]+)"?/.exec(disposition);
          var filename = match
            ? match[1]
            : "qr_batch." + (fmt === "pdf" ? "pdf" : "zip");
          return response.blob().then(function (blob) {
            return { blob: blob, filename: filename };
          });
        })
        .then(function (result) {
          var url = URL.createObjectURL(result.blob);
          var a = document.createElement("a");
          a.href = url;
          a.download = result.filename;
          document.body.appendChild(a);
          a.click();
          document.body.removeChild(a);
          setTimeout(function () {
            URL.revokeObjectURL(url);
          }, 1000);
        })
        .catch(function (err) {
          batchError.textContent = err.message;
          batchError.hidden = false;
        })
        .finally(function () {
          // Hide progress and re-enable button
          if (batchProgress) batchProgress.setAttribute("hidden", "");
          if (submitBtn) submitBtn.disabled = false;
        });
    });
  }
})();
