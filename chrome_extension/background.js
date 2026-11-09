const base = "http://127.0.0.1:8766";
const watched = {urls: ["https://www.tiktok.com/*"], types: ["main_frame", "xmlhttprequest"]};
const requestJobs = new Map();
let operations = Promise.resolve();

function enqueue(work) {
  const operation = operations.then(work);
  operations = operation.catch(() => {});
  return operation;
}

async function localRequest(path, data) {
  const config = await (await fetch(chrome.runtime.getURL("local_config.json"))).json();
  const response = await fetch(base + path, {
    method: data ? "POST" : "GET", headers: {
      "Authorization": "Bearer " + config.token, "Content-Type": "application/json",
      "X-Chengxian-Protocol": "5",
      "X-Chengxian-Version": chrome.runtime.getManifest().version
    }, body: data ? JSON.stringify(data) : undefined,
    signal: AbortSignal.timeout(path === "/progress" ? 4000 : 25000)
  });
  if (response.status === 409) return {stale: true};
  if (!response.ok) throw new Error("Local collector unavailable");
  return response.json();
}

async function taskState() {
  return (await chrome.storage.session.get("task")).task;
}

function matches(url, job) {
  if (!url) return false;
  const actual = new URL(url);
  const target = new URL(job.url);
  return actual.origin === target.origin && actual.pathname === target.pathname &&
    (job.kind !== "search" || actual.searchParams.get("q") === target.searchParams.get("q"));
}

async function progress(task, phase, videoTrace) {
  try { await localRequest("/progress", {jobId: task.job.id, phase, videoTrace}); } catch {}
}

function reportCommentNetwork(binding, details, event) {
  void binding?.then(request => {
    if (!request?.commentKind) return;
    return localRequest("/progress", {
      jobId: request.jobId, phase: "navigation_started",
      commentNetwork: {
        id: details.requestId, kind: request.commentKind, event,
        status: details.statusCode, error: details.error,
      },
    }).catch(() => {});
  });
}

async function submit(task) {
  try {
    const response = await localRequest("/result", task.pending);
    if (!response.accepted && !response.stale) throw new Error("Result not acknowledged");
    await chrome.storage.session.remove("task");
    setTimeout(() => enqueue(poll).catch(() => {}), 1500);
    return true;
  } catch {
    await progress(task, "delivery_failed");
    return false;
  }
}

async function finish(task, result) {
  if (task.pending) return false;
  task.pending = {...result, jobId: task.job.id};
  await chrome.storage.session.set({task});
  if (["http_error", "blocked"].includes(result.status)) {
    try { await chrome.tabs.update(task.tabId, {url: "about:blank"}); } catch {}
  }
  await progress(task, "result_ready");
  return submit(task);
}

async function dispatch(task, tab) {
  if (task.pending || tab?.status !== "complete" || !matches(tab.url, task.job)) return;
  if (!task.loaded) {
    task.loaded = true;
    await chrome.storage.session.set({task});
    await progress(task, "page_loaded");
  }
  if (task.started) return;
  let answer;
  try { answer = await chrome.tabs.sendMessage(task.tabId, {type: "collect", job: task.job}); }
  catch { return; }
  if (answer?.accepted) {
    task.started = true;
    await chrome.storage.session.set({task});
    await progress(task, "collector_started");
  }
}

async function collectorTab(slot) {
  const saved = await chrome.storage.session.get(["collectorWindowId", slot]);
  let collectorWindow;
  if (saved.collectorWindowId != null) {
    try { collectorWindow = await chrome.windows.get(saved.collectorWindowId); } catch {}
  }
  if (!collectorWindow) {
    collectorWindow = await chrome.windows.create({
      url: "about:blank", type: "normal", focused: false, width: 960, height: 720,
    });
    if (!collectorWindow?.id) {
      throw new Error("Collector window could not be created");
    }
    const [tab] = collectorWindow.tabs || await chrome.tabs.query({windowId: collectorWindow.id});
    if (!tab?.id) throw new Error("Collector tab could not be created");
    await chrome.storage.session.set({collectorWindowId: collectorWindow.id, [slot]: tab.id});
    return tab;
  }
  if (saved[slot] != null) {
    try {
      const tab = await chrome.tabs.get(saved[slot]);
      if (tab.windowId === collectorWindow.id) return tab;
    } catch {}
  }
  return chrome.tabs.create({windowId: collectorWindow.id, url: "about:blank", active: true});
}

async function poll() {
  try {
    let task = await taskState();
    if (task?.pending && !await submit(task)) return;
    const state = await localRequest("/job");
    if (!state.job) {
      await chrome.storage.session.remove("task");
      await chrome.action.setBadgeText({text: state.upgradeRequired ? "UPD" : ""});
      return;
    }
    task = await taskState();
    if (task?.job.id === state.job.id) {
      try { await dispatch(task, await chrome.tabs.get(task.tabId)); } catch {}
      return;
    }
    const slot = ["video", "metadata"].includes(state.job.kind) ? "videoTabId" : "discoveryTabId";
    const tab = await collectorTab(slot);
    task = {job: state.job, tabId: tab.id, dispatchedAt: Date.now()};
    await chrome.storage.session.set({task, [slot]: tab.id});
    await progress(task, "navigation_started");
    await chrome.tabs.update(tab.id, {autoDiscardable: false, active: true, muted: true});
    const samePage = matches(tab.url, state.job);
    if (samePage && state.job.resumeDepth > 0) {
      await dispatch(task, tab);
    } else if (samePage) {
      await chrome.tabs.reload(tab.id);
    } else {
      await chrome.tabs.update(tab.id, {url: state.job.url});
    }
    await chrome.action.setBadgeText({text: "RUN"});
  } catch {
    await chrome.action.setBadgeText({text: ""});
  }
}

chrome.tabs.onUpdated.addListener((tabId, change, tab) => enqueue(async () => {
  const task = await taskState();
  if (!task || task.tabId !== tabId || task.pending) return;
  const url = change.url ? new URL(change.url) : null;
  if (url?.origin === "https://www.tiktok.com" && /^\/(login|captcha|verify)(\/|$)/.test(url.pathname)) {
    await finish(task, {status: "blocked"});
  } else if (change.status === "complete") {
    await dispatch(task, tab);
  }
}).catch(() => {}));

function dataRequest(details) {
  const path = new URL(details.url).pathname;
  return details.type === "xmlhttprequest" && details.frameId === 0 &&
    details.initiator === "https://www.tiktok.com" && /^\/api\/(search|comment|post|item|creator)\//.test(path);
}

function matchesDataRequest(url, job) {
  const request = new URL(url);
  if (job.kind === "search") return request.pathname.startsWith("/api/search/");
  if (job.kind === "profile") return /^\/api\/(post|item|creator)\//.test(request.pathname);
  if (!/^\/api\/(comment|item|post)\//.test(request.pathname)) return false;
  const requested = request.searchParams.get("aweme_id") || request.searchParams.get("item_id");
  return !requested || new URL(job.url).pathname.endsWith("/" + requested);
}

chrome.webRequest.onBeforeRequest.addListener(details => {
  if (details.tabId < 0 || requestJobs.has(details.requestId)) return;
  if (details.type !== "main_frame" && !dataRequest(details)) return;
  const binding = taskState().then(task => {
    if (!task || task.tabId !== details.tabId || details.timeStamp < task.dispatchedAt) return null;
    if (details.type === "main_frame" && !matches(details.url, task.job)) return null;
    const path = new URL(details.url).pathname;
    const commentKind = task.job.kind === "video" && path.startsWith("/api/comment/") &&
      matchesDataRequest(details.url, task.job) ? (path.includes("/reply/") ? "reply" : "list") : null;
    return {jobId: task.job.id, commentKind};
  }).catch(() => null);
  requestJobs.set(details.requestId, binding);
  reportCommentNetwork(binding, details, "requested");
}, watched);

chrome.webRequest.onHeadersReceived.addListener(details => {
  const binding = requestJobs.get(details.requestId);
  reportCommentNetwork(binding, details, "response");
  if (![401, 403, 429].includes(details.statusCode)) return;
  enqueue(async () => {
    const task = await taskState();
    if (!task || task.pending || task.tabId !== details.tabId) return;
    const jobId = (await binding)?.jobId;
    const matchingNavigation = !binding && details.type === "main_frame" &&
      details.timeStamp >= task.dispatchedAt && matches(details.url, task.job);
    if (jobId !== task.job.id && !matchingNavigation) return;
    if (details.type !== "main_frame" && details.statusCode === 403 &&
        !matchesDataRequest(details.url, task.job)) return;
    const retryAfter = details.responseHeaders?.find(header => header.name.toLowerCase() === "retry-after")?.value;
    await finish(task, {status: "http_error", httpStatus: details.statusCode,
      scope: details.type === "main_frame" ? "navigation" : "data_request", retryAfter});
  }).catch(() => {});
}, watched, ["responseHeaders"]);

chrome.webRequest.onCompleted.addListener(details => {
  reportCommentNetwork(requestJobs.get(details.requestId), details, "completed");
  requestJobs.delete(details.requestId);
}, watched);
chrome.webRequest.onErrorOccurred.addListener(details => {
  const binding = requestJobs.get(details.requestId);
  requestJobs.delete(details.requestId);
  reportCommentNetwork(binding, details, "failed");
  if (details.type !== "main_frame" || details.error === "net::ERR_ABORTED") return;
  enqueue(async () => {
    const task = await taskState();
    if (task?.tabId === details.tabId && (await binding)?.jobId === task.job.id && !task.pending) {
      await finish(task, {status: "navigation_error"});
    }
  }).catch(() => {});
}, watched);

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === "page_pace") {
    taskState().then(task => {
      if (!task || task.pending || task.job.id !== message.jobId ||
          task.tabId !== sender.tab?.id || sender.frameId !== 0 || !matches(sender.url, task.job) ||
          ![500, 900, 1400, 2500].includes(message.milliseconds)) {
        sendResponse({ready: false});
        return;
      }
      // Pace existing page actions outside the hidden TikTok document.
      setTimeout(() => sendResponse({ready: true}), message.milliseconds);
      if (task.job.kind === "video" && message.videoTrace?.step % 5 === 0) {
        void progress(task, "collector_started", message.videoTrace);
      }
    }).catch(() => sendResponse({ready: false}));
    return true;
  }
  if (message.type === "comment_checkpoint") {
    enqueue(async () => {
      const task = await taskState();
      if (!task || task.pending || task.job.kind !== "video" || task.job.id !== message.jobId ||
          task.tabId !== sender.tab?.id || sender.frameId !== 0 || !matches(sender.url, task.job)) {
        return {stale: true};
      }
      return localRequest("/comments", message);
    }).then(sendResponse, () => sendResponse({accepted: false}));
    return true;
  }
  if (message.type !== "result") return;
  enqueue(async () => {
    const task = await taskState();
    if (!task || task.tabId !== sender.tab?.id || sender.frameId !== 0 ||
        message.jobId !== task.job.id) return false;
    if (!matches(sender.url, task.job) || message.diagnostics?.urlMatches === false) {
      // The right tab and task can report navigation failure, but no data from the wrong page.
      return finish(task, {status: message.status === "blocked" ? "blocked" : "error", comments: [], videos: [],
        diagnostics: {protocol: 5, stopReason: "navigation_changed"}});
    }
    return finish(task, message);
  }).then(accepted => sendResponse({accepted}), () => sendResponse({accepted: false}));
  return true;
});

function triggerPoll() { enqueue(poll).catch(() => {}); }
chrome.action.onClicked.addListener(triggerPoll);
chrome.alarms.onAlarm.addListener(triggerPoll);
async function start() {
  await chrome.alarms.create("collector", {periodInMinutes: 0.5});
  triggerPoll();
}
chrome.runtime.onInstalled.addListener(start);
chrome.runtime.onStartup.addListener(start);
