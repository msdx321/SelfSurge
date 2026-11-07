// Answer only the catalog's status request locally, without an external API key.
function respond(status, data) {
  $done({
    response: {
      status,
      headers: {
        "Content-Type": "application/json; charset=utf-8",
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
      },
      body: JSON.stringify(data),
    },
  });
}

const origin = Object.entries($request.headers || {})
  .find(([key]) => key.toLowerCase() === "origin");

if ($request.method !== "GET") {
  respond(405, { error: "Only GET is supported" });
} else if (origin && origin[1] !== "https://msdx321.github.io") {
  respond(403, { error: "Only the SelfSurge catalog can read status" });
} else {
  $httpAPI("GET", "/v1/modules", {}, (result) => {
    if (!result || ![result.enabled, result.available].every(
      (names) => Array.isArray(names) && names.every((name) => typeof name === "string")
    )) {
      respond(502, { error: "Surge did not return a module list" });
      return;
    }
    respond(200, { bridge: "SelfSurge", enabled: result.enabled, available: result.available });
  });
}
