/** Select only the known same-origin deployment modes. */
function apiPrefix() {
  const prefix = globalThis.document?.querySelector?.('meta[name="hospital-api-prefix"]')?.content?.trim() ?? "";
  if (prefix !== "" && prefix !== "/api") throw new Error("invalid_api_prefix");
  return prefix;
}

/** Resolve the call API on the current origin; the empty prefix preserves local behavior. */
export function apiPath(path) {
  const prefix = apiPrefix();
  if (typeof path !== "string" || !path.startsWith("/") || path.startsWith("//") || path.startsWith("/\\")) {
    throw new Error("invalid_api_path");
  }
  return `${prefix}${path}`;
}

/** Leave the browser's Basic Auth header available to the proxy. */
export function callAuthorization(token) {
  if (!token) return {};
  const name = apiPrefix() === "/api" ? "X-Hospital-Authorization" : "Authorization";
  return { [name]: `Bearer ${token}` };
}
