## 2026-04-29 - [Middleware Exception Handling for Security Headers]
**Vulnerability:** Security headers (like CSP, X-Frame-Options) were not being applied to error responses (404, 500), potentially leaving the application vulnerable during failure states.
**Learning:** In `aiohttp`, middleware that only modifies the `response` returned by `await handler(request)` misses `web.HTTPException` cases, which skip the remainder of the middleware logic when raised.
**Prevention:** Always wrap the `handler(request)` call in a `try...except web.HTTPException` block in `aiohttp` middleware to explicitly apply security headers to the exception's `headers` before re-raising.
