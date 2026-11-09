// Capture only research fields from responses already requested by the page.
(() => {
  const videos = new Map();
  let commentPages = [];
  let pageNumber = 0;
  const pageSignatures = new Set();
  const diagnostics = {commentResponses: 0, commentPagesParsed: 0, commentJsonFailures: 0,
    commentStatusCodes: [], searchResponses: 0};
  function numericId(value) {
    if (typeof value === "number" && Number.isSafeInteger(value) && value >= 0) return String(value);
    return typeof value === "string" && /^\d+$/.test(value) ? value : null;
  }
  function captureComments(url, payload) {
    const request = new URL(url, location.href);
    const currentId = /^\/@[^/]+\/video\/(\d+)$/.exec(location.pathname)?.[1];
    const requestedId = request.searchParams.get("aweme_id") || request.searchParams.get("item_id");
    if (!currentId || (requestedId && requestedId !== currentId)) return;
    const isReply = request.pathname.includes("/reply/");
    const parentId = isReply ? numericId(request.searchParams.get("comment_id")) : null;
    if (isReply ? !parentId : !requestedId) return;
    const page = payload?.data && !Array.isArray(payload.data) ? payload.data : payload;
    const status = payload?.status_code ?? page?.status_code;
    if ((status != null && ![0, "0"].includes(status)) || !Array.isArray(page?.comments)) return;
    const cursor = numericId(request.searchParams.get("cursor") ?? "0");
    const nextCursor = numericId(page.cursor);
    const hasMore = [0, "0", false].includes(page.has_more) ? false :
      [1, "1", true].includes(page.has_more) ? true : null;
    let valid = true;
    const comments = [];
    for (const item of page.comments) {
      const cid = numericId(item?.cid);
      const replyId = numericId(item?.reply_id);
      const suppliedVideo = numericId(item?.aweme_id);
      if (!cid || (item?.aweme_id != null && suppliedVideo !== currentId) ||
          (item?.reply_id != null && !replyId) || typeof item?.text !== "string") {
        valid = false;
        continue;
      }
      const author = item.user || {};
      const replyTotal = numericId(item.reply_comment_total ?? item.reply_count);
      comments.push({
        cid, text: item.text, create_time: item.create_time, aweme_id: currentId,
        reply_id: replyId && replyId !== "0" ? replyId : parentId,
        reply_total: replyTotal != null && Number.isSafeInteger(Number(replyTotal)) ? Number(replyTotal) : null,
        user: {unique_id: author.unique_id || author.uniqueId || null}
      });
    }
    const signature = JSON.stringify([parentId, cursor, nextCursor, hasMore, valid, comments.map(row => row.cid)]);
    if (pageSignatures.has(signature)) return;
    pageSignatures.add(signature);
    diagnostics.commentPagesParsed++;
    commentPages.push({pageNumber: ++pageNumber, parentId, cursor, nextCursor, hasMore, valid, comments});
  }
  function receive(url, payload) {
    if (url.includes("/api/comment/")) {
      diagnostics.commentResponses += 1;
      captureComments(url, payload);
      if (payload?.status_code != null && diagnostics.commentStatusCodes.length < 20) {
        diagnostics.commentStatusCodes.push(payload.status_code);
      }
    } else {
      diagnostics.searchResponses += 1;
      // Only numerical cursors/has-more indicators, never URLs, headers or login state.
      const page = payload?.data && !Array.isArray(payload.data) ? payload.data : payload;
      const cursor = page?.cursor;
      if (typeof cursor === 'number' || (typeof cursor === 'string' && /^\d+$/.test(cursor))) diagnostics.lastCursor = String(cursor);
      if (page?.has_more != null) diagnostics.lastHasMore = Boolean(page.has_more);
    }
    inspect(payload);
  }
  function inspect(value, depth = 0) {
    if (!value || typeof value !== "object" || depth > 14) return;
    if (value.id && typeof value.desc === "string" && value.author) {
      const author = typeof value.author === "object" ? value.author.uniqueId : value.author;
      if (typeof author === "string") {
        videos.set(String(value.id), {
          id: String(value.id), desc: value.desc, createTime: value.createTime,
          author: {uniqueId: author}, stats: {commentCount: (value.stats || value.statsV2 || {}).commentCount}
        });
      }
    }
    if (Array.isArray(value)) {
      value.slice(0, 1000).forEach(item => inspect(item, depth + 1));
    } else {
      Object.values(value).forEach(item => inspect(item, depth + 1));
    }
  }
  function relevant(url) {
    try {
      const parsed = new URL(url, location.href);
      if (parsed.protocol === "https:" && parsed.hostname.endsWith(".tiktok.com") &&
          parsed.pathname.startsWith("/api/comment/")) return true;
      return parsed.origin === location.origin &&
        /^\/api\/(search|comment|post|item|creator)\//.test(parsed.pathname);
    } catch { return false; }
  }
  const originalFetch = window.fetch;
  window.fetch = async function (...args) {
    const response = await originalFetch.apply(this, args);
    if (relevant(response.url)) {
      response.clone().json().then(
        data => receive(response.url, data),
        () => { if (response.url.includes("/api/comment/")) diagnostics.commentJsonFailures++; },
      ).catch(() => {});
    }
    return response;
  };
  const originalOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (...args) {
    if (relevant(args[1])) {
      this.addEventListener("load", () => {
        const url = String(args[1]);
        let payload;
        try {
          payload = this.responseType === "json" ? this.response : JSON.parse(this.responseText);
        } catch {
          if (url.includes("/api/comment/")) diagnostics.commentJsonFailures++;
          return;
        }
        receive(url, payload);
      }, {once: true});
    }
    return originalOpen.apply(this, args);
  };
  window.addEventListener("message", event => {
    if (event.source !== window) return;
    if (event.data?.type === "CHENGXIAN_ACK_COMMENTS") {
      commentPages = commentPages.filter(page => page.pageNumber > event.data.pageNumber);
      return;
    }
    if (event.data?.type !== "CHENGXIAN_READ_PAGE") return;
    if (event.data.commentsOnly) {
      window.postMessage({type: "CHENGXIAN_COMMENT_PAGES", commentPages, diagnostics}, location.origin);
      return;
    }
    for (const id of ["__UNIVERSAL_DATA_FOR_REHYDRATION__", "SIGI_STATE"]) {
      try { inspect(JSON.parse(document.getElementById(id)?.textContent || "null")); }
      catch { /* Script data is optional. */ }
    }
    // Profile grids can expose links without an item-list API payload. Keep those
    // as incomplete candidates; the collector must verify dates on detail pages.
    const anchors = [...document.querySelectorAll('a[href*="/video/"]')];
    diagnostics.profileVideoLinks = anchors.length;
    diagnostics.visibility = document.visibilityState;
    const text = document.body?.innerText || '';
    diagnostics.profileState = /couldn.t find this account|account not found|找不到此账号|找不到该账号/i.test(text) ? 'not_found' :
      /this account is private|此账号为私密|私密账号/i.test(text) ? 'private' : 'unconfirmed';
    for (const anchor of anchors) {
      let parsed;
      try { parsed = new URL(anchor.href, location.href); } catch { continue; }
      const match = /^\/@([A-Za-z0-9_.-]+)\/video\/(\d+)/.exec(parsed.pathname);
      if (!match || parsed.origin !== location.origin || videos.has(match[2])) continue;
      const card = anchor.closest('[data-e2e="user-post-item"],[data-e2e="search_top-item"]') || anchor;
      const caption = card.querySelector('[data-e2e*="desc"]')?.textContent || anchor.getAttribute('aria-label') ||
        anchor.querySelector('img')?.alt || anchor.textContent || '';
      videos.set(match[2], {id: match[2], desc: caption.slice(0, 4000), createTime: null,
        author: {uniqueId: match[1]}, stats: {commentCount: null}});
    }
    window.postMessage({
      type: "CHENGXIAN_PAGE_DATA", videos: [...videos.values()].slice(0, 1000), diagnostics
    }, location.origin);
  });
})();
