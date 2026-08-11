import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import test from "node:test";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const projectRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const appSource = readFileSync(resolve(projectRoot, "static/app.js"), "utf8");
const queueStart = appSource.indexOf("function invalidateBroadcastRasterWork()");
const queueEnd = appSource.indexOf("\nfunction renderBroadcastPreview", queueStart);

assert.notEqual(queueStart, -1, "Raster queue start was not found in static/app.js");
assert.notEqual(queueEnd, -1, "Raster queue end was not found in static/app.js");

// Execute the production queue functions themselves in a deterministic browser
// harness. This catches regressions where frequent video frames starve a slow
// request, or an old render becomes visible after seeking/theme changes.
const productionQueueSource = appSource.slice(queueStart, queueEnd);

class FakeClock {
  constructor() {
    this.now = 1_000;
    this.nextId = 1;
    this.tasks = new Map();
  }

  setTimeout = (callback, delay = 0) => {
    const id = this.nextId++;
    this.tasks.set(id, {
      callback,
      due: this.now + Math.max(0, Number(delay) || 0),
    });
    return id;
  };

  clearTimeout = id => this.tasks.delete(id);

  runNext() {
    assert.ok(this.tasks.size, "Expected a queued raster timer");
    const [id, task] = [...this.tasks.entries()]
      .sort((left, right) => left[1].due - right[1].due || left[0] - right[0])[0];
    this.tasks.delete(id);
    this.now = task.due;
    task.callback();
  }
}

class FakeClassList {
  constructor() {
    this.values = new Set();
  }

  add(...names) {
    names.forEach(name => this.values.add(name));
  }

  remove(...names) {
    names.forEach(name => this.values.delete(name));
  }

  contains(name) {
    return this.values.has(name);
  }

  toggle(name, force) {
    const enabled = force === undefined ? !this.values.has(name) : Boolean(force);
    if (enabled) this.values.add(name);
    else this.values.delete(name);
    return enabled;
  }
}

class FetchHarness {
  constructor() {
    this.requests = [];
  }

  fetch = (url, options) => {
    const request = {
      url,
      options,
      payload: JSON.parse(options.body),
      aborted: false,
    };
    request.promise = new Promise((resolvePromise, rejectPromise) => {
      request.resolvePromise = resolvePromise;
      request.rejectPromise = rejectPromise;
    });
    options.signal.addEventListener("abort", () => {
      request.aborted = true;
      const error = new Error("aborted");
      error.name = "AbortError";
      request.rejectPromise(error);
    }, { once: true });
    this.requests.push(request);
    return request.promise;
  };

  resolve(index) {
    const request = this.requests[index];
    assert.ok(request, `Missing fetch request ${index}`);
    const time = request.payload.time;
    request.resolvePromise({
      ok: true,
      headers: {
        get(name) {
          const values = {
            "X-Telemetry-Time": String(time),
            "X-Telemetry-Pressure": String(Math.max(0, time * 10)),
            "X-Telemetry-Has-Thrust": "false",
            "X-Telemetry-Thrust": "0",
            "X-Telemetry-Peak-Pressure": "67.5",
            "X-Telemetry-Peak-Thrust": "",
          };
          return values[name] ?? null;
        },
      },
      blob: async () => ({ time, theme: request.payload.broadcast_theme }),
    });
  }
}

function field(value = "") {
  return { value };
}

function makeHarness({ paused = false, theme = "launch" } = {}) {
  const clock = new FakeClock();
  const network = new FetchHarness();
  const images = [];
  const revokedUrls = [];
  let objectUrlIndex = 0;
  const video = {
    paused,
    currentTime: 0,
    videoWidth: 1920,
    videoHeight: 1080,
  };
  const overlay = { classList: new FakeClassList() };
  const raster = {
    src: "",
    dataset: {},
    removeAttribute(name) {
      if (name === "src") this.src = "";
    },
  };
  const form = {
    elements: {
      ignition_video_s: field("0"),
      title: field("RNX TEST"),
      subtitle: field("STATIC FIRE TEST"),
      time_column: field("Time(s)"),
      pressure_column: field("Pressure(bar)"),
      thrust_column: field("__none__"),
      pressure_unit: field("bar"),
      thrust_unit: field("N"),
      run_number: field("RUN-1"),
      motor_type: field("L819"),
      propellant: field("LOX / PROPANE"),
      oxidizer: field("LOX"),
      fuel: field("PROPANE"),
      ablative_material: field("PHENOLIC ABLATIVE"),
      test_date: field("2026-08-09"),
      organization_name: field("STELLAR KINETICS"),
      test_site: field("DUQM"),
      coordinates_text: field("19N 57E"),
      footer_tagline: field("ENGINEERING THE VOID"),
      camera_label: field("CAM 01"),
      capture_fps: field("120 FPS"),
      pressure_limit: field("70"),
      telemetry_zero_s: field("81.977"),
      time_scale: field("1"),
    },
  };
  let selectedTheme = theme;

  class FakeImage {
    set src(value) {
      this.value = value;
      images.push(this);
    }
  }

  const context = vm.createContext({
    AbortController,
    Image: FakeImage,
    URL: {
      createObjectURL(blob) {
        objectUrlIndex += 1;
        return `blob:frame-${blob.time}-${blob.theme}-${objectUrlIndex}`;
      },
      revokeObjectURL(url) {
        revokedUrls.push(url);
      },
    },
    broadcastOverlay: overlay,
    broadcastRasterOverlay: raster,
    document: {
      querySelector(selector) {
        if (selector === "#broadcastAccent") return { value: "#38BDF8" };
        if (selector === "#showBroadcastChart") return { checked: true };
        if (selector === "#showBroadcastPhases") return { checked: true };
        return null;
      },
    },
    fetch: network.fetch,
    form,
    notify() {},
    performance: { now: () => clock.now },
    renderTelemetryDiagnostics() {},
    selectedTemplatePayload: () => ({}),
    selectedBroadcastTheme: () => selectedTheme,
    video,
    window: {
      clearTimeout: clock.clearTimeout,
      setTimeout: clock.setTimeout,
    },
  });

  vm.runInContext(`
    let broadcastPreviewSessionId = "preview-a";
    let broadcastRasterUrl = null;
    let broadcastRasterTimer = null;
    let broadcastRasterRequest = null;
    let broadcastRasterSequence = 0;
    let broadcastRasterGeneration = 0;
    let broadcastRasterPendingTime = null;
    let broadcastRasterLastStartedAt = 0;
    let broadcastRasterDisplayedSequence = 0;
    let runtimeTelemetry = null;
    let telemetryDiagnosticsData = null;
    let lastRasterError = "";

    ${productionQueueSource}

    globalThis.queueApi = {
      schedule: scheduleBroadcastRasterPreview,
      invalidate: invalidateBroadcastRasterWork,
      setTheme(value) { globalThis.__setSelectedTheme(value); },
      state() {
        return {
          generation: broadcastRasterGeneration,
          pendingTime: broadcastRasterPendingTime,
          requestActive: Boolean(broadcastRasterRequest),
          sequence: broadcastRasterSequence,
          displayedSequence: broadcastRasterDisplayedSequence,
          rasterUrl: broadcastRasterUrl,
        };
      },
    };
  `, context, { filename: "preview-raster-queue-production.js" });

  context.__setSelectedTheme = value => { selectedTheme = value; };
  return {
    api: context.queueApi,
    clock,
    images,
    network,
    overlay,
    raster,
    revokedUrls,
    video,
  };
}

async function settleAsyncWork() {
  // fetch continuation, response.blob(), Image setup, and async finally.
  for (let index = 0; index < 8; index += 1) await Promise.resolve();
}

test("continuous playback keeps the in-flight render and coalesces to the latest frame", async () => {
  const harness = makeHarness({ paused: false });
  harness.api.schedule(0);
  harness.clock.runNext();
  assert.equal(harness.network.requests.length, 1);

  harness.api.schedule(0.4);
  harness.api.schedule(0.8);
  harness.api.schedule(1.2);
  assert.equal(harness.network.requests.length, 1);
  assert.equal(harness.network.requests[0].aborted, false, "video frames must not cancel useful work");
  assert.equal(harness.api.state().pendingTime, 1.2);

  harness.network.resolve(0);
  await settleAsyncWork();
  assert.equal(harness.images.length, 1);

  harness.clock.runNext();
  assert.equal(harness.network.requests.length, 2);
  assert.equal(harness.network.requests[1].payload.time, 1.2);
  assert.deepEqual(
    [harness.network.requests[1].payload.width, harness.network.requests[1].payload.height],
    [960, 540],
  );

  // Decoding can finish out of order even though HTTP rendering is single-flight.
  harness.network.resolve(1);
  await settleAsyncWork();
  assert.equal(harness.images.length, 2);
  harness.images[1].onload();
  assert.match(harness.raster.src, /frame-1\.2-launch/);
  harness.images[0].onload();
  assert.match(harness.raster.src, /frame-1\.2-launch/, "a late decoder must not rewind the overlay");
});

test("backward seek rejects a decoded frame from the old generation", async () => {
  const harness = makeHarness({ paused: true });
  harness.api.schedule(5);
  harness.clock.runNext();
  harness.network.resolve(0);
  await settleAsyncWork();
  assert.equal(harness.images.length, 1);

  harness.api.invalidate();
  harness.api.schedule(1);
  harness.clock.runNext();
  harness.images[0].onload();
  assert.equal(harness.raster.src, "", "the forward frame must not flash after seeking backward");
  assert.ok(harness.revokedUrls.some(url => url.includes("frame-5-launch")));

  harness.network.resolve(1);
  await settleAsyncWork();
  harness.images[1].onload();
  assert.match(harness.raster.src, /frame-1-launch/);
});

test("theme change invalidates the old theme and only displays the replacement", async () => {
  const harness = makeHarness({ paused: true, theme: "launch" });
  harness.api.schedule(2);
  harness.clock.runNext();
  harness.network.resolve(0);
  await settleAsyncWork();

  harness.api.setTheme("mission_control");
  harness.api.invalidate();
  harness.api.schedule(2);
  harness.clock.runNext();
  assert.equal(harness.network.requests[1].payload.broadcast_theme, "mission_control");

  harness.images[0].onload();
  assert.equal(harness.raster.src, "");
  harness.network.resolve(1);
  await settleAsyncWork();
  harness.images[1].onload();
  assert.match(harness.raster.src, /frame-2-mission_control/);
});

test("pausing cancels a playing render and refreshes the exact paused frame at inspection size", async () => {
  const harness = makeHarness({ paused: false });
  harness.api.schedule(3);
  harness.clock.runNext();
  assert.equal(harness.network.requests[0].payload.width, 960);

  harness.video.paused = true;
  harness.api.invalidate();
  assert.equal(harness.network.requests[0].aborted, true);
  harness.api.schedule(3.25);
  harness.clock.runNext();
  assert.equal(harness.network.requests.length, 2);
  assert.deepEqual(
    [harness.network.requests[1].payload.width, harness.network.requests[1].payload.height],
    [1280, 720],
  );

  harness.network.resolve(1);
  await settleAsyncWork();
  harness.images[0].onload();
  assert.match(harness.raster.src, /frame-3\.25-launch/);
});
