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
      // Review v1 issue 4: defer the initial Batch preview render
      // until the Batch tab is actually activated. The default tab is
      // Single, so firing the preview unconditionally on page load
      // would waste one round-trip (and one logo upload, if a logo is
      // attached) for users who never visit the Batch tab.
      if (key === "batch") {
        maybeFireInitialBatchPreview();
      }
    });
  });

  // ---- Template gallery (Design sections) --------------------------
  //
  // On DOMContentLoaded (this IIFE runs at the end of the body, so the
  // DOM is already ready by the time we get here) we fetch the
  // /api/qr/templates listing once. The result is grouped by category,
  // each category populates the dropdown on both forms, and switching
  // categories lazily renders the matching tiles. Selecting a tile sets
  // the form's hidden template_id input and (on the Single QR form
  // only) re-triggers a submit so the live preview refreshes.
  //
  // Categories are sorted to a stable order: "default" first, then the
  // sport categories in a deliberate order, then the remaining
  // categories alphabetically. This keeps the dropdown predictable
  // across reloads even if the registry ordering ever changes.
  var SPORT_ORDER = [
    "marathon",
    "running",
    "duathlon",
    "triathlon",
    "cycling",
    "swimming",
  ];

  // Box size used for the HD re-render triggered by the
  // "Download HD PNG" button under each live preview. The on-screen
  // preview keeps using whatever ``box_size`` the user typed (default
  // 10, fast to render); the download path forces this constant so
  // the saved file is high-resolution regardless.
  //
  // (a) At HD_BOX_SIZE = 40 a 33-module QR is 33 * 40 + 2 * 4 * 40 =
  //     1640 px per side at the default border = 4, well past the
  //     "looks crisp on a phone screen / printed at 300 DPI"
  //     threshold.
  // (b) We deliberately stop short of MAX_BOX_SIZE = 50 because beyond
  //     ~40 the marginal visual gain is invisible while the response
  //     payload roughly doubles. The existing MAX_BOX_SIZE remains the
  //     upper bound users can request manually via the box_size form
  //     field; this constant only governs the one-click HD download.
  var HD_BOX_SIZE = 40;

  function categoryRank(cat) {
    if (cat === "default") return 0;
    var i = SPORT_ORDER.indexOf(cat);
    if (i !== -1) return 1 + i;
    return 100; // general categories sorted alphabetically below
  }

  function compareCategories(a, b) {
    var ra = categoryRank(a);
    var rb = categoryRank(b);
    if (ra !== rb) return ra - rb;
    return a < b ? -1 : a > b ? 1 : 0;
  }

  function setupDesignSection(formKey, onTemplateChange) {
    var select = document.getElementById(formKey + "-template-category");
    var grid = document.getElementById(formKey + "-template-grid");
    var hidden = document.getElementById(formKey + "-template-id");
    var logoInput = document.getElementById(formKey + "-logo");
    var clearBtn = document.querySelector(
      '.logo-clear-btn[data-form="' + formKey + '"]'
    );
    if (!select || !grid || !hidden) {
      return null;
    }

    var byCategory = {};
    var orderedCategories = [];

    function renderGrid(category) {
      grid.innerHTML = "";
      var entries = byCategory[category] || [];
      entries.forEach(function (entry) {
        var tile = document.createElement("div");
        tile.className = "template-tile";
        tile.setAttribute("data-template-id", entry.id);
        tile.setAttribute("title", entry.name);
        if (entry.id === hidden.value) {
          tile.classList.add("selected");
        }

        var img = document.createElement("img");
        img.alt = entry.name;
        img.loading = "lazy";
        img.src = "/api/qr/templates/" + encodeURIComponent(entry.id) + "/preview";
        tile.appendChild(img);

        var label = document.createElement("div");
        label.className = "template-tile-label";
        label.textContent = entry.name;
        tile.appendChild(label);

        tile.addEventListener("click", function () {
          if (hidden.value === entry.id) return;
          hidden.value = entry.id;
          var prev = grid.querySelector(".template-tile.selected");
          if (prev) prev.classList.remove("selected");
          tile.classList.add("selected");
          if (typeof onTemplateChange === "function") {
            onTemplateChange();
          }
        });

        grid.appendChild(tile);
      });
    }

    select.addEventListener("change", function () {
      renderGrid(select.value);
    });

    if (logoInput && typeof onTemplateChange === "function") {
      logoInput.addEventListener("change", function () {
        onTemplateChange();
      });
    }

    if (clearBtn && logoInput) {
      clearBtn.addEventListener("click", function () {
        if (!logoInput.value) return;
        logoInput.value = "";
        if (typeof onTemplateChange === "function") {
          onTemplateChange();
        }
      });
    }

    return {
      load: function (templates) {
        byCategory = {};
        templates.forEach(function (entry) {
          if (!byCategory[entry.category]) {
            byCategory[entry.category] = [];
          }
          byCategory[entry.category].push(entry);
        });
        orderedCategories = Object.keys(byCategory).sort(compareCategories);

        select.innerHTML = "";
        orderedCategories.forEach(function (cat) {
          var opt = document.createElement("option");
          opt.value = cat;
          opt.textContent = cat.charAt(0).toUpperCase() + cat.slice(1);
          select.appendChild(opt);
        });

        var initial = orderedCategories.indexOf("default") !== -1
          ? "default"
          : orderedCategories[0];
        select.value = initial;
        renderGrid(initial);
      },
    };
  }

  // The Single QR form has a live preview, so selecting a template (or
  // changing the logo) re-triggers the existing submit handler. The
  // Batch form mirrors this behaviour with a debounced live preview
  // (scheduleBatchPreview is hoisted from below).
  //
  // Review v1 issue 5: the Single live preview is intentionally not
  // debounced because it only fires on tile clicks and logo file
  // changes (discrete events), while the Batch live preview is
  // debounced at 250 ms because it additionally listens to numeric
  // and template inputs where users can hold an arrow key or paste,
  // which would otherwise produce a burst of fetches per second.
  var singleDesign = setupDesignSection("single", function () {
    var form = document.getElementById("single-form");
    if (!form) return;
    var dataInput = form.elements["data"];
    if (!dataInput || !dataInput.value) return;
    if (typeof form.requestSubmit === "function") {
      form.requestSubmit();
    } else {
      form.dispatchEvent(new Event("submit", { cancelable: true, bubbles: true }));
    }
  });
  // Review v1 issue 2: when the design section reports a change
  // (template tile click, logo file change, logo Clear) we are on the
  // slow path - the user explicitly interacted with the design, so the
  // preview should include the attached logo even though it costs an
  // upload. The fast path (numeric / template-text keystrokes, wired
  // further down via the batchForm 'input' listener) calls
  // scheduleBatchPreview() with no argument so the logo is omitted,
  // which avoids re-uploading up to 2 MB on every debounced keystroke.
  var batchDesign = setupDesignSection("batch", function () {
    scheduleBatchPreview({ includeLogo: true });
  });

  // Review v1 issue 3 + 4: track the templates fetch state and only
  // fire the initial Batch preview once the user has activated the
  // Batch tab AND the templates fetch has settled (success OR
  // failure). The HTML hardcodes ``template_id="default"`` so even
  // a templates outage still produces a valid preview.
  var batchTemplatesSettled = false;
  var batchInitialPreviewFired = false;

  function maybeFireInitialBatchPreview() {
    if (batchInitialPreviewFired) return;
    if (!batchTemplatesSettled) return;
    if (!document.getElementById("batch-form")) return;
    var batchPanel = panels.batch;
    if (!batchPanel || !batchPanel.classList.contains("active")) return;
    batchInitialPreviewFired = true;
    scheduleBatchPreview({ includeLogo: true });
  }

  fetch("/api/qr/templates")
    .then(function (response) {
      if (!response.ok) throw new Error("templates fetch failed");
      return response.json();
    })
    .then(function (body) {
      var templates = (body && body.templates) || [];
      if (singleDesign) singleDesign.load(templates);
      if (batchDesign) batchDesign.load(templates);
    })
    .catch(function () {
      // Templates unavailable (offline / server error). The Design
      // section remains empty but the form still submits with the
      // default template, so the page keeps working.
    })
    .finally(function () {
      // Review v1 issue 3: mark templates settled and try to fire the
      // initial Batch preview. We use ``finally`` rather than only
      // ``then`` so a templates outage still fires the initial render
      // (the HTML hardcodes ``template_id="default"``, which produces
      // a valid QR even without the registry data).
      batchTemplatesSettled = true;
      maybeFireInitialBatchPreview();
    });

  // ---- Shared helpers ---------------------------------------------
  //
  // ``triggerDownload`` is used by the Batch submit handler (for the
  // generated ZIP / PDF) and by the per-preview "Download PNG" buttons
  // attached beneath the live preview images on both tabs. It mints a
  // throwaway object URL, programmatically clicks an invisible <a>,
  // then revokes the URL on a short timer so the browser has time to
  // start the download before the URL is invalidated.
  function triggerDownload(blob, filename) {
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(function () {
      URL.revokeObjectURL(url);
    }, 1000);
  }

  // Append a "Download HD PNG" button to a preview pane. Both render
  // paths replace ``previewEl.innerHTML`` whenever they redraw or
  // reset to the empty state, so the button is naturally cleared
  // alongside the image when no QR is showing.
  //
  // ``hdRefetchOpts`` (optional) wires the HD re-render behaviour:
  // on click the button POSTs a fresh request to /api/qr/single with
  // ``box_size`` forced to HD_BOX_SIZE and downloads the resulting
  // higher-resolution PNG instead of the cached preview Blob. Two
  // shapes are accepted:
  //
  //   { form, fields, fileFields }
  //     Used by the Single QR submit handler. The button copies the
  //     listed text fields from ``form.elements`` and the listed file
  //     inputs (by name, reading ``.files[0]``) into a fresh FormData,
  //     overrides ``box_size`` with HD_BOX_SIZE, and POSTs.
  //
  //   { formData }
  //     Used by the Batch live preview, where the on-screen QR is
  //     rendered with substituted ``data`` / ``label`` values (the
  //     padded first range item, with ``{n}`` resolved). The HD
  //     download must encode the SAME substituted payload, not the
  //     raw ``{n}`` template, so the caller pre-builds a FormData
  //     that mirrors the preview request and passes it here. The
  //     button overrides ``box_size`` with HD_BOX_SIZE on click.
  //
  // If ``hdRefetchOpts`` is omitted the button reverts to today's
  // behaviour: just download the cached preview Blob.
  //
  // If the HD fetch fails (network, encoder error, abort from a
  // re-click), the button falls back to downloading the cached
  // preview Blob so the user still gets a file.
  function appendPreviewDownloadButton(previewEl, blob, filename, hdRefetchOpts) {
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "primary preview-download";
    btn.textContent = "Download HD PNG";
    btn.title =
      "Re-renders at high resolution (~1480 px per side at default border) before downloading.";

    // Per-button AbortController so a second click (or a redraw that
    // leaves the previous button orphaned) can cancel the in-flight
    // HD fetch instead of letting an outdated download arrive after
    // the user has moved on.
    var inflight = null;

    btn.addEventListener("click", function () {
      if (!hdRefetchOpts) {
        triggerDownload(blob, filename);
        return;
      }

      var hdFormData;
      if (hdRefetchOpts.formData) {
        // Batch live-preview path: caller pre-built the FormData with
        // substituted data/label.
        hdFormData = hdRefetchOpts.formData;
      } else if (hdRefetchOpts.form) {
        // Single QR submit path: copy named fields from the live form
        // so any edits the user made after the preview rendered are
        // reflected in the HD download.
        hdFormData = new FormData();
        var fields = hdRefetchOpts.fields || [];
        for (var i = 0; i < fields.length; i++) {
          var name = fields[i];
          var el = hdRefetchOpts.form.elements[name];
          if (el && el.value !== undefined && el.value !== null && el.value !== "") {
            hdFormData.set(name, el.value);
          }
        }
        var fileFields = hdRefetchOpts.fileFields || [];
        for (var j = 0; j < fileFields.length; j++) {
          var fname = fileFields[j];
          var fileEl = hdRefetchOpts.form.elements[fname];
          if (fileEl && fileEl.files && fileEl.files.length > 0) {
            hdFormData.set(fname, fileEl.files[0]);
          }
        }
      } else {
        triggerDownload(blob, filename);
        return;
      }
      hdFormData.set("box_size", String(HD_BOX_SIZE));

      // Cancel any earlier HD fetch from this same button before
      // starting a new one.
      if (inflight) {
        inflight.abort();
        inflight = null;
      }
      var controller = new AbortController();
      inflight = controller;

      var originalText = btn.textContent;
      btn.disabled = true;
      btn.textContent = "Generating HD...";

      fetch("/api/qr/single", {
        method: "POST",
        body: hdFormData,
        signal: controller.signal,
      })
        .then(function (response) {
          if (!response.ok) {
            throw new Error("HD render failed (" + response.status + ")");
          }
          return response.blob();
        })
        .then(function (hdBlob) {
          triggerDownload(hdBlob, filename);
        })
        .catch(function (err) {
          // AbortError fires when a second click or a redraw cancels
          // this fetch; in that case we should NOT trigger a fallback
          // download because the user explicitly moved on.
          if (err && err.name === "AbortError") {
            return;
          }
          // Any other failure (network, encoder error) falls back to
          // the cached preview Blob so the user still gets a file.
          triggerDownload(blob, filename);
        })
        .then(function () {
          // ``finally`` equivalent that also runs on AbortError. Only
          // restore the button if THIS fetch is still the latest one;
          // otherwise a newer click already updated the state and we
          // must not stomp on it.
          if (inflight === controller) {
            inflight = null;
            btn.disabled = false;
            btn.textContent = originalText;
          }
        });
    });
    previewEl.appendChild(btn);
  }

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
          // The form's ``data`` value is user-supplied and may contain
          // characters that are unsafe for filenames (slashes, NUL,
          // path traversal, etc.), so we use a fixed sensible default
          // rather than trying to sanitise it here.
          //
          // Pass HD-refetch opts so the button re-issues a fresh
          // POST at HD_BOX_SIZE = 40 before downloading. The fields
          // list deliberately omits ``box_size`` because the HD
          // download forces it to HD_BOX_SIZE.
          appendPreviewDownloadButton(singlePreview, blob, "qr.png", {
            form: singleForm,
            fields: ["data", "label", "border", "template_id"],
            fileFields: ["logo"],
          });
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
  var batchPreview = document.getElementById("batch-preview");
  var batchProgress = document.getElementById("batch-progress");
  var batchProgressText = batchProgress ? batchProgress.querySelector(".progress-text") : null;
  var batchProgressFill = batchProgress ? batchProgress.querySelector(".progress-bar-fill") : null;
  var batchProgressPercent = batchProgress ? batchProgress.querySelector(".progress-percent") : null;
  var countRow = document.getElementById("batch-count-row");
  var endRow = document.getElementById("batch-end-row");

  // ---- Batch live preview -----------------------------------------
  //
  // The Batch tab mirrors the Single tab's live preview: whenever a
  // field that affects the rendered QR changes (template tile, logo,
  // start, padding, data/label templates, box size, border) we re-fetch
  // a sample using /api/qr/single, substituting '{n}' in the data and
  // label templates with the zero-padded first number of the configured
  // range (matching what generate_sequence does on the server).
  //
  // Review v1 issue 5: the Single QR preview is intentionally not
  // debounced because it only fires on discrete events (template tile
  // clicks and logo file changes). The Batch preview additionally
  // listens to numeric and template-text inputs where users can hold
  // an arrow key or paste, so a 250 ms debounce keeps fast typing in
  // start/count/padding/data_template from firing dozens of requests.
  // The asymmetry is deliberate; harmonising both at 250 ms would
  // delay the Single preview's response to a single tile click for
  // no real benefit.
  //
  // Review v1 issue 2: when the user is on the keystroke fast path
  // (numeric / text inputs into the Batch form) we skip the logo on
  // the request. The Batch preview's headline value is showing what
  // each generated QR will look like for the substituted data and
  // template; including the (potentially 2 MB) logo on every
  // debounced keystroke is meaningful per-keystroke server cost. The
  // logo is included on the slow path: template tile click, logo file
  // change, and logo Clear button (the events that are explicitly
  // about the logo or the design). That means a user who types into a
  // text field with a logo attached will see the preview without the
  // logo until they next interact with the design section, which is a
  // reasonable trade-off for not paying a 2 MB upload per arrow-key
  // tick.
  var batchPreviewAbort = null;
  var batchPreviewBlobUrl = null;
  var batchPreviewTimer = null;
  var batchPreviewIncludeLogo = false;

  function scheduleBatchPreview(opts) {
    // ``opts.includeLogo`` is sticky across the debounce window: if
    // any caller in the window passes ``includeLogo: true`` (e.g. a
    // template tile click) the actual fetch will include the logo,
    // even if a later keystroke fires scheduleBatchPreview() with no
    // opts. Without this, a tile click followed quickly by a
    // keystroke would lose the logo on the resulting fetch.
    if (opts && opts.includeLogo) {
      batchPreviewIncludeLogo = true;
    }
    clearTimeout(batchPreviewTimer);
    batchPreviewTimer = setTimeout(refreshBatchPreview, 250);
  }

  function setBatchPreviewMessage(message) {
    if (!batchPreview) return;
    batchPreview.innerHTML = "";
    var p = document.createElement("p");
    p.className = "preview-empty";
    p.textContent = message;
    batchPreview.appendChild(p);
  }

  function refreshBatchPreview() {
    clearTimeout(batchPreviewTimer);
    batchPreviewTimer = null;
    // Snapshot then reset the sticky flag so the next debounce window
    // starts fresh (the next caller decides whether to include the
    // logo, just like the first caller of this window did).
    var includeLogo = batchPreviewIncludeLogo;
    batchPreviewIncludeLogo = false;
    if (!batchForm || !batchPreview) return;

    var startVal = parseInt(batchForm.elements["start"].value || "", 10);
    if (isNaN(startVal)) {
      setBatchPreviewMessage(
        "A sample QR using the first range value will appear here."
      );
      return;
    }
    var paddingVal = parseInt(batchForm.elements["padding"].value || "0", 10);
    if (isNaN(paddingVal) || paddingVal < 0) paddingVal = 0;
    var paddedFirst = paddingVal > 0 ? pad(startVal, paddingVal) : String(startVal);

    var dataTemplateEl = batchForm.elements["data_template"];
    var labelTemplateEl = batchForm.elements["label_template"];
    var dataTemplate = dataTemplateEl ? dataTemplateEl.value : "";
    var labelTemplate = labelTemplateEl ? labelTemplateEl.value : "";
    var dataValue = dataTemplate.split("{n}").join(paddedFirst);
    var labelValue = labelTemplate.split("{n}").join(paddedFirst);

    if (!dataValue) {
      setBatchPreviewMessage(
        "A sample QR using the first range value will appear here."
      );
      return;
    }

    var formData = new FormData();
    formData.set("data", dataValue);
    if (labelValue) {
      formData.set("label", labelValue);
    }
    var boxSizeEl = batchForm.elements["box_size"];
    if (boxSizeEl && boxSizeEl.value) {
      formData.set("box_size", boxSizeEl.value);
    }
    var borderEl = batchForm.elements["border"];
    if (borderEl && borderEl.value) {
      formData.set("border", borderEl.value);
    }
    var templateIdEl = batchForm.elements["template_id"];
    if (templateIdEl && templateIdEl.value) {
      formData.set("template_id", templateIdEl.value);
    }
    // FormData.set on a file input only copies the empty .value string,
    // so reach for .files[0] like the existing Batch submit handler.
    // Review v1 issue 2: the logo is only attached when ``includeLogo``
    // is set (slow path: template tile click, logo file change, logo
    // Clear). On the fast path (numeric/text-input keystrokes) the
    // logo is omitted to avoid re-uploading up to 2 MB per debounced
    // keystroke.
    if (includeLogo) {
      var batchLogoInput = document.getElementById("batch-logo");
      if (batchLogoInput && batchLogoInput.files && batchLogoInput.files.length > 0) {
        formData.set("logo", batchLogoInput.files[0]);
      }
    }

    if (batchPreviewAbort) {
      batchPreviewAbort.abort();
      batchPreviewAbort = null;
    }
    var controller = new AbortController();
    batchPreviewAbort = controller;

    fetch("/api/qr/single", {
      method: "POST",
      body: formData,
      signal: controller.signal,
    })
      .then(function (response) {
        if (!response.ok) {
          return response.json().then(
            function (body) {
              throw new Error(body.error || "Preview unavailable");
            },
            function () {
              throw new Error("Preview unavailable");
            }
          );
        }
        return response.blob();
      })
      .then(function (blob) {
        if (batchPreviewBlobUrl) {
          URL.revokeObjectURL(batchPreviewBlobUrl);
        }
        batchPreviewBlobUrl = URL.createObjectURL(blob);
        batchPreview.innerHTML = "";
        var img = document.createElement("img");
        img.alt = "Batch sample QR preview";
        img.src = batchPreviewBlobUrl;
        batchPreview.appendChild(img);
        // Build a fresh FormData for the HD download path that
        // mirrors the preview request's substituted values. We
        // cannot reuse ``form + fields`` like the Single path does
        // because ``data`` and ``label`` on the form carry the raw
        // {n} TEMPLATES; sending those untouched would encode a
        // different payload than the preview shows. The HD download
        // must encode the SAME substituted dataValue/labelValue the
        // preview just rendered.
        //
        // The logo is included only when the preview itself ran with
        // a logo (``includeLogo`` is the same flag that gated the
        // preview fetch). On the keystroke fast path the preview was
        // rendered without the logo, so the HD download mirrors that
        // and stays consistent with what the user sees.
        var hdFormData = new FormData();
        hdFormData.set("data", dataValue);
        if (labelValue) {
          hdFormData.set("label", labelValue);
        }
        if (borderEl && borderEl.value) {
          hdFormData.set("border", borderEl.value);
        }
        if (templateIdEl && templateIdEl.value) {
          hdFormData.set("template_id", templateIdEl.value);
        }
        if (includeLogo) {
          var hdLogoInput = document.getElementById("batch-logo");
          if (hdLogoInput && hdLogoInput.files && hdLogoInput.files.length > 0) {
            hdFormData.set("logo", hdLogoInput.files[0]);
          }
        }
        // ``paddedFirst`` is the zero-padded first range value (e.g.
        // "0101"), matching what generate_sequence would emit on the
        // server. It only contains digits, so it is filename-safe.
        appendPreviewDownloadButton(
          batchPreview,
          blob,
          "qr_" + paddedFirst + ".png",
          { formData: hdFormData }
        );
      })
      .catch(function (err) {
        if (err && err.name === "AbortError") {
          return;
        }
        setBatchPreviewMessage(err && err.message ? err.message : "Preview unavailable");
      });
  }

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
    batchForm.addEventListener("input", function () {
      updateHint();
      scheduleBatchPreview();
    });
    batchForm.addEventListener("change", function (event) {
      if (event.target && event.target.name === "mode") {
        updateModeVisibility();
      }
      scheduleBatchPreview();
    });
    updateModeVisibility();

    batchForm.addEventListener("submit", function (event) {
      event.preventDefault();
      batchError.hidden = true;
      batchError.textContent = "";

      var mode = getMode();
      // Build a clean FormData that contains only the active range field.
      var formData = new FormData();
      var pass = ["start", "padding", "prefix", "data_template", "label_template", "box_size", "border", "format", "template_id"];
      pass.forEach(function (name) {
        var el = batchForm.elements[name];
        if (el && el.value !== undefined && el.value !== null) {
          formData.set(name, el.value);
        }
      });
      // Include the logo file separately (FormData.set on a file input
      // copies only the .value string, which is empty / fake-pathed for
      // security reasons; .files[0] is the real File object).
      var batchLogoInput = document.getElementById("batch-logo");
      if (batchLogoInput && batchLogoInput.files && batchLogoInput.files.length > 0) {
        formData.set("logo", batchLogoInput.files[0]);
      }
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
        // Restore the bar container in case a previous submit ran the
        // non-streaming fallback path and hid it.
        var barRestore = batchProgress.querySelector(".progress-bar-container");
        if (barRestore) barRestore.removeAttribute("hidden");
        if (batchProgressText) {
          batchProgressText.textContent = "Generating " + codeCount + " QR code" + (codeCount === 1 ? "" : "s") + "...";
        }
        if (batchProgressFill) batchProgressFill.style.width = "0%";
        if (batchProgressPercent) batchProgressPercent.textContent = "0%";
      }

      // Disable the submit button during request
      var submitBtn = batchForm.querySelector('button[type="submit"]');
      if (submitBtn) submitBtn.disabled = true;

      function setPercent(pct) {
        if (batchProgressFill) batchProgressFill.style.width = pct + "%";
        if (batchProgressPercent) batchProgressPercent.textContent = pct + "%";
      }

      function base64ToBytes(b64) {
        var bin = atob(b64);
        var len = bin.length;
        var out = new Uint8Array(len);
        for (var i = 0; i < len; i++) {
          out[i] = bin.charCodeAt(i);
        }
        return out;
      }

      function finish() {
        if (batchProgress) batchProgress.setAttribute("hidden", "");
        if (submitBtn) submitBtn.disabled = false;
      }

      function showError(message) {
        batchError.textContent = message;
        batchError.hidden = false;
      }

      fetch("/api/qr/batch/stream", { method: "POST", body: formData })
        .then(function (response) {
          if (!response.ok) {
            // Validation failure: server returned a normal JSON 400.
            return response.json().then(
              function (body) {
                throw new Error(body.error || "Request failed");
              },
              function () {
                throw new Error("Request failed (" + response.status + ")");
              }
            );
          }

          if (!response.body || !response.body.getReader) {
            // Older browsers without streaming - fall back to text() then
            // parse linearly. We can't show a live percentage in this
            // mode, so hide the bar and show a generic "Generating..."
            // message instead of leaving the user staring at a stuck 0%.
            if (batchProgressFill) batchProgressFill.style.width = "0%";
            if (batchProgressPercent) batchProgressPercent.textContent = "";
            if (batchProgress) {
              // Keep the section visible only as a textual status, hide
              // the bar container itself.
              var bar = batchProgress.querySelector(".progress-bar-container");
              if (bar) bar.setAttribute("hidden", "");
            }
            if (batchProgressText) {
              batchProgressText.textContent = "Generating, please wait...";
            }
            return response.text().then(function (text) {
              return processStreamText(text);
            });
          }

          var reader = response.body.getReader();
          var decoder = new TextDecoder("utf-8");
          var leftover = "";
          var totalSeen = 0;

          function processLine(line) {
            if (!line) return;
            var evt;
            try {
              evt = JSON.parse(line);
            } catch (parseErr) {
              // Ignore malformed lines defensively; the real result event
              // will surface the failure.
              return;
            }
            if (evt.event === "start") {
              totalSeen = evt.total || 0;
              setPercent(0);
              if (batchProgressText) {
                batchProgressText.textContent =
                  "Generating " + totalSeen + " QR code" + (totalSeen === 1 ? "" : "s") + "...";
              }
            } else if (evt.event === "progress") {
              var total = evt.total || totalSeen || 1;
              var pct = Math.round(((evt.index + 1) / total) * 100);
              if (pct < 0) pct = 0;
              if (pct > 100) pct = 100;
              setPercent(pct);
            } else if (evt.event === "result") {
              setPercent(100);
              var bytes = base64ToBytes(evt.data_base64 || "");
              var blob = new Blob([bytes], {
                type: evt.mimetype || "application/octet-stream",
              });
              var filename = evt.filename || ("qr_batch." + (fmt === "pdf" ? "pdf" : "zip"));
              triggerDownload(blob, filename);
            } else if (evt.event === "error") {
              throw new Error(evt.error || "Generation failed");
            }
          }

          function processStreamText(text) {
            // Used by the non-streaming fallback path: parse the whole body at once.
            var lines = text.split("\n");
            for (var i = 0; i < lines.length; i++) {
              processLine(lines[i]);
            }
          }

          function pump() {
            return reader.read().then(function (result) {
              if (result.done) {
                // Flush any final non-newline-terminated line.
                if (leftover) {
                  processLine(leftover);
                  leftover = "";
                }
                return;
              }
              var chunk = decoder.decode(result.value, { stream: true });
              leftover += chunk;
              var nl = leftover.indexOf("\n");
              while (nl !== -1) {
                var line = leftover.slice(0, nl);
                leftover = leftover.slice(nl + 1);
                processLine(line);
                nl = leftover.indexOf("\n");
              }
              return pump();
            });
          }

          return pump();
        })
        .catch(function (err) {
          showError(err.message || String(err));
        })
        .finally(finish);
    });
  }
})();
