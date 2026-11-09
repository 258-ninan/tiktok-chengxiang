(() => {
  let working = false;
  let captured = {videos: [], diagnostics: {}};
  let commentPages = [];
  let commentDiagnostics = {};
  let depth = 0;
  let lastJob = null;
  const commentControl = '[data-e2e="comment-icon"],[data-key-interaction="action_comment"]';
  const commentItems = '[data-e2e="comment-level-1"],[data-e2e="comment-item"]';
  const commentPanel = '[class*="DivCommentListContainer"],[data-e2e="comment-input"]';
  const clickable = 'button,[role="button"],[role="tab"],[tabindex="0"]';
  const commentLabel = /^(?:评论|留言|comments?|التعليقات|comentarios|komentar)$/i;
  const bubbleIcon = 'svg[viewBox="0 0 48 48"] > path[d^="M2 21.5c0-10.22 9.88-18 22-18s22 7.78 22 18"]';
  window.addEventListener("message", event => {
    if (event.source !== window) return;
    if (event.data?.type === "CHENGXIAN_COMMENT_PAGES") {
      commentPages = Array.isArray(event.data.commentPages) ? event.data.commentPages : [];
      commentDiagnostics = event.data.diagnostics || {};
      return;
    }
    if (event.data?.type !== "CHENGXIAN_PAGE_DATA") return;
    captured = {
      videos: Array.isArray(event.data.videos) ? event.data.videos.slice(0, 1000) : [],
      diagnostics: event.data.diagnostics || {}
    };
  });
  function blocked() {
    const text = document.body?.innerText || "";
    const challenge = document.querySelector("iframe[src*='captcha'],#captcha_container");
    return location.pathname.startsWith("/login") ||
      (challenge && challenge.getClientRects().length > 0) ||
      /access denied|verify to continue|verify you are human|请完成验证|请验证您是真人/.test(text.toLowerCase());
  }
  function scroller(from) {
    let node = from;
    while (node && node !== document.body) {
      if (node.clientHeight > 100 && node.scrollHeight > node.clientHeight + 30 && /auto|scroll/.test(getComputedStyle(node).overflowY)) return node;
      node = node.parentElement;
    }
    return null;
  }
  function scrollComments(panel) {
    const node = scroller(panel.querySelector(commentItems) || panel);
    if (!node) return false;
    node.scrollTop += Math.max(300, Math.round(node.clientHeight * 0.75));
    return true;
  }
  function discoveryScroller() {
    const anchors = [...document.querySelectorAll('a[href*="/video/"]')];
    const visible = anchors.find(node => node.getClientRects().length > 0);
    return scroller(visible) || document.scrollingElement;
  }
  function rendered(node) {
    return node?.getClientRects().length > 0 && getComputedStyle(node).visibility === "visible" &&
      !node.closest('[aria-hidden="true"],[inert]');
  }
  function commentTab() {
    const labels = document.querySelectorAll('[role="tab"],[data-testid="tux-web-text"]');
    let first = null;
    for (const label of labels) {
      if (!commentLabel.test(label.textContent.trim()) || !rendered(label)) continue;
      const tab = label.closest(clickable);
      if (!tab || tab.matches(':disabled,[aria-disabled="true"]')) continue;
      if (tab.getAttribute("aria-selected") === "true") return tab;
      first ??= tab;
    }
    return first;
  }
  function visibleCommentPanel(tab = commentTab()) {
    const evidence = [...document.querySelectorAll(commentPanel)].find(rendered);
    if (evidence) return evidence.closest('[class*="RightPanelContainer"]') || evidence;
    const item = [...document.querySelectorAll(commentItems)].find(rendered);
    const itemPanel = item?.closest('[class*="RightPanelContainer"],[role="tabpanel"]');
    if (itemPanel) return itemPanel;
    const panelId = tab?.getAttribute("aria-controls");
    if (panelId && tab.getAttribute("aria-selected") === "true") {
      const panel = document.getElementById(panelId);
      if (rendered(panel)) return panel;
    }
    return null;
  }
  function commentControls(tab) {
    const controls = new Map();
    function add(node, source) {
      if (rendered(node) && !node.matches(':disabled,[aria-disabled="true"]') && !controls.has(node)) {
        controls.set(node, source);
      }
    }
    const direct = [...document.querySelectorAll(commentControl)].find(rendered);
    add(direct?.querySelector('button:not([disabled])') || direct, "structure");
    add(tab, "text_tab");
    const labelled = [...document.querySelectorAll('button[aria-label],[role="button"][aria-label]')].find(node =>
      rendered(node) &&
      /read or add comments|阅读或添加评论|^评论$|^comments?$|عرض التعليقات|^التعليقات$|^comentarios$/i
        .test(node.getAttribute("aria-label").trim()));
    add(labelled?.querySelector('button:not([disabled])') || labelled, "aria_label");
    for (const path of document.querySelectorAll(bubbleIcon)) {
      const svg = path.parentElement;
      if (!rendered(svg)) continue;
      const target = svg.closest(clickable);
      if (target && !target.matches(':disabled,[aria-disabled="true"]')) {
        add(target, "svg_icon");
        break;
      }
    }
    add(direct, "structure_wrapper");
    return controls;
  }
  function expandReply(panel, clicks, acknowledged) {
    const controls = '[data-key-interaction="comment_view_replies"],button,[role="button"]';
    for (const node of panel.querySelectorAll(controls)) {
      const label = (node.getAttribute("aria-label") || node.textContent || "").trim();
      if (!rendered(node) || /^(?:hide|collapse|隐藏|收起|إخفاء|ocultar)/i.test(label)) continue;
      if (node.getAttribute("data-key-interaction") !== "comment_view_replies" &&
          !/^(?:view|show|load).*repl|^(?:查看|展开|显示|加载).*回复|^عرض.*رد|^(?:ver|mostrar|cargar).*respuestas/i.test(label)) continue;
      const button = node.querySelector('button') || node.closest('button,[role="button"]') || node;
      const previous = clicks.get(button);
      if (previous?.label === label && previous.acknowledged === acknowledged) continue;
      clicks.set(button, {label, acknowledged});
      button.click();
      return true;
    }
    return false;
  }
  function matches(job) {
    const target = new URL(job.url);
    return location.origin === target.origin && location.pathname === target.pathname &&
      (job.kind !== "search" || new URL(location.href).searchParams.get("q") === target.searchParams.get("q"));
  }
  function read(commentsOnly = false) {
    window.postMessage({type: "CHENGXIAN_READ_PAGE", commentsOnly}, location.origin);
  }
  async function pace(job, milliseconds, videoTrace) {
    const response = await chrome.runtime.sendMessage({
      type: "page_pace", jobId: job.id, milliseconds,
      videoTrace,
    });
    if (!response?.ready) throw new Error("page_pace_unavailable");
  }
  async function collectComments(job) {
    const clicks = new WeakMap();
    let number = 0;
    let acknowledged = 0;
    let commentsSeen = 0;
    let idle = 0;
    let clickCount = 0;
    let lastClick = -4;
    let panelState = "control_missing";
    let controlSource = null;
    let entrySeen = false;
    let panelSeen = false;
    const attempted = new WeakSet();
    let videoTrace;
    for (let step = 0; idle < 16; step++) {
      if (blocked()) throw new Error("blocked");
      if (!matches(job)) throw new Error("navigation_changed");
      const tab = commentTab();
      const panel = visibleCommentPanel(tab);
      if (panel) {
        panelSeen = true;
        panelState = "panel_open";
      } else {
        const controls = commentControls(tab);
        entrySeen ||= controls.size > 0;
        const selected = [...controls.keys()].some(node =>
          node.matches('[aria-expanded="true"],[aria-selected="true"]'));
        const next = [...controls].find(([node]) => !attempted.has(node));
        panelState = controls.size ? "awaiting_panel" : "control_missing";
        if (next && !selected && clickCount < 4 && step - lastClick >= 4) {
          const [target, source] = next;
          attempted.add(target);
          target.click();
          clickCount++;
          lastClick = step;
          controlSource = source;
          panelState = source === "structure_wrapper" ? "wrapper_clicked" : "button_clicked";
        }
      }
      read(true);
      videoTrace = {
        step, panelOpened: Boolean(panel), panelState, clickCount, controlSource,
        entrySeen, panelSeen,
        renderedTopLevel: panel?.querySelectorAll(commentItems).length || 0,
        visibility: document.visibilityState, commentsSeen,
        commentResponses: commentDiagnostics.commentResponses || 0,
        commentPagesParsed: commentDiagnostics.commentPagesParsed || 0,
        commentJsonFailures: commentDiagnostics.commentJsonFailures || 0,
      };
      await pace(job, 1400, videoTrace);
      let grew = false;
      let complete = false;
      for (const {pageNumber, comments, ...metadata} of commentPages) {
        if (pageNumber <= acknowledged) continue;
        for (let offset = 0; offset < Math.max(1, comments.length); offset += 200) {
          const batch = comments.slice(offset, offset + 200);
          const response = await chrome.runtime.sendMessage({
            type: "comment_checkpoint", jobId: job.id, number: ++number,
            comments: batch, ...metadata, final: offset + 200 >= comments.length,
          });
          if (!response?.accepted) throw new Error("comment_checkpoint_unavailable");
          commentsSeen += batch.length;
          complete = response.complete;
        }
        acknowledged = pageNumber;
        grew = true;
      }
      if (grew) window.postMessage({type: "CHENGXIAN_ACK_COMMENTS", pageNumber: acknowledged}, location.origin);
      if (complete) return {reason: "comment_pagination_complete", videoTrace};
      idle = grew ? 0 : idle + 1;
      const currentPanel = visibleCommentPanel();
      panelSeen ||= Boolean(currentPanel);
      if (currentPanel && !expandReply(currentPanel, clicks, acknowledged)) scrollComments(currentPanel);
    }
    return {reason: panelState === "panel_open" ? "comment_pagination_stalled" : "comment_panel_not_open",
      videoTrace: {...videoTrace, panelSeen}};
  }
  async function run(job) {
    if (working || !matches(job) || job.id === lastJob) return;
    working = true;
    const beginning = Date.now();
    const initialCount = captured.videos.length;
    let moved = 0;
    let reason = "target_depth";
    let videoTrace;
    const resumeMode = depth >= (job.resumeDepth || 0) && depth > 0 ? "same_document" : "replay_from_top";
    try {
      if (job.kind !== "video") await pace(job, 2500);
      if (job.kind === 'metadata') {
        for (let index=0; index<4; index++) {
          if (blocked()) throw new Error('blocked');
          if (!matches(job)) throw new Error('navigation_changed');
          read();
          await pace(job, 900);
        }
        reason='metadata_only';
      } else if (job.kind === "video") {
        ({reason, videoTrace} = await collectComments(job));
      } else {
        let idle = 0;
        const targetDepth = job.targetDepth || job.scrolls;
        while (depth < targetDepth) {
          if (blocked()) throw new Error("blocked");
          if (!matches(job)) throw new Error("navigation_changed");
          read();
          const oldCount = captured.videos.length;
          const node = discoveryScroller();
          const top = node?.scrollTop || 0;
          node?.scrollBy(0, Math.max(850, (node.clientHeight || innerHeight) * 0.85));
          await pace(job, 1400);
          read();
          await pace(job, 500);
          const changed = node && node.scrollTop > top + 10;
          if (changed) { depth++; moved++; }
          if (!changed && captured.videos.length <= oldCount) idle++;
          else idle = 0;
          if (idle >= 5) { reason = "no_scroll_or_result_growth"; break; }
          if (Date.now() - beginning > 95000) { reason = "page_time_budget"; break; }
        }
      }
      if (job.kind !== "video") {
        read();
        await pace(job, 500);
      }
      const commentButtons = [...document.querySelectorAll('button,[role="button"]')]
        .map(node => node.getAttribute("aria-label") || node.innerText || "")
        .filter(value => /comment|评论|رد/i.test(value)).map(value => value.slice(0, 90)).slice(0, 12);
      const result = {
        type: "result", jobId: job.id, status: "observed", videoTrace, ...captured,
        diagnostics: {...(job.kind === "video" ? commentDiagnostics : captured.diagnostics),
          protocol: 5, commentButtons, urlMatches: matches(job),
          reachedDepth: depth, scrollMoved: moved, stopReason: reason,
          newObservedVideos: Math.max(0, captured.videos.length - initialCount), resumeMode,
          elapsedMs: Date.now() - beginning, visibility: document.visibilityState}
      };
      lastJob = job.id;
      await chrome.runtime.sendMessage(result);
    } catch (error) {
      await chrome.runtime.sendMessage({type: "result", jobId: job.id,
        status: error.message === "blocked" ? "blocked" : "error", videos: [], comments: [],
        diagnostics: {protocol: 5, reachedDepth: depth, stopReason: error.message || "page_error"}});
    } finally { working = false; }
  }
  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (message.type === "collect" && !working && message.job.id !== lastJob && matches(message.job)) {
      run(message.job);
      sendResponse({accepted: true});
    }
  });
})();
