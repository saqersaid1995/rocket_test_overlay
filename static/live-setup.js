(() => {
  "use strict";
  const typeToggle = document.querySelector("#typeToggle");
  const form = document.querySelector("#setupForm");
  const submitButton = document.querySelector("#submitButton");
  const errorBox = document.querySelector("#formError");
  let missionType = "static_fire";

  function setType(type) {
    missionType = type;
    typeToggle.querySelectorAll("button").forEach(btn => {
      btn.classList.toggle("active", btn.dataset.type === type);
    });
    document.querySelectorAll(".mission-type-block").forEach(block => {
      block.classList.toggle("active", block.dataset.type === type);
    });
  }

  typeToggle.addEventListener("click", event => {
    const button = event.target.closest("button[data-type]");
    if (button) setType(button.dataset.type);
  });

  function showError(message) {
    errorBox.textContent = message;
    errorBox.classList.add("visible");
  }

  function clearError() {
    errorBox.textContent = "";
    errorBox.classList.remove("visible");
  }

  function fieldValue(name) {
    const field = form.elements.namedItem(name);
    return field ? field.value : "";
  }

  function numberOrUndefined(name) {
    const raw = fieldValue(name);
    if (raw === "" || raw === null) return undefined;
    const value = Number(raw);
    return Number.isFinite(value) ? value : undefined;
  }

  form.addEventListener("submit", async event => {
    event.preventDefault();
    clearError();

    const t0Local = fieldValue("t0");
    if (!t0Local) {
      showError("Target T-0 time is required.");
      return;
    }
    const t0Epoch = new Date(t0Local).getTime() / 1000;
    if (!Number.isFinite(t0Epoch)) {
      showError("Target T-0 time is invalid.");
      return;
    }
    const holdPointS = numberOrUndefined("hold_point_s") ?? 120;

    const payload = {
      mission_name: fieldValue("mission_name").trim(),
      mission_type: missionType,
      run_number: fieldValue("run_number"),
      site: fieldValue("site"),
      team: fieldValue("team"),
      organization_name: fieldValue("organization_name") || "STELLAR KINETICS",
      accent: fieldValue("accent") || "#F59E0B",
      camera_label: fieldValue("camera_label") || "CAM 1",
      t0_epoch: t0Epoch,
      hold_points_s: [-Math.abs(holdPointS)],
      max_wind_speed_mps: numberOrUndefined("max_wind_speed_mps") ?? 12,
      temp_min_c: numberOrUndefined("temp_min_c") ?? -10,
      temp_max_c: numberOrUndefined("temp_max_c") ?? 45,
    };

    if (missionType === "static_fire") {
      payload.pressure_unit = fieldValue("pressure_unit") || "bar";
      payload.thrust_unit = fieldValue("thrust_unit") || "N";
      payload.static_fire = {
        engine_type: fieldValue("engine_type"),
        oxidizer: fieldValue("oxidizer"),
        fuel: fieldValue("fuel"),
        ablative_material: fieldValue("ablative_material"),
        expected_burn_duration_s: numberOrUndefined("expected_burn_duration_s") ?? 10,
        pressure_limit: numberOrUndefined("pressure_limit") ?? null,
      };
    } else {
      payload.launch = {
        vehicle_type: fieldValue("vehicle_type"),
        stage_count: numberOrUndefined("stage_count") ?? 1,
        target_altitude_km: numberOrUndefined("target_altitude_km") ?? 0,
        target_orbit: fieldValue("target_orbit"),
        evacuation_radius_m: numberOrUndefined("evacuation_radius_m") ?? 0,
        launch_window_s: numberOrUndefined("launch_window_s") ?? 1800,
        ascent_duration_s: numberOrUndefined("ascent_duration_s") ?? 480,
        max_velocity_mps: numberOrUndefined("max_velocity_mps") ?? 8000,
      };
    }

    submitButton.disabled = true;
    submitButton.textContent = "Creating…";
    try {
      const response = await fetch("/api/live/sessions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.error || "Could not create the session.");
      window.location.href = `/live/${body.session.id}/control-room`;
    } catch (error) {
      showError(error.message);
      submitButton.disabled = false;
      submitButton.textContent = "Create session →";
    }
  });

  setType("static_fire");
})();
