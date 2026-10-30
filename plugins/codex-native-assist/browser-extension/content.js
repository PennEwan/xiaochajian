(function () {
  "use strict";

  // The native window opens a nonce-scoped request on this fixed loopback port. Polling at 120 ms
  // keeps the click-to-copy path responsive while the in-flight guard avoids duplicate requests.
  var bridge = "http://127.0.0.1:43991";
  var busy = false;
  var timer = null;

  function sessionUrl() {
    return location.origin + "/api/auth/session?utm_source=chatgpt.com";
  }

  function getPending() {
    // Long-poll: the bridge holds this for up to 25 s and answers the moment the window asks for a
    // session, so a throttled timer in a background tab no longer delays the click.
    return fetch(bridge + "/v1/browser-session/pending?wait=25", {
      method: "GET",
      cache: "no-store",
      credentials: "omit"
    }).then(function (response) {
      if (response.status !== 200) return null;
      return response.json();
    });
  }

  function sendResult(request, status, body) {
    return fetch(bridge + "/v1/browser-session/result", {
      method: "POST",
      cache: "no-store",
      credentials: "omit",
      headers: { "Content-Type": "text/plain;charset=UTF-8" },
      body: JSON.stringify({
        request_id: request.request_id,
        nonce: request.nonce,
        status: status,
        body: body
      })
    });
  }

  function readSession(request) {
    return fetch(sessionUrl(), {
      method: "GET",
      cache: "no-store",
      credentials: "include"
    }).then(function (response) {
      return response.text().then(function (body) {
        return sendResult(request, response.status, body);
      });
    }).catch(function () {
      return sendResult(request, 0, "");
    });
  }

  function poll() {
    if (busy) return;
    busy = true;
    getPending().then(function (request) {
      return request ? readSession(request) : null;
    }).catch(function () {
      // The native window may be closed or may not be running; keep polling quietly.
    }).then(function () {
      busy = false;
      // Chain the next long-poll immediately; also wake up as soon as the tab is visible again.
      setTimeout(poll, 50);
    });
  }

  poll();
  document.addEventListener("visibilitychange", function () { if (!document.hidden) poll(); });
  window.addEventListener("focus", poll);
  window.addEventListener("unload", function () {
    if (timer !== null) clearInterval(timer);
  });
})();
